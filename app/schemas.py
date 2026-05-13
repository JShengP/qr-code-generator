from datetime import datetime

from pydantic import BaseModel


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


class UpdateRequest(BaseModel):
    url: str | None = None
    expires_at: datetime | None = None


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


class MyQRsResponse(BaseModel):
    items: list[QRSummary]
