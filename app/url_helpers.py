"""Resolves the public base URL for the current request.

Centralizes the "dev: auto-derive from request; prod: use config"
logic so every user-visible URL we generate — short URLs, QR-encoded
URLs, magic-link sign-in URLs — matches whatever hostname/port the
user actually accessed in their browser.

Without this, a dev who runs uvicorn on :8001 sees short URLs
pointing at :8000 (the hardcoded fallback) and magic links also
pointing at :8000 — neither is reachable. Production keeps the
explicit `BASE_URL` env var because the email link / QR PNG MUST
encode the public domain, not whatever internal hostname uvicorn
is reachable at.
"""
from fastapi import Request

from . import config


def base_url(request: Request) -> str:
    """The base URL (scheme://host:port, no trailing slash) to put in
    any URL we hand back to the user — UI short URLs, QR PNG payload,
    magic-link sign-in emails.

    Falls back to `config.BASE_URL` whenever the env var was set
    (production path) or whenever we're in production mode.
    """
    if config.BASE_URL_AUTO:
        return str(request.base_url).rstrip("/")
    return config.BASE_URL
