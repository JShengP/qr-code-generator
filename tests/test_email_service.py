"""Unit tests for the email-service factory + ResendEmailService.

ResendEmailService talks to a real HTTP API; we monkey-patch
`httpx.post` so the tests don't actually hit Resend during pytest.
"""
import pytest

from app import email_service as email_module
from app.email_service import (
    ConsoleEmailService,
    ResendEmailService,
    _build_default_service,
)


def test_default_service_when_provider_unset_is_console(monkeypatch):
    monkeypatch.setattr(email_module.config, "EMAIL_PROVIDER", "")
    assert isinstance(_build_default_service(), ConsoleEmailService)


def test_default_service_explicit_console(monkeypatch):
    monkeypatch.setattr(email_module.config, "EMAIL_PROVIDER", "console")
    assert isinstance(_build_default_service(), ConsoleEmailService)


def test_default_service_resend_requires_api_key(monkeypatch):
    monkeypatch.setattr(email_module.config, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(email_module.config, "RESEND_API_KEY", "")
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        _build_default_service()


def test_default_service_resend_constructs_with_api_key(monkeypatch):
    monkeypatch.setattr(email_module.config, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(email_module.config, "RESEND_API_KEY", "re_test_123")
    monkeypatch.setattr(email_module.config, "EMAIL_FROM", "noreply@example.com")
    svc = _build_default_service()
    assert isinstance(svc, ResendEmailService)
    assert svc.api_key == "re_test_123"
    assert svc.from_address == "noreply@example.com"


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setattr(email_module.config, "EMAIL_PROVIDER", "smtp")
    with pytest.raises(ValueError, match="Unknown EMAIL_PROVIDER"):
        _build_default_service()


def test_resend_send_posts_to_correct_endpoint_with_bearer(monkeypatch):
    """Lock the request shape — anyone porting to a different Resend
    SDK version or copy-pasting curl examples needs to know the wire
    format matches what the Resend docs specify."""
    captured = {}

    class _FakeResp:
        status_code = 200
        text = '{"id":"abc"}'

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(email_module.httpx, "post", _fake_post)

    svc = ResendEmailService(api_key="re_test_123", from_address="noreply@example.com")
    svc.send_magic_link("user@example.com", "https://example.com/verify?token=xyz")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"]["from"] == "noreply@example.com"
    assert captured["json"]["to"] == ["user@example.com"]
    assert "sign-in" in captured["json"]["subject"].lower()
    assert "https://example.com/verify?token=xyz" in captured["json"]["text"]
    assert "15 minutes" in captured["json"]["text"]


def test_resend_send_raises_on_4xx(monkeypatch):
    """Resend returns 4xx for bad auth / invalid sender. The service
    must raise so the auth route's vague-success copy doesn't leak
    that the send actually failed — but the operator sees it in logs."""

    class _Resp:
        status_code = 401
        text = '{"message":"Invalid API key"}'

    monkeypatch.setattr(
        email_module.httpx, "post", lambda *args, **kwargs: _Resp()
    )

    svc = ResendEmailService(api_key="re_bad", from_address="x@y.com")
    with pytest.raises(RuntimeError, match="401"):
        svc.send_magic_link("user@example.com", "https://e.com/v?t=x")


def test_resend_send_raises_on_transport_error(monkeypatch):
    def _boom(*args, **kwargs):
        raise email_module.httpx.ConnectError("DNS lookup failed")

    monkeypatch.setattr(email_module.httpx, "post", _boom)

    svc = ResendEmailService(api_key="re_test", from_address="x@y.com")
    with pytest.raises(RuntimeError, match="transport"):
        svc.send_magic_link("user@example.com", "https://e.com/v?t=x")
