from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateRequest(BaseModel):
    url: str
    expires_at: datetime | None = None


class CreateResponse(BaseModel):
    token: str
    short_url: str
    qr_code_url: str
    original_url: str
    # One-time edit token. Returned only on creation; the DB stores
    # only its SHA-256 hash. Required as `Authorization: Bearer ...`
    # on PATCH/DELETE for this token. If the caller loses it, the
    # short link is no longer editable.
    edit_token: str


class QRInfoResponse(BaseModel):
    token: str
    original_url: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    is_deleted: bool
    redirect_status: int = 302


class UpdateRequest(BaseModel):
    url: str | None = None
    expires_at: datetime | None = None
    # Promote-to-301 is the only redirect_status mutation we accept.
    # 302 → 301 is one-way (clients cache 301s aggressively, so the
    # promotion fact propagates faster than any undo); a request for
    # 302 on a row that's already 301 is rejected at the route level
    # rather than silently downgrading.
    redirect_status: int | None = None

    @field_validator("redirect_status")
    @classmethod
    def _only_301(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v != 301:
            raise ValueError(
                "redirect_status only accepts 301 (promote). "
                "Demoting a promoted link is not supported."
            )
        return v


class RotateEditTokenResponse(BaseModel):
    # The new plaintext edit token. Returned only at rotation time; the
    # DB stores only its SHA-256 hash. The previous token is no longer
    # valid the instant this response is produced.
    edit_token: str


# --- Auth ------------------------------------------------------------


class MagicLinkRequest(BaseModel):
    email: str


class UserResponse(BaseModel):
    id: int
    email: str
    name: str | None
    provider: str


class MeResponse(BaseModel):
    # Wrapping in an envelope so the response shape stays consistent
    # whether the caller is logged in (user populated) or not (null).
    user: UserResponse | None


# --- My QRs (logged-in user's owned mappings) -----------------------


class QRSummary(BaseModel):
    """Lightweight projection of UrlMapping for list views — same fields
    as QRInfoResponse plus short_url for convenience. Does NOT include
    edit_token / edit_token_hash; owner identity is the auth here."""

    token: str
    short_url: str
    original_url: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    redirect_status: int = 302
    # Soft-delete state. The default `/api/qr/mine` view filters these
    # out; the `?include_deleted=1` projection surfaces them for the
    # Restore flow. `deleted_at` is populated whenever is_deleted is
    # True; older rows that pre-date the column will be None.
    is_deleted: bool = False
    deleted_at: datetime | None = None


class MyQRsResponse(BaseModel):
    items: list[QRSummary]


class BulkDeleteRequest(BaseModel):
    # Caller-supplied tokens to soft-delete. Capped at 100 per call so
    # one request can't churn the audit log unboundedly. Every token
    # must belong to the caller — partial-success isn't allowed; the
    # whole batch rolls back if any token isn't owned.
    tokens: list[str] = Field(min_length=1, max_length=100)


class BulkDeleteResponse(BaseModel):
    deleted: int
    tokens: list[str]


# --- Audit log (owner read of /api/qr/{token}/audit) -----------------


class AuditEntry(BaseModel):
    # Action recorded on the mapping: "create" | "patch_url" |
    # "patch_expires" | "delete" | "rotate_edit_token". Same labels
    # the writer side in routes.py uses; clients should be tolerant
    # of new actions appearing in the future.
    action: str
    before_value: str | None
    after_value: str | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    # Most recent first. Capped server-side at 100 — a full pagination
    # surface is a future enhancement, not needed for the common case
    # of "what happened to this QR recently?".
    items: list[AuditEntry]
