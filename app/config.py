"""Single source of truth for env-driven configuration.

Every value here falls back to a hardcoded default that's safe for
local development; set the matching env var to override in any other
environment. Reads happen exactly once, at module import time — there
is no runtime watch or reload. Tests that need a different value
monkey-patch the module-level constant in `app.routes` (or `app.main`,
etc.) where the value is actually consumed.

The conventional set of overrides:

  DEPLOY_ENV               dev | production           ("dev")
  DATABASE_URL             SQLAlchemy URL            (sqlite:///./qr_code.db)
  BASE_URL                 public URL for QR codes   (http://localhost:8000)

  CREATE_RATE_LIMIT        slowapi expression        ("10/minute")
  REDIRECT_RATE_LIMIT      slowapi expression        ("300/minute")
  MUTATION_RATE_LIMIT      slowapi expression        ("30/minute")
  RATE_LIMIT_STORAGE_URI   limits storage backend    ("memory://")

  SCAN_DEDUP_WINDOW        float seconds             (1.0)
  SCAN_FLUSH_BATCH_SIZE    int                       (10)
  SCAN_FLUSH_INTERVAL      float seconds             (5.0)
"""
from __future__ import annotations

import os
from pathlib import Path

# Auto-load `.env` from the project root (sibling of `app/`) before any
# os.getenv() calls below see their defaults. Means a dev doesn't have
# to re-export `$env:GITHUB_CLIENT_ID = ...` etc every time they open
# a new PowerShell session — just keep the secrets in `.env` (which
# .gitignore covers) and they get picked up automatically.
#
# We pass an explicit path rather than relying on `find_dotenv()` so
# `pytest` run from any cwd resolves to the same file. `override=False`
# (the default) means real env vars still win, so CI / production with
# explicit `BASE_URL=...` etc isn't shadowed by a stray local .env.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    # python-dotenv is a soft dependency: in environments where the
    # user truly only relies on real env vars (Docker, CI), skipping
    # the load is fine. requirements.txt does pin it, so this branch
    # is only hit if someone runs from a stripped-down image.
    pass

# --- Deployment / environment ------------------------------------------
DEPLOY_ENV: str = os.getenv("DEPLOY_ENV", "").lower()
IS_PRODUCTION: bool = DEPLOY_ENV == "production"

# --- Database -----------------------------------------------------------
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./qr_code.db")

# --- Public-facing URL --------------------------------------------------
# Used to construct `short_url` and `qr_code_url` in API responses, and
# what the QR-code PNG actually encodes.
#
# Two-mode resolution:
#   - Env var set (any environment): use that value verbatim. Required
#     in production so the QR encodes the public domain, not whatever
#     internal hostname uvicorn happens to be reachable at.
#   - Env var unset AND not production: routes auto-derive from the
#     incoming `request.base_url` (handled in app.routes._base_url).
#     Means the dev short URL always matches the hostname/port the
#     user typed in their browser — no more "Short URL says :8000 but
#     my uvicorn is on :8001" foot-gun when iterating.
_BASE_URL_RAW: str | None = os.getenv("BASE_URL")
BASE_URL: str = _BASE_URL_RAW or "http://localhost:8000"
BASE_URL_AUTO: bool = _BASE_URL_RAW is None and not IS_PRODUCTION

# --- slowapi rate limits ------------------------------------------------
CREATE_RATE_LIMIT: str = os.getenv("CREATE_RATE_LIMIT", "10/minute")
REDIRECT_RATE_LIMIT: str = os.getenv("REDIRECT_RATE_LIMIT", "300/minute")
MUTATION_RATE_LIMIT: str = os.getenv("MUTATION_RATE_LIMIT", "30/minute")
# limits-style URI for slowapi's storage backend. `memory://` is the
# default and is per-process; set to `redis://host:6379/0` for a
# shared bucket across uvicorn workers.
RATE_LIMIT_STORAGE_URI: str = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

# --- Scan event tuning --------------------------------------------------
SCAN_DEDUP_WINDOW: float = float(os.getenv("SCAN_DEDUP_WINDOW", "1.0"))
SCAN_FLUSH_BATCH_SIZE: int = int(os.getenv("SCAN_FLUSH_BATCH_SIZE", "10"))
SCAN_FLUSH_INTERVAL: float = float(os.getenv("SCAN_FLUSH_INTERVAL", "5.0"))

# --- Auth ---------------------------------------------------------------
# Cookie name for the opaque session token. We don't use a JWT — the
# session ID is a 256-bit random string stored in the `user_sessions`
# table; revocation is just `DELETE FROM user_sessions WHERE id = ...`.
COOKIE_NAME: str = os.getenv("SESSION_COOKIE_NAME", "qrs_session")
SESSION_TTL_DAYS: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
MAGIC_LINK_TTL_MINUTES: int = int(os.getenv("MAGIC_LINK_TTL_MINUTES", "15"))
# Rate limit on the request-link endpoint to keep one IP from spamming
# someone else's inbox.
AUTH_REQUEST_RATE_LIMIT: str = os.getenv("AUTH_REQUEST_RATE_LIMIT", "3/minute")

# Email provider selection. Empty value => ConsoleEmailService (dev
# mode, prints link to stdout). Future: "resend" / "smtp" / etc.
EMAIL_PROVIDER: str = os.getenv("EMAIL_PROVIDER", "").lower()
EMAIL_FROM: str = os.getenv("EMAIL_FROM", "noreply@localhost")

# --- OAuth providers ---------------------------------------------------
# GitHub OAuth app credentials. The app registration lives at
# https://github.com/settings/developers; the callback URL there MUST
# match `{BASE_URL}/api/auth/github/callback` exactly. Both values stay
# in env vars — never committed.
GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_OAUTH_ENABLED: bool = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)
