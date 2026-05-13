"""Pluggable email-send abstraction for magic-link auth.

In dev (`EMAIL_PROVIDER` unset) we route through `ConsoleEmailService`
which prints the magic link to stdout so a developer can copy-click it
without an SMTP setup. In production set `EMAIL_PROVIDER=resend` (or
similar) and wire up a real backend.

The function `get_email_service()` is what routes inject via
FastAPI's `Depends(...)`, which lets tests swap in a capturing fake
through `app.dependency_overrides`.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod

from . import config


class EmailService(ABC):
    @abstractmethod
    def send_magic_link(self, to: str, link_url: str) -> None: ...


class ConsoleEmailService(EmailService):
    """Dev-mode service: prints the link to the server's stdout.

    Output is intentionally noisy (banner lines, flush=True) so a
    developer notices it in a busy uvicorn log and can pick the URL
    out for copy-paste. Not safe for any non-local environment.
    """

    def send_magic_link(self, to: str, link_url: str) -> None:
        print(
            "\n=================== MAGIC LINK ===================\n"
            f"To:    {to}\n"
            f"Link:  {link_url}\n"
            "Open the link in a browser to complete sign-in.\n"
            "==================================================\n",
            file=sys.stdout,
            flush=True,
        )


def _build_default_service() -> EmailService:
    provider = config.EMAIL_PROVIDER
    # Empty / unset / "console" all map to dev mode. Other providers
    # (Resend, SES, SMTP, ...) would branch here once implemented.
    if provider in ("", "console"):
        return ConsoleEmailService()
    raise ValueError(
        f"Unknown EMAIL_PROVIDER={provider!r}; expected one of: '', 'console'"
    )


# Module-level singleton — chosen once at import time from config.
# Tests don't touch this directly; they override via
# `app.dependency_overrides[get_email_service]`.
_default_service: EmailService = _build_default_service()


def get_email_service() -> EmailService:
    """FastAPI dependency. Override in tests via dependency_overrides."""
    return _default_service
