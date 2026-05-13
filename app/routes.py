import hashlib
import hmac
import io
from datetime import datetime, timezone

import qrcode
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import get_db
from .limiter import limiter
from .models import ScanEvent, UrlMapping
from .schemas import CreateRequest, CreateResponse, QRInfoResponse, UpdateRequest
from .token_gen import generate_edit_token, generate_token
from .url_validator import validate_url

router = APIRouter()

# In-memory cache (simulates Redis for prototype).
# Stores (url, expires_at_naive_utc | None) so the redirect handler can
# evict expired entries on hit instead of serving them past their TTL.
redirect_cache: dict[str, tuple[str, datetime | None]] = {}

BASE_URL = "http://localhost:8000"


def _now_naive() -> datetime:
    """Naive UTC `now`, comparable with the naive DateTime columns in models.py.

    `datetime.utcnow()` is deprecated in Python 3.12+; this is the
    forward-compatible spelling that still produces a naive value so it
    compares cleanly with `expires_at`.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Coerce a possibly tz-aware datetime to naive UTC.

    Pydantic parses ISO strings ending in `Z` or `+00:00` as tz-aware
    datetimes; the DB column is declared without `timezone=True`, so
    mixing both styles raises `TypeError` on `<`/`>` comparisons. We
    normalize everything to naive UTC at the application boundary.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@router.post("/api/qr/create", response_model=CreateResponse)
@limiter.limit("10/minute")
def create_qr(request: Request, req: CreateRequest, db: Session = Depends(get_db)):
    # slowapi reads the client IP off `request`; the param must be named
    # `request` for the decorator to find it. We don't otherwise use it
    # here — but it's required to be in the signature.
    try:
        normalized_url = validate_url(req.url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    token = generate_token(normalized_url, db)
    edit_token_plain, edit_token_hash = generate_edit_token()

    expires_at = _to_naive_utc(req.expires_at)

    mapping = UrlMapping(
        token=token,
        original_url=normalized_url,
        edit_token_hash=edit_token_hash,
        expires_at=expires_at,
    )
    db.add(mapping)
    db.commit()

    short_url = f"{BASE_URL}/r/{token}"

    # Warm cache with the same expiry the DB sees, so the redirect handler
    # can short-circuit without a DB hit.
    redirect_cache[token] = (normalized_url, expires_at)

    return CreateResponse(
        token=token,
        short_url=short_url,
        qr_code_url=f"{BASE_URL}/api/qr/{token}/image",
        original_url=normalized_url,
        edit_token=edit_token_plain,
    )


@router.get("/r/{token}")
def redirect(token: str, request: Request, db: Session = Depends(get_db)):
    """Cache → DB → 404/410. The hottest path in the system.

    The cache stores (url, expires_at) so we can serve permanent links
    AND time-limited links from memory. On a cache hit past TTL we evict
    the entry and fall through to the DB path, which produces the 410
    response with the canonical "expired" detail.
    """
    now = _now_naive()

    # ----- Cache path ---------------------------------------------------
    cached = redirect_cache.get(token)
    if cached is not None:
        url, exp = cached
        if exp is None or exp > now:
            _record_scan(token, request, db)
            return RedirectResponse(url=url, status_code=302)
        # Cached entry has expired — evict and let the DB path handle 410.
        redirect_cache.pop(token, None)

    # ----- DB path ------------------------------------------------------
    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()

    if mapping is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if mapping.is_deleted:
        raise HTTPException(status_code=410, detail="Gone — this link has been deleted")

    if mapping.expires_at is not None and mapping.expires_at <= now:
        raise HTTPException(status_code=410, detail="Gone — this link has expired")

    # Warm cache with the DB-observed expiry so the next hit can short-circuit.
    redirect_cache[token] = (mapping.original_url, mapping.expires_at)

    _record_scan(token, request, db)
    return RedirectResponse(url=mapping.original_url, status_code=302)


@router.get("/api/qr/{token}", response_model=QRInfoResponse)
def get_qr_info(token: str, db: Session = Depends(get_db)):
    mapping = _get_mapping_or_404(token, db)
    return mapping


@router.patch("/api/qr/{token}", response_model=QRInfoResponse)
def update_qr(
    token: str,
    req: UpdateRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    mapping = _get_mapping_or_404(token, db)
    _require_edit_token(mapping, authorization)

    if req.url is not None:
        try:
            mapping.original_url = validate_url(req.url)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        # Invalidate cache
        redirect_cache.pop(token, None)

    if req.expires_at is not None:
        mapping.expires_at = _to_naive_utc(req.expires_at)
        # Invalidate cache
        redirect_cache.pop(token, None)

    db.commit()
    db.refresh(mapping)
    return mapping


@router.delete("/api/qr/{token}")
def delete_qr(
    token: str,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    mapping = _get_mapping_or_404(token, db)
    _require_edit_token(mapping, authorization)
    mapping.is_deleted = True
    db.commit()
    # Invalidate cache
    redirect_cache.pop(token, None)
    return {"detail": "Deleted"}


@router.get("/api/qr/{token}/image")
def get_qr_image(token: str, db: Session = Depends(get_db)):
    _get_mapping_or_404(token, db)
    short_url = f"{BASE_URL}/r/{token}"

    img = qrcode.make(short_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@router.get("/api/qr/{token}/analytics")
def get_analytics(token: str, db: Session = Depends(get_db)):
    _get_mapping_or_404(token, db)

    total = db.query(func.count(ScanEvent.id)).filter(ScanEvent.token == token).scalar()

    daily = (
        db.query(
            func.date(ScanEvent.scanned_at).label("date"),
            func.count(ScanEvent.id).label("count"),
        )
        .filter(ScanEvent.token == token)
        .group_by(func.date(ScanEvent.scanned_at))
        .all()
    )

    return {
        "token": token,
        "total_scans": total,
        "scans_by_day": [{"date": str(row.date), "count": row.count} for row in daily],
    }


def _get_mapping_or_404(token: str, db: Session) -> UrlMapping:
    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
    if mapping is None or mapping.is_deleted:
        raise HTTPException(status_code=404, detail="Not Found")
    return mapping


def _require_edit_token(mapping: UrlMapping, authorization: str | None) -> None:
    """Reject PATCH/DELETE unless the caller presents the right edit token.

    The header is the standard `Authorization: Bearer <plaintext>`.
    We hash the presented value and compare it against `edit_token_hash`
    on the row with `hmac.compare_digest` to keep timing-attack resistance
    on the hash comparison. Rows created before this column existed have
    `edit_token_hash is None` and are treated as un-editable — there's
    no way to recover a credential for them, which matches the "credential
    is shown once at create time" semantics.
    """
    if mapping.edit_token_hash is None:
        # Legacy row, or somehow created without an edit token. Refuse.
        raise HTTPException(
            status_code=401,
            detail="This link is not editable (no edit_token on record).",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization: Bearer <edit_token> header.",
        )

    presented = authorization[len("Bearer ") :].strip()
    presented_hash = hashlib.sha256(presented.encode()).hexdigest()

    if not hmac.compare_digest(presented_hash, mapping.edit_token_hash):
        raise HTTPException(status_code=401, detail="Invalid edit_token.")


def _record_scan(token: str, request: Request, db: Session):
    event = ScanEvent(
        token=token,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    db.add(event)
    db.commit()
