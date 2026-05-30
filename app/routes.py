import hashlib
import hmac
import io
import re
import threading
import time
from datetime import datetime, timezone

import qrcode
from cachetools import TTLCache
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import (
    HorizontalGradiantColorMask,
    RadialGradiantColorMask,
    SolidFillColorMask,
    SquareGradiantColorMask,
    VerticalGradiantColorMask,
)
from qrcode.image.styles.moduledrawers.pil import (
    CircleModuleDrawer,
    GappedSquareModuleDrawer,
    RoundedModuleDrawer,
    SquareModuleDrawer,
)
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import config
from .auth import get_current_user
from .database import get_db
from .limiter import limiter
from .models import AuditLog, ScanEvent, UrlMapping, User
from .schemas import (
    AuditEntry,
    AuditLogResponse,
    BulkDeleteRequest,
    BulkDeleteResponse,
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
BASE_URL_AUTO = config.BASE_URL_AUTO


def _base_url(request: Request) -> str:
    """Wrapper that respects the routes-module monkey-patches used by
    tests (`routes.BASE_URL` / `routes.BASE_URL_AUTO`) instead of
    going straight through to `url_helpers.base_url`. Test fixtures
    flip these locally to exercise both auto-derive and explicit
    paths — see test_short_url_*."""
    if BASE_URL_AUTO:
        return str(request.base_url).rstrip("/")
    return BASE_URL

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

    base = _base_url(request)
    short_url = f"{base}/r/{token}"

    # Warm cache with the same expiry the DB sees, so the redirect handler
    # can short-circuit without a DB hit. Fresh QRs are 302 by default.
    redirect_cache[token] = (normalized_url, expires_at, 302)

    return CreateResponse(
        token=token,
        short_url=short_url,
        qr_code_url=f"{base}/api/qr/{token}/image",
        original_url=normalized_url,
        edit_token=edit_token_plain,
    )


# Bounded cache for promoted (301) redirects. Without this header
# Chrome treats 301 as cacheable indefinitely (RFC default), so a
# destination change after promotion never reaches already-cached
# clients — they'd be stuck on the old destination until they
# manually cleared cache. 300 s matches what production short-URL
# services do (t.co uses ~10 s; Cloudflare/Stripe use ~3600 s);
# 5 min is the middle that still saves repeat-scan round-trips
# while capping the blast radius of a destination change at one
# coffee break. Captured in DECISIONS.md "301 cache trade-off".
_PROMOTED_301_CACHE_HEADER = "public, max-age=300, must-revalidate"
# 302 must NOT be cached — every scan is supposed to hit us so
# Update / Delete propagate instantly and we can record analytics.
# Modern browsers won't cache a 302 by default but proxies and
# corporate caches might, so be explicit.
_TEMPORARY_302_CACHE_HEADER = "no-store"


def _redirect_with_cache_header(url: str, status: int) -> RedirectResponse:
    """Build a 301/302 RedirectResponse with the right Cache-Control
    so a future Update / Delete actually propagates within a bounded
    window. See `_PROMOTED_301_CACHE_HEADER` for the rationale."""
    header = (
        _PROMOTED_301_CACHE_HEADER if status == 301 else _TEMPORARY_302_CACHE_HEADER
    )
    return RedirectResponse(
        url=url, status_code=status, headers={"Cache-Control": header}
    )


@router.get("/r/{token}")
@limiter.limit(_redirect_rate_limit)
def redirect(token: str, request: Request, db: Session = Depends(get_db)):
    """Cache → DB → 404/410. The hottest path in the system.

    The cache stores `(url, expires_at, status)` so we can serve
    permanent links AND time-limited links AND promoted-to-301 links
    from memory. On a cache hit past TTL we evict the entry and fall
    through to the DB path, which produces the 410 response with the
    canonical "expired" detail.

    Every response carries an explicit `Cache-Control` (see
    `_redirect_with_cache_header`) — without it Chrome will cache
    a 301 indefinitely and a later destination change would never
    reach already-cached clients.
    """
    now = _now_naive()

    # ----- Cache path ---------------------------------------------------
    cached = redirect_cache.get(token)
    if cached is not None:
        url, exp, status = cached
        if exp is None or exp > now:
            _record_scan(token, request, db)
            return _redirect_with_cache_header(url, status)
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

    # Warm cache with the DB-observed expiry + status so the next hit can short-circuit.
    redirect_cache[token] = (mapping.original_url, mapping.expires_at, mapping.redirect_status)

    _record_scan(token, request, db)
    return _redirect_with_cache_header(mapping.original_url, mapping.redirect_status)


@router.get("/api/qr/mine", response_model=MyQRsResponse)
def list_my_qrs(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    include_deleted: bool = Query(
        default=False,
        description="If true, include soft-deleted rows so the UI can offer Restore.",
    ),
    search: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive substring match on token OR original_url.",
    ),
    sort: str = Query(
        default="created_desc",
        pattern="^(created_desc|created_asc|updated_desc|updated_asc|destination)$",
        description="Sort order. Default newest-first.",
    ),
):
    """Return the signed-in user's owned QR codes.

    REGISTERED BEFORE `/api/qr/{token}` on purpose — FastAPI matches
    routes in registration order, and `mine` would otherwise be
    captured by the `{token}` path parameter and 404 out of
    `_get_mapping_or_404`.

    Anonymous callers get an empty list — the UI uses 200/empty as
    the "no QRs to show" signal, so it doesn't need a separate 401
    path just to render the sidebar header.

    `include_deleted` / `search` / `sort` are applied server-side so a
    user with thousands of QRs doesn't have to ship them all to the
    client just to filter. (We're nowhere near that scale yet — same
    pattern, smaller payload regardless.)
    """
    if user is None:
        return MyQRsResponse(items=[])

    q = db.query(UrlMapping).filter(UrlMapping.owner_id == user.id)
    if not include_deleted:
        q = q.filter(UrlMapping.is_deleted.is_(False))

    if search:
        like = f"%{search.strip()}%"
        q = q.filter(
            or_(
                UrlMapping.token.ilike(like),
                UrlMapping.original_url.ilike(like),
            )
        )

    sort_map = {
        "created_desc": UrlMapping.created_at.desc(),
        "created_asc": UrlMapping.created_at.asc(),
        "updated_desc": UrlMapping.updated_at.desc(),
        "updated_asc": UrlMapping.updated_at.asc(),
        "destination": UrlMapping.original_url.asc(),
    }
    rows = q.order_by(sort_map[sort]).all()
    base = _base_url(request)

    return MyQRsResponse(
        items=[
            QRSummary(
                token=r.token,
                short_url=f"{base}/r/{r.token}",
                original_url=r.original_url,
                created_at=r.created_at,
                updated_at=r.updated_at,
                expires_at=r.expires_at,
                redirect_status=r.redirect_status,
                is_deleted=r.is_deleted,
                deleted_at=r.deleted_at,
            )
            for r in rows
        ]
    )


@router.post("/api/qr/bulk-delete", response_model=BulkDeleteResponse)
@limiter.limit(_mutation_rate_limit)
def bulk_delete_qrs(
    req: BulkDeleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Soft-delete a batch of owned QRs in one transaction.

    Owner-only — no bearer fallback. Bulk auth via per-row bearers
    would mean N tokens in one request which the protocol doesn't
    have a sane shape for. Programmatic clients that need bulk
    operations can loop over the per-row DELETE endpoint.

    All-or-nothing: if ANY token isn't owned by the caller, the
    entire batch fails with 403 and no rows are touched. This avoids
    "you deleted 4 of 5, the 5th was someone else's" surprises.
    """
    if user is None:
        raise HTTPException(
            status_code=401, detail="Sign in to bulk-delete QRs."
        )

    # Dedupe to avoid double-logging the same row if the client
    # accidentally sent a token twice. Order is preserved in the
    # response so callers can correlate with their input.
    unique_tokens = list(dict.fromkeys(req.tokens))

    rows = (
        db.query(UrlMapping)
        .filter(UrlMapping.token.in_(unique_tokens))
        .all()
    )
    by_token = {r.token: r for r in rows}

    for tok in unique_tokens:
        row = by_token.get(tok)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown token: {tok}"
            )
        if row.owner_id != user.id:
            # Same 403 string regardless of whether the row exists —
            # no cross-user existence oracle via this endpoint.
            raise HTTPException(
                status_code=403,
                detail="One or more tokens are not owned by you.",
            )

    now = _now_naive()
    affected: list[str] = []
    for tok in unique_tokens:
        row = by_token[tok]
        if row.is_deleted:
            # Idempotent: re-deleting a deleted row is a no-op, not an
            # error. The audit log already has the original delete.
            continue
        _log_audit(db, row, user, request, "delete")
        row.is_deleted = True
        row.deleted_at = now
        redirect_cache.pop(row.token, None)
        affected.append(row.token)

    db.commit()
    return BulkDeleteResponse(deleted=len(affected), tokens=affected)


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

    if req.redirect_status is not None:
        # Schema already validated this is 301. Reject the request
        # outright if the row is already 301 — silently accepting it
        # would create a duplicate audit-log entry for a no-op.
        if mapping.redirect_status == 301:
            raise HTTPException(
                status_code=409,
                detail="This link is already a 301 (permanent) redirect.",
            )
        _log_audit(
            db, mapping, user, request, "promote_to_301",
            before=str(mapping.redirect_status),
            after="301",
        )
        mapping.redirect_status = 301
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
    mapping.deleted_at = _now_naive()
    db.commit()
    # Invalidate cache
    redirect_cache.pop(token, None)
    return {"detail": "Deleted"}


@router.post("/api/qr/{token}/restore", response_model=QRInfoResponse)
@limiter.limit(_mutation_rate_limit)
def restore_qr(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Un-delete a soft-deleted QR. Owner-only.

    Bypasses `_get_mapping_or_404` (which 404s deleted rows) by reading
    the mapping directly. Bearer fallback isn't supported here on
    purpose: a script holding the edit_token of a deleted QR would
    normally have no way to discover it's deleted (the redirect 410s
    without distinguishing). Restoration is a UI affordance for the
    owner who's looking at the deleted-list, not a routine API op.
    """
    if user is None:
        raise HTTPException(
            status_code=401, detail="Sign in to restore a QR."
        )

    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
    if mapping is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if mapping.owner_id != user.id:
        raise HTTPException(
            status_code=403, detail="This QR is owned by another user."
        )
    if not mapping.is_deleted:
        # Idempotency would let this 200, but a non-deleted "restore"
        # is almost certainly a UI bug — surface it.
        raise HTTPException(
            status_code=409, detail="This QR is not deleted."
        )

    _log_audit(db, mapping, user, request, "restore")
    mapping.is_deleted = False
    mapping.deleted_at = None
    db.commit()
    db.refresh(mapping)
    # Cache was cleared on the original delete; no eviction needed.
    return mapping


# --- QR image styling ----------------------------------------------------
#
# The image endpoint accepts a handful of presentation knobs as query
# params. They're deliberately stateless — styling is a pure function of
# (short_url, params), never persisted on the mapping — so a restyle
# can't change where the QR points, no DB migration is needed, and the
# same token can be rendered in different palettes for different
# contexts (dark slide deck vs. printed flyer) without forking the row.
# See DECISIONS.md "QR styling: query params, not stored columns".

# Error-correction levels. Higher levels embed more redundancy so the
# code still scans when partly damaged/obscured, at the cost of a denser
# matrix: L ~7%, M ~15%, Q ~25%, H ~30%. 'M' is the qrcode-library
# default and the right balance for a clean screen/print scan; bump to
# 'H' if you plan to overlay a logo or expect physical wear.
_ECC_LEVELS = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}

# Accept #rgb / #rrggbb / rgb / rrggbb (case-insensitive). The leading
# '#' is optional because it's the URL fragment delimiter — callers
# passing it in a query string have to percent-encode it as %23, so we
# also accept the bare form.
_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Pillow's default QR image factory only emits a compact 1-bit PNG when
# the colors are exactly the strings "black"/"white"; any other value
# (including the equivalent hex) forces a larger RGB image. Mapping the
# canonical black/white hex back to those names keeps the default,
# uncustomized request producing the exact same small PNG it always did.
_PIL_NAMED_COLOR = {"#000000": "black", "#ffffff": "white"}


def _normalize_hex_color(value: str, *, field: str) -> str:
    """Validate a hex color string and return it normalized as '#rrggbb'.

    Raises HTTPException(422) on malformed input so a hand-crafted query
    string gets a clear, actionable error instead of a 500 bubbling up
    out of Pillow's color parser.
    """
    v = value.strip()
    if not _HEX_COLOR_RE.match(v):
        raise HTTPException(
            status_code=422,
            detail=(
                f"`{field}` must be a hex color like `1a3b7c` or `#1a3b7c` "
                f"(3 or 6 hex digits, '#' optional). Got: {value!r}"
            ),
        )
    v = v.lstrip("#").lower()
    if len(v) == 3:
        # Expand shorthand the way CSS does: #abc -> #aabbcc.
        v = "".join(ch * 2 for ch in v)
    return f"#{v}"


# --- Styled-render building blocks (module shape, gradient, logo) --------
#
# Beyond flat color, the endpoint can render rounded/circular modules, a
# gradient fill, and a centered logo via qrcode's StyledPilImage factory
# (module drawers + color masks + an embedded image). Flat + square +
# no-logo requests still take the original fast path in `_render_qr_png`,
# so the common case keeps its compact (and for black/white, 1-bit) output.

# Module (dot) shapes — factories, not instances: qrcode mutates drawer
# state during a render, so each render must get a fresh one.
_MODULE_DRAWERS = {
    "square": SquareModuleDrawer,
    "rounded": lambda: RoundedModuleDrawer(radius_ratio=1),
    "circle": CircleModuleDrawer,
    "gapped": lambda: GappedSquareModuleDrawer(size_ratio=0.85),
}

# Gradient styles. `none` is a solid `fill`; the rest sweep `fill`->`fill2`
# (center->edge for radial/square, left->right horizontal, top->bottom
# vertical).
_GRADIENTS = {"none", "radial", "square", "horizontal", "vertical"}

# Logo upload bounds. A 2 MB cap on the raw bytes plus Pillow's own
# decompression-bomb guard keep one request from allocating an unbounded
# bitmap. The ratio is the fraction of the QR width the logo spans; much
# above ~0.3 starts eating into scannability even at ECC H.
_LOGO_MAX_BYTES = 2 * 1024 * 1024
_LOGO_RATIO_MIN, _LOGO_RATIO_MAX = 0.1, 0.3


def _hex_to_rgb(normalized: str) -> tuple[int, int, int]:
    """'#rrggbb' (already normalized by `_normalize_hex_color`) -> (r, g, b).

    Color masks take RGB tuples, not the hex strings the flat-PIL factory
    accepts — this is the bridge for the styled path.
    """
    h = normalized.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _make_color_mask(gradient: str, back_rgb, fill_rgb, fill2_rgb):
    """Build the qrcode color mask for the requested gradient style."""
    if gradient == "radial":
        return RadialGradiantColorMask(
            back_color=back_rgb, center_color=fill_rgb, edge_color=fill2_rgb
        )
    if gradient == "square":
        return SquareGradiantColorMask(
            back_color=back_rgb, center_color=fill_rgb, edge_color=fill2_rgb
        )
    if gradient == "horizontal":
        return HorizontalGradiantColorMask(
            back_color=back_rgb, left_color=fill_rgb, right_color=fill2_rgb
        )
    if gradient == "vertical":
        return VerticalGradiantColorMask(
            back_color=back_rgb, top_color=fill_rgb, bottom_color=fill2_rgb
        )
    return SolidFillColorMask(back_color=back_rgb, front_color=fill_rgb)


def _resolve_style(
    *, fill: str, back: str, fill2: str, module: str, gradient: str, ecc: str
) -> tuple[str, str, str, str]:
    """Validate + normalize every style field shared by GET and POST.

    Returns `(fill_hex, back_hex, fill2_hex, ecc_key)`. Raises 422 on any
    malformed/unknown value so both handlers reject identically.
    """
    fill_hex = _normalize_hex_color(fill, field="fill")
    back_hex = _normalize_hex_color(back, field="back")
    fill2_hex = _normalize_hex_color(fill2, field="fill2")

    if module not in _MODULE_DRAWERS:
        raise HTTPException(
            status_code=422,
            detail=f"`module` must be one of {sorted(_MODULE_DRAWERS)}. Got: {module!r}",
        )
    if gradient not in _GRADIENTS:
        raise HTTPException(
            status_code=422,
            detail=f"`gradient` must be one of {sorted(_GRADIENTS)}. Got: {gradient!r}",
        )
    ecc_key = ecc.strip().upper()
    if ecc_key not in _ECC_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"`ecc` must be one of L, M, Q, H. Got: {ecc!r}",
        )

    # Zero-contrast guard only for solid fills — a gradient gets its
    # contrast from `fill2`, so fill==back there is fine.
    if gradient == "none" and fill_hex == back_hex:
        raise HTTPException(
            status_code=422,
            detail="`fill` and `back` are the same color — the QR would be a "
            "solid block and unscannable. Pick a dark fill on a light back.",
        )
    return fill_hex, back_hex, fill2_hex, ecc_key


def _read_logo(upload: UploadFile) -> Image.Image:
    """Read + validate an uploaded logo into an RGBA PIL image.

    Caps the raw upload at `_LOGO_MAX_BYTES` (413 if exceeded) and rejects
    anything Pillow can't decode as an image (422). RGBA so a transparent
    logo composites cleanly over the QR.
    """
    raw = upload.file.read(_LOGO_MAX_BYTES + 1)
    if len(raw) > _LOGO_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Logo too large (max {_LOGO_MAX_BYTES // (1024 * 1024)} MB).",
        )
    if not raw:
        raise HTTPException(status_code=422, detail="Logo file is empty.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force decode now so a bad/oversized image fails here
    except Exception:
        # UnidentifiedImageError, truncated files, decompression bombs —
        # all collapse to "not a usable image".
        raise HTTPException(
            status_code=422,
            detail="Logo must be a valid image file (PNG, JPEG, etc.).",
        )
    return img.convert("RGBA")


def _render_qr_png(
    short_url: str,
    *,
    fill_hex: str,
    back_hex: str,
    scale: int,
    border: int,
    ecc_key: str,
    module: str = "square",
    gradient: str = "none",
    fill2_hex: str = "#5b9eff",
    logo: Image.Image | None = None,
    logo_ratio: float = 0.22,
) -> bytes:
    """Render the QR for `short_url` to PNG bytes with the given styling.

    Flat + square + no-logo takes the original PilImage fast path (and its
    1-bit optimization for pure black/white); anything fancier goes through
    StyledPilImage. Shared by the GET (no logo) and POST (logo) handlers so
    the two render paths can't drift apart.
    """
    qr = qrcode.QRCode(
        version=None,  # auto-size the matrix to fit the data
        error_correction=_ECC_LEVELS[ecc_key],
        box_size=scale,
        border=border,
    )
    qr.add_data(short_url)
    qr.make(fit=True)

    if module == "square" and gradient == "none" and logo is None:
        img = qr.make_image(
            fill_color=_PIL_NAMED_COLOR.get(fill_hex, fill_hex),
            back_color=_PIL_NAMED_COLOR.get(back_hex, back_hex),
        )
    else:
        kwargs = dict(
            image_factory=StyledPilImage,
            module_drawer=_MODULE_DRAWERS[module](),
            color_mask=_make_color_mask(
                gradient,
                _hex_to_rgb(back_hex),
                _hex_to_rgb(fill_hex),
                _hex_to_rgb(fill2_hex),
            ),
        )
        if logo is not None:
            kwargs["embeded_image"] = logo
            kwargs["embeded_image_ratio"] = logo_ratio
        img = qr.make_image(**kwargs)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/api/qr/{token}/image")
def get_qr_image(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    download: bool = Query(
        default=False,
        description="If true, set Content-Disposition: attachment so the "
        "browser saves the file as `qr-<token>.png` instead of rendering inline.",
    ),
    fill: str = Query(
        default="000000",
        description="Hex color of the dark modules (e.g. `1a3b7c`). The '#' "
        "is optional; if you include it, percent-encode it as %23.",
    ),
    back: str = Query(
        default="ffffff",
        description="Hex color of the background / quiet zone.",
    ),
    scale: int = Query(
        default=10,
        ge=1,
        le=40,
        description="Pixels per QR module. Higher = sharper, larger PNG "
        "(affects the exported file resolution, not the on-screen preview).",
    ),
    border: int = Query(
        default=4,
        ge=0,
        le=20,
        description="Quiet-zone width in modules. The QR spec recommends "
        ">= 4 for reliable scanning; lower it only if you frame the code yourself.",
    ),
    ecc: str = Query(
        default="M",
        description="Error-correction level: L (~7%), M (~15%), Q (~25%), H (~30%).",
    ),
    module: str = Query(
        default="square",
        description="Module (dot) shape: square, rounded, circle, gapped.",
    ),
    gradient: str = Query(
        default="none",
        description="Foreground gradient: none, radial, square, horizontal, "
        "vertical. When set, the fill sweeps `fill` -> `fill2`.",
    ),
    fill2: str = Query(
        default="5b9eff",
        description="Gradient end color (hex). Only used when `gradient` != none.",
    ),
):
    # Image is a pure function of the short URL (+ style params) — render
    # it even for soft-deleted rows so the Restore preview in the UI can
    # show what's about to be brought back. Logos can't ride a GET query
    # string (they're binary), so the logo path lives on the POST twin below.
    _get_mapping_any_state_or_404(token, db)
    short_url = f"{_base_url(request)}/r/{token}"

    fill_hex, back_hex, fill2_hex, ecc_key = _resolve_style(
        fill=fill, back=back, fill2=fill2, module=module, gradient=gradient, ecc=ecc
    )
    png = _render_qr_png(
        short_url,
        fill_hex=fill_hex,
        back_hex=back_hex,
        scale=scale,
        border=border,
        ecc_key=ecc_key,
        module=module,
        gradient=gradient,
        fill2_hex=fill2_hex,
    )
    headers = (
        {"Content-Disposition": f'attachment; filename="qr-{token}.png"'}
        if download
        else None
    )
    return StreamingResponse(io.BytesIO(png), media_type="image/png", headers=headers)


@router.post("/api/qr/{token}/image")
def post_qr_image(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    fill: str = Form(default="000000"),
    back: str = Form(default="ffffff"),
    fill2: str = Form(default="5b9eff"),
    scale: int = Form(default=10),
    border: int = Form(default=4),
    ecc: str = Form(default="M"),
    module: str = Form(default="square"),
    gradient: str = Form(default="none"),
    logo_ratio: float = Form(default=0.22),
    logo: UploadFile | None = File(default=None),
):
    """Render the QR with an optional centered logo (multipart upload).

    The GET twin handles every style EXCEPT the logo, which is binary and
    can't ride a query string. The frontend only reaches for this endpoint
    when a logo is attached; everything else stays on the cacheable GET.

    Styling is still stateless: the logo is composited into THIS response
    and never stored on the mapping. A center logo occludes modules, so we
    force ECC `H` whenever one is present, regardless of the `ecc` field.
    """
    _get_mapping_any_state_or_404(token, db)
    short_url = f"{_base_url(request)}/r/{token}"

    # Form() (unlike Query()) doesn't enforce numeric bounds — do it here.
    if not 1 <= scale <= 40:
        raise HTTPException(status_code=422, detail="`scale` must be 1–40.")
    if not 0 <= border <= 20:
        raise HTTPException(status_code=422, detail="`border` must be 0–20.")
    logo_ratio = min(max(logo_ratio, _LOGO_RATIO_MIN), _LOGO_RATIO_MAX)

    fill_hex, back_hex, fill2_hex, ecc_key = _resolve_style(
        fill=fill, back=back, fill2=fill2, module=module, gradient=gradient, ecc=ecc
    )

    # `logo` arrives as None when the field is absent, or as an UploadFile
    # with an empty filename when the form sent an empty file input — treat
    # both as "no logo".
    logo_img = None
    if logo is not None and logo.filename:
        logo_img = _read_logo(logo)
        ecc_key = "H"

    png = _render_qr_png(
        short_url,
        fill_hex=fill_hex,
        back_hex=back_hex,
        scale=scale,
        border=border,
        ecc_key=ecc_key,
        module=module,
        gradient=gradient,
        fill2_hex=fill2_hex,
        logo=logo_img,
        logo_ratio=logo_ratio,
    )
    return StreamingResponse(io.BytesIO(png), media_type="image/png")


@router.get("/api/qr/{token}/analytics")
def get_analytics(
    token: str,
    db: Session = Depends(get_db),
    date_from: str | None = Query(
        default=None,
        alias="from",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Inclusive lower bound on scan date (YYYY-MM-DD).",
    ),
    date_to: str | None = Query(
        default=None,
        alias="to",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Inclusive upper bound on scan date (YYYY-MM-DD).",
    ),
):
    """Total + per-day scan counts, optionally bounded by a date range.

    Date params are inclusive on both ends and operate on the SQL
    `DATE(scanned_at)` projection so a `from=2025-12-01&to=2025-12-31`
    covers every scan in December regardless of time-of-day. Both
    are independently optional; omitting them returns all-time.

    Soft-deleted rows still report analytics: the scan history is
    immutable, and an owner inspecting a deleted QR before restoring
    it benefits from seeing the lifetime activity.
    """
    _get_mapping_any_state_or_404(token, db)

    # Buffered scans aren't yet visible to a COUNT/GROUP BY query —
    # drain the buffer first so a caller sees a consistent view.
    force_flush_pending_scans(db)

    scoped_total = db.query(func.count(ScanEvent.id)).filter(ScanEvent.token == token)
    scoped_daily = (
        db.query(
            func.date(ScanEvent.scanned_at).label("date"),
            func.count(ScanEvent.id).label("count"),
        )
        .filter(ScanEvent.token == token)
    )

    if date_from is not None:
        scoped_total = scoped_total.filter(func.date(ScanEvent.scanned_at) >= date_from)
        scoped_daily = scoped_daily.filter(func.date(ScanEvent.scanned_at) >= date_from)
    if date_to is not None:
        # Pattern validation prevents bad input here; sanity check that
        # `from` doesn't post-date `to` so the empty result isn't
        # mistaken for "no scans in range" silently.
        if date_from is not None and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="`from` must be on or before `to`.",
            )
        scoped_total = scoped_total.filter(func.date(ScanEvent.scanned_at) <= date_to)
        scoped_daily = scoped_daily.filter(func.date(ScanEvent.scanned_at) <= date_to)

    total = scoped_total.scalar()
    daily = scoped_daily.group_by(func.date(ScanEvent.scanned_at)).all()

    return {
        "token": token,
        "total_scans": total,
        "from": date_from,
        "to": date_to,
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


def _get_mapping_any_state_or_404(token: str, db: Session) -> UrlMapping:
    """Same as `_get_mapping_or_404` but returns soft-deleted rows too.

    Used by read-only endpoints where the deletion shouldn't hide the
    underlying record from the owner who's trying to inspect or
    restore it: the QR image (a pure function of the short URL), the
    analytics chart (historical, owner already saw it before delete).
    The redirect path itself still 410s — this only relaxes the
    metadata endpoints.
    """
    mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
    if mapping is None:
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
