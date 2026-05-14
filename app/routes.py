import hashlib
import hmac
import io
import threading
import time
from datetime import datetime, timezone

import qrcode
from cachetools import TTLCache
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config
from .auth import get_current_user
from .database import get_db
from .limiter import limiter
from .models import AuditLog, ScanEvent, UrlMapping, User
from .schemas import (
    AuditEntry,
    AuditLogResponse,
    CreateRequest,
    CreateResponse,
    MyQRsResponse,
    QRInfoResponse,
    QRSummary,
    RotateEditTokenResponse,
    UpdateRequest,
)
from .token_gen import generate_edit_token, generate_token
from .url_validator import validate_url

router = APIRouter()

# In-memory cache (simulates Redis for prototype).
#
# Two TTLs are at work here, on purpose:
#
# - The TTLCache's own `ttl=3600` is a *cache freshness* bound: entries
#   are evicted an hour after insertion regardless of QR expiry, so DB
#   schema changes or rare DB-side mutations are picked up within ≤1 h.
# - The `(url, expires_at)` value tuple lets the redirect handler also
#   enforce the QR's own expiry on every hit, independent of the cache
#   TTL. This is what makes time-limited links 410 the moment they
#   pass their `expires_at` rather than waiting up to 1 h for the
#   cache entry to age out.
#
# `maxsize=10_000` bounds memory: at ~100 bytes per entry that's ~1 MB
# resident even under attack. LRU eviction kicks in past the cap, so a
# warm working set survives while cold tokens get dropped.
redirect_cache: TTLCache = TTLCache(maxsize=10_000, ttl=3600)

# Pulled from config so deployments can override via env (BASE_URL=
# https://qr.example.com). Re-exposed at module scope so tests can
# monkey-patch one location without re-reading env.
BASE_URL = config.BASE_URL

# Per-(token, ip) timestamp of the most recent scan we recorded. Used to
# dedupe rapid-fire refreshes from the same client so a single attacker
# can't bloat `scan_events` from one IP. The redirect itself still serves
# 302 — only the DB INSERT is skipped on hit.
_scan_last_seen: dict[tuple[str, str], float] = {}
SCAN_DEDUP_WINDOW = config.SCAN_DEDUP_WINDOW

# Per-IP rate limits resolved via callables so tests can lower them
# without re-importing. Defaults from config / env. 300/min on redirect
# ≈ 5 req/sec is transparent for legitimate NAT traffic; 30/min on
# mutations is defense-in-depth on the bearer-token check.
REDIRECT_RATE_LIMIT = config.REDIRECT_RATE_LIMIT
MUTATION_RATE_LIMIT = config.MUTATION_RATE_LIMIT
CREATE_RATE_LIMIT = config.CREATE_RATE_LIMIT


def _create_rate_limit() -> str:
    return CREATE_RATE_LIMIT


def _redirect_rate_limit() -> str:
    return REDIRECT_RATE_LIMIT


def _mutation_rate_limit() -> str:
    return MUTATION_RATE_LIMIT


# --- Scan-event write batching -------------------------------------------
#
# The original implementation INSERT-then-commit'd a `scan_events` row
# inside every redirect call, which (a) made the hot path block on disk
# fsync and (b) gave an attacker who's evading the per-(token, ip) dedup
# a 1:1 ratio between HTTP requests and DB writes. We now buffer scan
# rows in memory and flush them in batches.
#
# Two flush triggers:
#   1. `_pending_scans` reaches `SCAN_FLUSH_BATCH_SIZE` (size-based).
#   2. More than `SCAN_FLUSH_INTERVAL` seconds elapsed since the last
#      flush (time-based, so trailing partial batches don't sit forever).
# Plus an explicit force-flush from `/analytics` so a reader sees a
# consistent view, and a lifespan-shutdown drain in `app.main` so we
# don't lose buffered scans on process exit.
_pending_scans: list[dict] = []
_pending_lock = threading.Lock()
# Initialized to monotonic() so that `now_mono - _last_flush_time` is a
# small positive delta from the start, not a billion seconds (which
# would short-circuit the time-based flush at boot). Tests reset this
# in conftest with the same `time.monotonic()` value.
_last_flush_time: float = time.monotonic()

# Pulled from config so deployments can tune via env. Module-level
# constants so tests monkey-patch one location.
SCAN_FLUSH_BATCH_SIZE = config.SCAN_FLUSH_BATCH_SIZE
SCAN_FLUSH_INTERVAL = config.SCAN_FLUSH_INTERVAL


def _drain_buffer_locked() -> list[dict]:
    """Snapshot and clear `_pending_scans` under the lock. Caller flushes."""
    global _last_flush_time
    batch = _pending_scans[:]
    _pending_scans.clear()
    _last_flush_time = time.monotonic()
    return batch


def _flush_scans_to_db(batch: list[dict], db: Session) -> None:
    """One bulk INSERT + commit for the snapshot. No-op on empty input."""
    if not batch:
        return
    db.add_all(ScanEvent(**row) for row in batch)
    db.commit()


def force_flush_pending_scans(db: Session) -> None:
    """Public entry: drain the buffer and write whatever's there.

    Called by `/analytics` so a reader sees their own scans, and by
    the lifespan shutdown handler in `app.main` so a graceful stop
    doesn't lose buffered rows.
    """
    with _pending_lock:
        batch = _drain_buffer_locked()
    _flush_scans_to_db(batch, db)


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
@limiter.limit(_create_rate_limit)
def create_qr(
    request: Request,
    req: CreateRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    # slowapi reads the client IP off `request`; the param must be named
    # `request` for the decorator to find it. We don't otherwise use it
    # here — but it's required to be in the signature.
    if user is None:
        # Anonymous create is no longer allowed. The previous behavior
        # produced an orphan QR whose only owner-equivalent was the
        # one-time edit_token; that "limbo" state confused users (the
        # UI offered a Create form without any indication that the
        # result would be untraceable). Now we require a session.
        raise HTTPException(
            status_code=401,
            detail="Sign in to create QR codes.",
        )
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
        # Tag with owner only if the caller is authenticated. Anonymous
        # creates still produce a usable QR + edit_token; they just
        # won't show up in anyone's "My QRs" list.
        owner_id=user.id if user is not None else None,
        expires_at=expires_at,
    )
    db.add(mapping)
    db.flush()  # populates mapping.id for the audit log FK
    _log_audit(db, mapping, user, request, "create", after=normalized_url)
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
@limiter.limit(_redirect_rate_limit)
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


@router.get("/api/qr/mine", response_model=MyQRsResponse)
def list_my_qrs(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Return the signed-in user's owned, non-deleted QR codes.

    REGISTERED BEFORE `/api/qr/{token}` on purpose — FastAPI matches
    routes in registration order, and `mine` would otherwise be
    captured by the `{token}` path parameter and 404 out of
    `_get_mapping_or_404`.

    Anonymous callers get an empty list — the UI uses 200/empty as
    the "no QRs to show" signal, so it doesn't need a separate 401
    path just to render the sidebar header.
    """
    if user is None:
        return MyQRsResponse(items=[])

    rows = (
        db.query(UrlMapping)
        .filter(UrlMapping.owner_id == user.id)
        .filter(UrlMapping.is_deleted.is_(False))
        .order_by(UrlMapping.created_at.desc())
        .all()
    )
    return MyQRsResponse(
        items=[
            QRSummary(
                token=r.token,
                short_url=f"{BASE_URL}/r/{r.token}",
                original_url=r.original_url,
                created_at=r.created_at,
                updated_at=r.updated_at,
                expires_at=r.expires_at,
            )
            for r in rows
        ]
    )


@router.get("/api/qr/{token}", response_model=QRInfoResponse)
def get_qr_info(token: str, db: Session = Depends(get_db)):
    mapping = _get_mapping_or_404(token, db)
    return mapping


@router.patch("/api/qr/{token}", response_model=QRInfoResponse)
@limiter.limit(_mutation_rate_limit)
def update_qr(
    token: str,
    req: UpdateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    mapping = _get_mapping_or_404(token, db)
    _require_edit_authorization(mapping, authorization, user)

    if req.url is not None:
        try:
            new_url = validate_url(req.url)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        _log_audit(
            db, mapping, user, request, "patch_url",
            before=mapping.original_url, after=new_url,
        )
        mapping.original_url = new_url
        # Invalidate cache
        redirect_cache.pop(token, None)

    if req.expires_at is not None:
        new_expires = _to_naive_utc(req.expires_at)
        _log_audit(
            db, mapping, user, request, "patch_expires",
            before=(mapping.expires_at.isoformat() if mapping.expires_at else None),
            after=(new_expires.isoformat() if new_expires else None),
        )
        mapping.expires_at = new_expires
        # Invalidate cache
        redirect_cache.pop(token, None)

    db.commit()
    db.refresh(mapping)
    return mapping


@router.post(
    "/api/qr/{token}/rotate-edit-token", response_model=RotateEditTokenResponse
)
@limiter.limit(_mutation_rate_limit)
def rotate_edit_token_route(
    token: str,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Issue a fresh edit_token and invalidate the old one.

    Authorization: either the current edit_token (Authorization header)
    OR being signed in as the mapping's owner. Owners can now rotate
    the bearer credential even if they never saved it.
    """
    mapping = _get_mapping_or_404(token, db)
    _require_edit_authorization(mapping, authorization, user)

    new_plain, new_hash = generate_edit_token()
    mapping.edit_token_hash = new_hash
    _log_audit(
        db, mapping, user, request, "rotate_edit_token",
        # No values logged — recording the hashes (old or new) would
        # defeat the point of hashing them in the first place.
    )
    db.commit()
    # `updated_at` auto-bumps via the SQLAlchemy onupdate trigger.
    return RotateEditTokenResponse(edit_token=new_plain)


@router.delete("/api/qr/{token}")
@limiter.limit(_mutation_rate_limit)
def delete_qr(
    token: str,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    mapping = _get_mapping_or_404(token, db)
    _require_edit_authorization(mapping, authorization, user)
    _log_audit(db, mapping, user, request, "delete")
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

    # Buffered scans aren't yet visible to a COUNT/GROUP BY query —
    # drain the buffer first so a caller sees a consistent view.
    force_flush_pending_scans(db)

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


@router.get("/api/qr/{token}/audit", response_model=AuditLogResponse)
def get_qr_audit(
    token: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Return the owner-visible audit trail for this QR.

    Owner-only by design — the trail reveals when destinations were
    changed and to what, which is sensitive enough that we won't
    hand it out anonymously. Anonymous → 401, non-owner → 403.

    Looks up the mapping WITHOUT `_get_mapping_or_404` (which 404s
    soft-deleted rows): the audit trail is more useful AFTER a
    delete, not less, so we read deleted mappings here too. A
    genuinely unknown token still 404s.

    Capped at 100 most-recent entries server-side. A full
    pagination surface is a future enhancement.
    """
    if user is None:
        raise HTTPException(
            status_code=401, detail="Sign in to view a QR's audit log."
        )

    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
    if mapping is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if mapping.owner_id != user.id:
        # Don't leak the existence of someone else's QR — same 403
        # whether the row exists or not, so the audit endpoint can't
        # be used as a token-existence oracle for cross-user probing.
        raise HTTPException(
            status_code=403, detail="This QR's audit log is owner-only."
        )

    rows = (
        db.query(AuditLog)
        .filter(AuditLog.mapping_id == mapping.id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(100)
        .all()
    )
    return AuditLogResponse(
        items=[
            AuditEntry(
                action=r.action,
                before_value=r.before_value,
                after_value=r.after_value,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


def _get_mapping_or_404(token: str, db: Session) -> UrlMapping:
    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
    if mapping is None or mapping.is_deleted:
        raise HTTPException(status_code=404, detail="Not Found")
    return mapping


def _log_audit(
    db: Session,
    mapping: UrlMapping,
    user: User | None,
    request: Request,
    action: str,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """Record one mutation on a UrlMapping. Caller is responsible for
    the surrounding db.commit() — we want the audit row to land in the
    same transaction as the mutation it describes, so a half-applied
    change can't end up unaudited.

    `user` is the currently-signed-in user (None when only a bearer
    token was used). `before`/`after` are loose strings; format
    depends on the action.
    """
    db.add(
        AuditLog(
            mapping_id=mapping.id,
            user_id=user.id if user is not None else None,
            action=action,
            before_value=before,
            after_value=after,
            ip_address=request.client.host if request.client else None,
        )
    )


def _require_edit_authorization(
    mapping: UrlMapping,
    authorization: str | None,
    user: User | None,
) -> None:
    """Authorize a mutation on `mapping` by EITHER ownership OR bearer.

    Two paths are accepted, in order:

    1. **Owner shortcut.** The caller is signed in AND owns the
       mapping (`mapping.owner_id == user.id`). No bearer needed —
       the session cookie is the credential. This is the path most
       users take from the UI's "My QRs" sidebar.

    2. **Bearer fallback.** The caller presents the correct
       `Authorization: Bearer <edit_token>` header. This is the path
       for anonymous creators (no account) and for programmatic
       callers like CI scripts. The header is hashed and compared
       with `hmac.compare_digest` for timing-attack resistance.

    Mappings with `edit_token_hash is None` AND no owner are
    un-editable — that's the legacy-row case noted in the column doc.
    """
    # Path 1: owner shortcut
    if user is not None and mapping.owner_id == user.id:
        return

    # Path 2: bearer fallback
    if mapping.edit_token_hash is None:
        raise HTTPException(
            status_code=401,
            detail="This link is not editable (no edit_token on record).",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "This link is owned by another user. Provide "
                "`Authorization: Bearer <edit_token>` or sign in as the owner."
            ),
        )

    presented = authorization[len("Bearer ") :].strip()
    presented_hash = hashlib.sha256(presented.encode()).hexdigest()

    if not hmac.compare_digest(presented_hash, mapping.edit_token_hash):
        raise HTTPException(status_code=401, detail="Invalid edit_token.")


def _record_scan(token: str, request: Request, db: Session):
    """Buffer one ScanEvent. Flushes on size or time threshold.

    Per-(token, ip) burst dedup runs first — if the same client just
    scanned within `SCAN_DEDUP_WINDOW` seconds, this returns without
    even buffering. Otherwise the row is appended to `_pending_scans`
    and flushed in batches; the synchronous commit per redirect is
    gone.
    """
    ip = request.client.host if request.client else "unknown"

    if SCAN_DEDUP_WINDOW > 0:
        now = time.monotonic()
        key = (token, ip)
        last = _scan_last_seen.get(key)
        if last is not None and (now - last) < SCAN_DEDUP_WINDOW:
            return
        _scan_last_seen[key] = now
        # Lazy GC: when the dict grows large, drop entries older than
        # 30× the dedup window. Bounds memory without a background task.
        if len(_scan_last_seen) > 5000:
            cutoff = now - SCAN_DEDUP_WINDOW * 30
            stale = [k for k, t in _scan_last_seen.items() if t < cutoff]
            for k in stale:
                _scan_last_seen.pop(k, None)

    row = {
        "token": token,
        "user_agent": (request.headers.get("user-agent") or "")[:500] or None,
        "ip_address": ip if ip != "unknown" else None,
    }

    # Append; if we cross the size threshold or enough time has passed,
    # drain the buffer and flush in one bulk INSERT.
    batch: list[dict] = []
    with _pending_lock:
        _pending_scans.append(row)
        now_mono = time.monotonic()
        if (
            len(_pending_scans) >= SCAN_FLUSH_BATCH_SIZE
            or (now_mono - _last_flush_time) >= SCAN_FLUSH_INTERVAL
        ):
            batch = _drain_buffer_locked()

    _flush_scans_to_db(batch, db)
