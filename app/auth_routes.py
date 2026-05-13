"""Magic-link auth endpoints.

Routes mounted under `/api/auth`:

    POST /api/auth/request-link    — generate a magic link, "send" it
    GET  /api/auth/verify          — consume the link, set session cookie
    GET  /api/auth/me              — current user or null
    POST /api/auth/logout          — delete session, clear cookie

GitHub OAuth (Phase E) will plug into the same `User` / `UserSession`
tables — see DECISIONS.md "User identity layer" for the full plan.

We deliberately do NOT use `from __future__ import annotations` in
this module: that turns every annotation into a string, which under
slowapi's `functools.wraps` shim defeats FastAPI's body-vs-query
inference and makes a `BaseModel` body parameter look like a missing
query string. Keep annotations live here.
"""
import re
from datetime import timedelta
from secrets import token_urlsafe

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import config
from .auth import get_current_user
from .database import get_db
from .email_service import EmailService, get_email_service
from .limiter import limiter
from .models import MagicLink, User, UserSession
from .schemas import MagicLinkRequest, MeResponse, UserResponse

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

# RFC 5322 is a deep rabbit hole; this is the conventional "good enough"
# regex for an interactive sign-up form. The actual validity test is
# whether the email service can deliver to it.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(raw: str) -> str:
    cleaned = raw.strip().lower()
    if len(cleaned) == 0 or len(cleaned) > 254:
        raise HTTPException(status_code=422, detail="Email is required (max 254 chars)")
    if not _EMAIL_RE.match(cleaned):
        raise HTTPException(status_code=422, detail="Email format is invalid")
    return cleaned


def _auth_request_rate_limit() -> str:
    # Callable so tests can monkey-patch config without re-importing.
    return config.AUTH_REQUEST_RATE_LIMIT


@auth_router.post("/request-link")
@limiter.limit(_auth_request_rate_limit)
def request_magic_link(
    request: Request,
    req: MagicLinkRequest,
    db: Session = Depends(get_db),
    email_svc: EmailService = Depends(get_email_service),
):
    """Generate a one-time login link for `req.email` and send it.

    The response is intentionally vague — we say "if the email exists,
    we sent something" whether or not anyone is registered, to keep
    the endpoint from doubling as an account-enumeration oracle.
    """
    email = _validate_email(req.email)

    from .routes import _now_naive  # local import dodges a cycle

    magic_token = token_urlsafe(32)
    expires_at = _now_naive() + timedelta(minutes=config.MAGIC_LINK_TTL_MINUTES)

    db.add(MagicLink(token=magic_token, email=email, expires_at=expires_at))
    db.commit()

    link_url = f"{config.BASE_URL}/api/auth/verify?token={magic_token}"
    email_svc.send_magic_link(email, link_url)

    return {"detail": "If that email exists, a sign-in link has been sent."}


@auth_router.get("/verify")
def verify_magic_link(
    token: str,
    db: Session = Depends(get_db),
):
    """Consume a magic link: validate, find-or-create the user, set cookie.

    Returns a 303 to `/` so the user lands on the app with the cookie
    set. Single-use — re-clicking the same link after consumption gets
    a 400 with the canonical "already used" message.
    """
    from .routes import _now_naive

    link = db.query(MagicLink).filter(MagicLink.token == token).first()
    if link is None:
        raise HTTPException(status_code=400, detail="Invalid sign-in link")
    if link.consumed_at is not None:
        raise HTTPException(status_code=400, detail="Sign-in link has already been used")
    if link.expires_at < _now_naive():
        raise HTTPException(status_code=400, detail="Sign-in link has expired")

    link.consumed_at = _now_naive()

    user = db.query(User).filter(User.email == link.email).first()
    if user is None:
        user = User(email=link.email, provider="email")
        db.add(user)
        db.flush()  # need user.id before the session insert

    session_id = token_urlsafe(32)
    expires_at = _now_naive() + timedelta(days=config.SESSION_TTL_DAYS)
    db.add(UserSession(id=session_id, user_id=user.id, expires_at=expires_at))
    db.commit()

    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(
        key=config.COOKIE_NAME,
        value=session_id,
        max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.IS_PRODUCTION,
        path="/",
    )
    return redirect


@auth_router.get("/me", response_model=MeResponse)
def me(user: User | None = Depends(get_current_user)):
    if user is None:
        return MeResponse(user=None)
    return MeResponse(
        user=UserResponse(id=user.id, email=user.email, name=user.name, provider=user.provider)
    )


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Delete the server-side session row and clear the client cookie.

    Always returns 200 even if no session was present — caller doesn't
    need to know whether they were logged in. The client-side cleanup
    (clear cookie) runs unconditionally.
    """
    session_id = request.cookies.get(config.COOKIE_NAME)
    if session_id:
        db.query(UserSession).filter(UserSession.id == session_id).delete()
        db.commit()
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"detail": "Logged out"}
