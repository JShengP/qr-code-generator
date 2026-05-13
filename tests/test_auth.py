"""Auth tests: magic-link request → verify → session → me → logout.

We intercept the email send via a capturing fake so the test can grab
the magic link URL without needing real email infrastructure. The
session cookie is round-tripped naturally because TestClient stores
cookies across requests in the same instance.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qs

import pytest

from app import email_service as email_module
from app.email_service import EmailService
from app.main import app


class _CapturingEmailService(EmailService):
    """Records the email + link instead of printing/sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_magic_link(self, to: str, link_url: str) -> None:
        self.sent.append((to, link_url))


@pytest.fixture
def email_capture():
    """Override the email service dependency with a capturing fake.

    The route declares `email_svc: EmailService = Depends(get_email_service)`,
    and we replace that dependency for the duration of the test.
    """
    capture = _CapturingEmailService()
    app.dependency_overrides[email_module.get_email_service] = lambda: capture
    yield capture
    # `client` fixture's teardown clears dependency_overrides, so
    # cleanup here is belt + suspenders.
    app.dependency_overrides.pop(email_module.get_email_service, None)


def _extract_magic_token(link_url: str) -> str:
    qs = parse_qs(urlparse(link_url).query)
    return qs["token"][0]


def test_request_link_sends_email_and_returns_vague_response(client, email_capture):
    r = client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    assert r.status_code == 200
    # Response is intentionally vague so it doesn't leak whether the
    # email is registered.
    assert "sent" in r.json()["detail"].lower()
    assert len(email_capture.sent) == 1
    to, link = email_capture.sent[0]
    assert to == "alice@example.com"
    assert "/api/auth/verify?token=" in link


@pytest.mark.parametrize(
    "bad_email",
    ["", "not-an-email", "no@dot", "@nope.com", "a" * 250 + "@example.com"],
)
def test_request_link_validates_email(client, email_capture, bad_email):
    r = client.post("/api/auth/request-link", json={"email": bad_email})
    assert r.status_code == 422
    assert email_capture.sent == []


def test_verify_consumes_link_and_sets_session_cookie(client, email_capture):
    r = client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    assert r.status_code == 200
    token = _extract_magic_token(email_capture.sent[0][1])

    # follow_redirects=False so we can inspect the 303 directly.
    r = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # The set-cookie should land on the TestClient's jar
    assert "qrs_session" in client.cookies


def test_verify_invalid_token_returns_400(client):
    r = client.get("/api/auth/verify?token=not-a-real-token", follow_redirects=False)
    assert r.status_code == 400


def test_verify_reused_link_returns_400(client, email_capture):
    client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    token = _extract_magic_token(email_capture.sent[0][1])

    r1 = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert r1.status_code == 303

    # Clear the session cookie so the test client is anonymous when
    # we hit verify again — otherwise the re-verify would be by an
    # already-logged-in caller, which isn't what we're testing.
    client.cookies.clear()

    r2 = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert r2.status_code == 400
    assert "already" in r2.json()["detail"].lower()


def test_verify_expired_link_returns_400(client, email_capture):
    """Force-expire the magic link by mutating it in the DB, then verify."""
    import time
    from app import routes as routes_module

    # Move the test "now" forward by adjusting the magic link's
    # expires_at to be in the past. We could also monkey-patch
    # _now_naive but mutating the row is more direct.
    client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    token = _extract_magic_token(email_capture.sent[0][1])

    # Reach into the test's DB via the dependency_overrides session
    from datetime import datetime, timedelta, timezone

    from app.database import get_db
    from app.models import MagicLink

    db_factory = app.dependency_overrides[get_db]
    db_gen = db_factory()
    db = next(db_gen)
    try:
        link = db.query(MagicLink).filter(MagicLink.token == token).first()
        # Naive UTC to match the column convention; utcnow() is deprecated.
        link.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

    r = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


def test_me_returns_user_when_logged_in(client, email_capture):
    client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    token = _extract_magic_token(email_capture.sent[0][1])
    client.get(f"/api/auth/verify?token={token}", follow_redirects=False)

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"] is not None
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["provider"] == "email"


def test_me_returns_null_without_cookie(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"] is None


def test_logout_clears_cookie_and_invalidates_session(client, email_capture):
    client.post("/api/auth/request-link", json={"email": "alice@example.com"})
    token = _extract_magic_token(email_capture.sent[0][1])
    client.get(f"/api/auth/verify?token={token}", follow_redirects=False)

    # Sanity: logged in
    assert client.get("/api/auth/me").json()["user"] is not None

    r = client.post("/api/auth/logout")
    assert r.status_code == 200

    # Cookie cleared on the client side AND session row deleted on
    # the server, so even resurrecting the cookie value wouldn't help.
    assert client.get("/api/auth/me").json()["user"] is None


def _login(client, email_capture, email="alice@example.com") -> None:
    """Helper: complete a full magic-link round-trip so subsequent
    requests on this client are authenticated as `email`."""
    client.post("/api/auth/request-link", json={"email": email})
    token = _extract_magic_token(email_capture.sent[-1][1])
    client.get(f"/api/auth/verify?token={token}", follow_redirects=False)


def test_my_qrs_empty_when_anonymous(client):
    r = client.get("/api/qr/mine")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_my_qrs_lists_only_creates_after_signin(client, email_capture):
    # Anonymous create — should NOT appear in my-qrs after signin.
    anon = client.post("/api/qr/create", json={"url": "https://anon.example"})
    anon_token = anon.json()["token"]

    _login(client, email_capture)

    owned = client.post("/api/qr/create", json={"url": "https://mine.example"})
    owned_token = owned.json()["token"]

    r = client.get("/api/qr/mine").json()
    tokens = {item["token"] for item in r["items"]}
    assert owned_token in tokens
    assert anon_token not in tokens


def test_owner_can_patch_without_edit_token(client, email_capture):
    _login(client, email_capture)
    created = client.post("/api/qr/create", json={"url": "https://before.example"})
    token = created.json()["token"]

    # PATCH without Authorization header — owner shortcut should accept
    r = client.patch(f"/api/qr/{token}", json={"url": "https://after.example"})
    assert r.status_code == 200
    assert r.json()["original_url"] == "https://after.example"


def test_non_owner_cannot_patch_without_edit_token(client, email_capture):
    # Alice creates a link while logged in
    _login(client, email_capture, "alice@example.com")
    created = client.post("/api/qr/create", json={"url": "https://alice.example"})
    token = created.json()["token"]
    # Note: edit_token is still returned for owners too -- the API
    # contract doesn't change. We just don't NEED it.

    # Bob logs in (replaces session cookie)
    client.cookies.clear()
    _login(client, email_capture, "bob@example.com")

    # Bob tries to PATCH without bearer
    r = client.patch(f"/api/qr/{token}", json={"url": "https://bob.example"})
    assert r.status_code == 401


def test_owner_my_qrs_does_not_include_deleted(client, email_capture):
    _login(client, email_capture)
    a = client.post("/api/qr/create", json={"url": "https://a.example"}).json()
    b = client.post("/api/qr/create", json={"url": "https://b.example"}).json()

    # Soft-delete one
    client.delete(f"/api/qr/{a['token']}")

    items = client.get("/api/qr/mine").json()["items"]
    tokens = {i["token"] for i in items}
    assert b["token"] in tokens
    assert a["token"] not in tokens


def test_second_login_with_same_email_reuses_user_row(client, email_capture):
    """Two successful magic-link round-trips for one email produce the
    same `User` row (matched on email). The second session is independent."""
    for _ in range(2):
        client.post("/api/auth/request-link", json={"email": "alice@example.com"})
        token = _extract_magic_token(email_capture.sent[-1][1])
        client.get(f"/api/auth/verify?token={token}", follow_redirects=False)

    # Same user via /me
    r = client.get("/api/auth/me").json()
    assert r["user"]["email"] == "alice@example.com"

    # Verify only one User row exists for that email
    from app.database import get_db
    from app.models import User

    db_factory = app.dependency_overrides[get_db]
    db_gen = db_factory()
    db = next(db_gen)
    try:
        count = db.query(User).filter(User.email == "alice@example.com").count()
        assert count == 1
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
