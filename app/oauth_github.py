"""GitHub OAuth login flow.

Two endpoints under /api/auth/github:

  GET /login    -> 302 to GitHub authorize URL with state cookie
  GET /callback -> exchange code for access_token, fetch user,
                   find-or-create User row, create session, redirect
                   home

This intentionally lives in its own module so the auth surface area
stays scannable: `auth_routes.py` is magic-link only, this one is
GitHub-only, and a future Google flow would be `oauth_google.py`.

We deliberately do NOT use `from __future__ import annotations` here
for the same reason auth_routes.py doesn't — stringified annotations
break FastAPI's body/dependency inference when stacked with slowapi
or Depends chains.
"""
from datetime import timedelta
from secrets import token_urlsafe

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import config
from .database import get_db
from .models import User, UserSession

github_router = APIRouter(prefix="/api/auth/github", tags=["auth", "github"])


@github_router.get("/available")
def github_available():
    """Cheap probe so the UI can decide whether to render the GitHub
    sign-in button. When this router isn't registered (no creds),
    this endpoint 404s and the UI keeps the button hidden. When it
    is registered, returns 200 unconditionally.
    """
    return {"enabled": True}

# The CSRF anti-forgery cookie. The same value goes into the `state`
# query parameter sent to GitHub; on callback we require them to match
# so an attacker can't trick a victim into completing a login under
# the attacker's account.
_STATE_COOKIE = "qrs_oauth_state"

# Scope. We only need the user's email to map to the existing
# `users.email` unique key, so we ask for the minimum that gives us
# a usable email value back. `read:user` covers public profile;
# `user:email` lets us fetch the primary verified email even if the
# user has hidden it from their public profile.
_SCOPES = "read:user user:email"

# httpx timeouts — GitHub is normally <300ms, but a cold callback
# under network friction shouldn't hang the request worker forever.
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


@github_router.get("/login")
def github_login(request: Request):
    """Kick off the OAuth flow.

    Sets a short-lived CSRF state cookie, then 302s the browser to
    GitHub's authorize page with `client_id`, `redirect_uri`, `state`,
    and `scope`. The callback validates the `state` round-trip.
    """
    if not config.GITHUB_OAUTH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="GitHub sign-in is not configured on this server.",
        )

    state = token_urlsafe(32)
    redirect_uri = f"{config.BASE_URL}/api/auth/github/callback"

    authorize_url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={config.GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
        f"&scope={_SCOPES.replace(' ', '%20')}"
    )

    response = RedirectResponse(url=authorize_url, status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE,
        value=state,
        max_age=600,  # 10 minutes — plenty for the user to click "Authorize"
        httponly=True,
        samesite="lax",
        secure=config.IS_PRODUCTION,
        path="/api/auth/github",
    )
    return response


@github_router.get("/callback")
def github_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    state_cookie: str | None = Cookie(default=None, alias=_STATE_COOKIE),
    db: Session = Depends(get_db),
):
    """GitHub redirected here after the user authorized. Exchange the
    short-lived `code` for an access token, fetch the user's profile +
    primary email, find-or-create the matching User row, and issue a
    session cookie.
    """
    if not config.GITHUB_OAUTH_ENABLED:
        raise HTTPException(status_code=503, detail="GitHub sign-in not configured.")

    if error:
        # User declined or GitHub rejected; surface the reason.
        raise HTTPException(
            status_code=400,
            detail=f"GitHub OAuth error: {error}: {error_description or ''}",
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    # CSRF check — the state we sent must match what came back.
    if not state_cookie or state_cookie != state:
        raise HTTPException(
            status_code=400,
            detail="OAuth state mismatch — possible CSRF; please try again.",
        )

    # Exchange code for access_token.
    token_resp = httpx.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": config.GITHUB_CLIENT_ID,
            "client_secret": config.GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": f"{config.BASE_URL}/api/auth/github/callback",
        },
        headers={"Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub token exchange failed ({token_resp.status_code}).",
        )
    token_body = token_resp.json()
    access_token = token_body.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub did not return an access_token: {token_body.get('error')}",
        )

    # Fetch the user's profile (gives us numeric id + display name).
    profile_resp = httpx.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_TIMEOUT,
    )
    if profile_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch GitHub profile.")
    profile = profile_resp.json()
    github_id = str(profile["id"])
    name = profile.get("name") or profile.get("login")

    # Fetch the primary verified email. GitHub's /user can return null
    # for email when the user hides it from their public profile, so we
    # always hit /user/emails which returns all addresses on the
    # account.
    emails_resp = httpx.get(
        "https://api.github.com/user/emails",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_TIMEOUT,
    )
    if emails_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch GitHub emails.")

    primary_email = None
    for entry in emails_resp.json():
        if entry.get("primary") and entry.get("verified"):
            primary_email = entry["email"].lower()
            break
    if not primary_email:
        raise HTTPException(
            status_code=400,
            detail="No verified primary email on GitHub account.",
        )

    # Find by provider+id first (preferred — handles email change on
    # GitHub side), then by email (links existing magic-link account
    # to the GitHub identity), then create.
    user = (
        db.query(User)
        .filter(User.provider == "github", User.provider_user_id == github_id)
        .first()
    )
    if user is None:
        user = db.query(User).filter(User.email == primary_email).first()
        if user is not None:
            # Account merge: existing magic-link user signs in via
            # GitHub for the first time. Upgrade their provider link
            # so future logins via either path hit the same row.
            user.provider = "github"
            user.provider_user_id = github_id
            if not user.name and name:
                user.name = name
        else:
            user = User(
                email=primary_email,
                name=name,
                provider="github",
                provider_user_id=github_id,
            )
            db.add(user)
            db.flush()

    # Issue a session.
    from .routes import _now_naive

    session_id = token_urlsafe(32)
    db.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            expires_at=_now_naive() + timedelta(days=config.SESSION_TTL_DAYS),
        )
    )
    db.commit()

    # Build the redirect, clear the state cookie, set the session cookie.
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.delete_cookie(_STATE_COOKIE, path="/api/auth/github")
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
