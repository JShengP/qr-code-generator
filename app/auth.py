"""Auth primitives shared across the auth routes and any other endpoint
that needs to know who the caller is.

Right now we expose one thing: `get_current_user`, a FastAPI dependency
that reads the session cookie, validates it against the `user_sessions`
table, and returns the `User` row (or `None` if anonymous).

The session token itself is an opaque 256-bit random string — no JWT,
no signing, no claims. The DB lookup is one indexed-PK SELECT per
request; revocation is one DELETE. We pay one query for a clean
revocation story instead of running a JWT denylist alongside the
signing key.

NOTE: we deliberately don't use `from __future__ import annotations`
here — when this dependency is wrapped in FastAPI's `Depends(...)`
and consumed by an endpoint that's ALSO wrapped by slowapi, the
stringified annotations defeat FastAPI's parameter resolution and
the `user` parameter silently arrives as the literal class instead
of an instance. Same root cause as the auth_routes.py note.
"""
from fastapi import Cookie, Depends
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, UserSession


def get_current_user(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias="qrs_session"),
) -> User | None:
    """Look up the caller's user, or return None if not signed in.

    Treats expired sessions identically to "no session" — we don't
    raise; downstream routes decide whether anonymous access is OK.
    Cleanup of expired rows is deferred to a future maintenance task.
    """
    if not session_token:
        return None

    session = db.query(UserSession).filter(UserSession.id == session_token).first()
    if session is None:
        return None

    # Lazy expiry check — we never auto-delete here, but we treat
    # expired sessions as if absent so the caller sees a logged-out state.
    from .routes import _now_naive  # local import to avoid a cycle

    if session.expires_at < _now_naive():
        return None

    return db.query(User).filter(User.id == session.user_id).first()
