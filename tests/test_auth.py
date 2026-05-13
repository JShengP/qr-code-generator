"""Auth tests: magic-link request → verify → session → me → logout.

We intercept the email send via a capturing fake (conftest.email_capture)
so the test can grab the magic link URL without real email
infrastructure. The session cookie is round-tripped naturally because
TestClient stores cookies across requests in the same instance.

NOTE: the default `client` fixture starts pre-authenticated as
test-runner@example.com so that POST /api/qr/create (which now
requires auth) keeps working for the bulk of API tests. Tests in
this file that exercise the sign-in flow itself call
`client.cookies.clear()` at the top to start anonymous.
"""
from urllib.parse import urlparse, parse_qs

import pytest

from app.main import app


def _extract_magic_token(link_url: str) -> str:
    qs = parse_qs(urlparse(link_url).query)
    return qs["token"][0]


def _go_anonymous(client) -> None:
    """Drop the auto-login cookie so the test starts from an anonymous
    state — necessary for tests that exercise the sign-in flow or that
    assert anonymous-specific behavior."""
    client.cookies.clear()


def test_request_link_sends_email_and_returns_vague_response(client, email_capture):
    _go_anonymous(client)
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
    _go_anonymous(client)
    r = client.post("/api/auth/request-link", json={"email": bad_email})
    assert r.status_code == 422
    assert email_capture.sent == []


def test_verify_consumes_link_and_sets_session_cookie(client, email_capture):
    _go_anonymous(client)
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
    _go_anonymous(client)
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
    requests on this client are authenticated as `email`.

    Clears any prior session cookie first (the default `client`
    fixture auto-logs in as test-runner@example.com; tests that want
    to be Alice need to overwrite that)."""
    client.cookies.clear()
    client.post("/api/auth/request-link", json={"email": email})
    token = _extract_magic_token(email_capture.sent[-1][1])
    client.get(f"/api/auth/verify?token={token}", follow_redirects=False)


def test_anonymous_create_returns_401(client):
    """API now requires auth on /api/qr/create."""
    _go_anonymous(client)
    r = client.post("/api/qr/create", json={"url": "https://example.com"})
    assert r.status_code == 401
    assert "sign in" in r.json()["detail"].lower()


def test_my_qrs_empty_when_anonymous(client):
    _go_anonymous(client)
    r = client.get("/api/qr/mine")
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_my_qrs_isolated_between_users(client, email_capture):
    """A user only sees their own QRs, not other users'."""
    # client starts as test-runner. Create a QR they own.
    test_runner_token = client.post(
        "/api/qr/create", json={"url": "https://test-runner.example"}
    ).json()["token"]

    # Switch to Alice
    _login(client, email_capture, "alice@example.com")
    alice_token = client.post(
        "/api/qr/create", json={"url": "https://alice.example"}
    ).json()["token"]

    # Alice's /mine should NOT contain test-runner's token
    items = client.get("/api/qr/mine").json()["items"]
    tokens = {i["token"] for i in items}
    assert alice_token in tokens
    assert test_runner_token not in tokens


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
