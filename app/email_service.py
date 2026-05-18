"""Pluggable email-send abstraction for magic-link auth.

In dev (`EMAIL_PROVIDER` unset) we route through `ConsoleEmailService`
which prints the magic link to stdout so a developer can copy-click it
without an SMTP setup. In production set `EMAIL_PROVIDER=resend` and
provide `RESEND_API_KEY` + `EMAIL_FROM`.

The function `get_email_service()` is what routes inject via
FastAPI's `Depends(...)`, which lets tests swap in a capturing fake
through `app.dependency_overrides`.
"""
from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod

import httpx

from . import config

logger = logging.getLogger(__name__)


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


class ResendEmailService(EmailService):
    """Transactional email via Resend (https://resend.com).

    Free tier: 3000 emails/month, 100/day. Use the test-only sender
    `onboarding@resend.dev` when you haven't verified a domain yet —
    legitimate but lands in spam more often than verified senders.
    Verify a domain in the Resend dashboard once you have one.

    Failures here intentionally raise. The auth endpoint catches the
    exception and returns a generic "if that email exists, we sent
    something" — so a downstream API consumer can't tell whether the
    address was wrong, the API key was wrong, or Resend itself is
    having a bad day. The full error is logged for the operator.
    """

    _ENDPOINT = "https://api.resend.com/emails"
    _TIMEOUT = 10.0

    def __init__(self, api_key: str, from_address: str) -> None:
        self.api_key = api_key
        self.from_address = from_address

    def send_magic_link(self, to: str, link_url: str) -> None:
        text_body = (
            "Click the link below to sign in to QR Code Generator:\n\n"
            f"{link_url}\n\n"
            "This link expires in 15 minutes and can only be used once.\n"
            "If you didn't request this, you can safely ignore this email."
        )
        try:
            resp = httpx.post(
                self._ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": self.from_address,
                    "to": [to],
                    "subject": "Your QR Code Generator sign-in link",
                    "text": text_body,
                },
                timeout=self._TIMEOUT,
            )
        except httpx.HTTPError as e:
            logger.error("Resend send failed: transport error: %s", e)
            raise RuntimeError(f"Email send failed (transport): {e}") from e

        if resp.status_code >= 400:
            # Don't leak `to` into logs — addresses are PII.
            logger.error(
                "Resend send failed: HTTP %s body=%r",
                resp.status_code,
                resp.text[:500],
            )
            raise RuntimeError(
                f"Email send failed: Resend returned {resp.status_code}"
            )


def _build_default_service() -> EmailService:
    provider = config.EMAIL_PROVIDER
    # Empty / unset / "console" all map to dev mode.
    if provider in ("", "console"):
        return ConsoleEmailService()
    if provider == "resend":
        if not config.RESEND_API_KEY:
            raise RuntimeError(
                "EMAIL_PROVIDER=resend but RESEND_API_KEY is not set. "
                "Set it via .env (dev) or the platform's secret store (prod)."
            )
        return ResendEmailService(
            api_key=config.RESEND_API_KEY,
            from_address=config.EMAIL_FROM,
        )
    raise ValueError(
        f"Unknown EMAIL_PROVIDER={provider!r}; expected one of: '', 'console', 'resend'"
    )


# Module-level singleton — chosen once at import time from config.
# Tests don't touch this directly; they override via
# `app.dependency_overrides[get_email_service]`.
_default_service: EmailService = _build_default_service()


def get_email_service() -> EmailService:
    """FastAPI dependency. Override in tests via dependency_overrides."""
    return _default_service
