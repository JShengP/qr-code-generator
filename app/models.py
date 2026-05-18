from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _utc_now_naive() -> datetime:
    """Naive UTC `now` for column defaults.

    `datetime.utcnow` is deprecated in Python 3.12+. We keep the value
    naive because the columns are declared without `timezone=True`; the
    application code in routes.py uses the same convention so all
    comparisons stay on the naive side of the line.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UrlMapping(Base):
    __tablename__ = "url_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(8), unique=True, nullable=False, index=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex digest of the edit_token returned at creation time.
    # Stored hashed so an attacker who compromises the DB can't act as
    # the owner of every existing short link. Nullable so legacy rows
    # created before this column existed remain readable; they're
    # treated as un-editable.
    edit_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # FK to the User who created this mapping, if any. Nullable so the
    # original anonymous create flow keeps working: a logged-out user
    # still gets a token + edit_token and can edit via the bearer header,
    # they just don't have a "My QRs" list. Logged-in users get both.
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utc_now_naive, onupdate=_utc_now_naive
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Soft-delete bookkeeping: when the row was marked deleted, and which
    # user did it. Restore clears `deleted_at` (and is_deleted) and the
    # event lands in audit_logs the same way delete does.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 302 (default) keeps every scan observable. Owners can promote to
    # 301 once they're confident the destination is permanent — this is
    # a ONE-WAY trip because clients cache 301s aggressively and the
    # promotion fact propagates faster than any "undo". Modelled as an
    # int (not a bool flag) so future status codes can slot in without
    # a column rename.
    redirect_status: Mapped[int] = mapped_column(Integer, default=302, nullable=False)


class ScanEvent(Base):
    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(8), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (Index("idx_token_scanned", "token", "scanned_at"),)


class User(Base):
    """An identity. Created the first time someone successfully verifies
    a magic link (or, later, completes an OAuth callback).

    We never store passwords — auth is purely via magic link or OAuth.
    `provider` records which path created the account; the same email
    going through two providers will produce the same user record
    because of the unique constraint on `email`.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # "email" for magic-link signups; "github" / "google" once OAuth lands.
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    # Provider's user ID (GitHub numeric ID, Google sub, etc.). NULL for email.
    provider_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)


class UserSession(Base):
    """A logged-in session. The `id` is an opaque 256-bit random token
    stored in a cookie; the row lives in the DB so logout is one DELETE
    and so we can list / revoke sessions per user later.

    Named UserSession (not Session) to avoid shadowing
    `sqlalchemy.orm.Session` everywhere else in the codebase.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)


class AuditLog(Base):
    """A row per mutating action on a UrlMapping.

    Captures every create / patch_url / patch_expires / delete /
    rotate_edit_token so the system has a forensic trail and an
    "undo from history" path even though the live row only carries
    the current state. Soft-deleted mappings keep their audit history
    indefinitely.

    `before_value` / `after_value` are loose strings so the same
    schema covers URL strings, ISO datetimes, and "rotated" sentinels
    without a polymorphic column.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("url_mappings.id"), nullable=False, index=True
    )
    # Nullable because a future bearer-only action (no session, no
    # user_id resolvable from cookie) still gets logged with whatever
    # we know — but `bearer` ownership is acted on the mapping's
    # owner_id, which we can also resolve and stash here. For now
    # we just log `user_id = current session's user.id if any`.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    # One of: "create", "patch_url", "patch_expires", "delete",
    # "rotate_edit_token". Kept as a free-text column on purpose so
    # adding new action types doesn't need a migration.
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)


class MagicLink(Base):
    """A pending login. Created when someone hits /api/auth/request-link;
    consumed when they click the emailed link. Single-use: once
    `consumed_at` is set, re-using the same token returns 400.

    The `email` is captured at request time rather than dereferencing
    a user FK so the link can pre-register an account: the verify
    handler finds-or-creates the User by email at consumption time.
    """

    __tablename__ = "magic_links"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
