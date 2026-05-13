"""Shared fixtures: isolated in-memory SQLite + cache reset per test.

The production app writes to a file-based SQLite. For tests we override
the `get_db` dependency to point at an in-memory database (one per test
function), and we clear the module-global `redirect_cache` before and
after each test so state can't leak between cases.

`POST /api/qr/create` now requires an authenticated caller. The default
`client` fixture pre-creates a `test-runner@example.com` user + active
session row and sets the cookie on the TestClient, so existing tests
that POST /create keep working without rewriting. Tests that need to
exercise anonymous behavior call `client.cookies.clear()` explicitly.
"""
import time
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import email_service as email_module
from app import routes as routes_module
from app.database import Base, get_db
from app.email_service import EmailService
from app.limiter import limiter
from app.main import app
from app.models import User, UserSession


class CapturingEmailService(EmailService):
    """In-test stand-in for ConsoleEmailService.

    Records each `(to, link_url)` pair so a test can pull the magic
    link out without parsing stdout. Reset on every fixture set-up so
    tests don't leak state.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_magic_link(self, to: str, link_url: str) -> None:
        self.sent.append((to, link_url))


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _auto_login_via_verify(
    client: TestClient,
    TestingSession: sessionmaker,
    email: str = "test-runner@example.com",
) -> None:
    """Pre-authenticate `client` by inserting a MagicLink and then
    hitting `/api/auth/verify`. The server's Set-Cookie response is
    what lands in the TestClient's cookie jar, with the exact
    (name, domain, path) the production routes use.

    We deliberately do NOT shortcut by writing the session row and
    calling `client.cookies.set(...)` directly: httpx is strict about
    cookie domain matching and a manually-set cookie with
    `domain="testserver"` doesn't get sent on subsequent requests
    (the actual `testserver` host doesn't satisfy httpx's matcher
    for explicit-domain cookies). Doing it through the live route
    ensures the cookie is stored exactly as a real browser would
    see it, and a later `/api/auth/logout` `delete_cookie` clears
    it cleanly without leaving a phantom entry behind.
    """
    from app.models import MagicLink

    db = TestingSession()
    try:
        magic_token = token_urlsafe(32)
        db.add(
            MagicLink(
                token=magic_token,
                email=email,
                expires_at=_utc_now_naive() + timedelta(hours=1),
            )
        )
        db.commit()
    finally:
        db.close()

    # Real Set-Cookie round-trip — cookie lands in the jar with the
    # right domain attribute for the test host.
    client.get(f"/api/auth/verify?token={magic_token}", follow_redirects=False)


@pytest.fixture(scope="function")
def client():
    # StaticPool keeps a single connection alive so :memory: persists
    # for the whole test (multiple requests share the same DB).
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False)

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    routes_module.redirect_cache.clear()
    routes_module._scan_last_seen.clear()
    routes_module._pending_scans.clear()
    routes_module._last_flush_time = time.monotonic()
    # Disable per-(token, ip) scan dedup for the default fixture so that
    # tests which hit `/r/{token}` multiple times in a tight loop see
    # every scan counted. The dedicated dedup test re-enables it.
    routes_module.SCAN_DEDUP_WINDOW = 0.0
    # Force every scan to flush immediately so existing tests that don't
    # explicitly hit /analytics still see scan_events in the DB. The
    # dedicated batching test raises this back up.
    routes_module.SCAN_FLUSH_BATCH_SIZE = 1
    routes_module.SCAN_FLUSH_INTERVAL = 0.0

    # Disable rate limiting for the bulk of tests — most of them post
    # many URLs in a tight loop and would otherwise trip the bucket.
    # The dedicated rate-limit test re-enables it locally.
    limiter.enabled = False

    # NOTE: TestClient is intentionally NOT used as a context manager
    # here. The `with` form fires the ASGI lifespan, which calls
    # Base.metadata.create_all() on the production file engine and
    # writes qr_code.db to disk on every test run. We don't need
    # lifespan in tests — the schema is already created on the
    # in-memory engine above, and limiter/router registration happens
    # at module import.
    client = TestClient(app)
    # Pre-authenticate the test client so the bulk of API tests don't
    # need an explicit sign-in step. The cookie is set via a real
    # /api/auth/verify round-trip (see _auto_login_via_verify) so it
    # lives in the jar with the same (name, domain, path) the prod
    # auth routes use — letting a later logout/clear actually clear it.
    _auto_login_via_verify(client, TestingSession)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()
        routes_module.redirect_cache.clear()
        routes_module._scan_last_seen.clear()
        routes_module._pending_scans.clear()
        routes_module._last_flush_time = time.monotonic()
        routes_module.SCAN_DEDUP_WINDOW = 1.0  # restore prod default
        routes_module.SCAN_FLUSH_BATCH_SIZE = 10  # restore prod default
        routes_module.SCAN_FLUSH_INTERVAL = 5.0   # restore prod default
        routes_module.REDIRECT_RATE_LIMIT = "300/minute"  # restore prod default
        routes_module.MUTATION_RATE_LIMIT = "30/minute"   # restore prod default
        limiter.enabled = True
        limiter.reset()
        engine.dispose()


@pytest.fixture(scope="function")
def rate_limited_client(client):
    """Same fixture as `client`, but with the limiter switched back on
    and its state reset before the test. Use this in tests that want
    to verify 429 behavior."""
    limiter.enabled = True
    limiter.reset()
    yield client


@pytest.fixture(scope="function")
def email_capture():
    """Override the email-service dependency with a capturing fake.

    Lives in conftest (not test_auth.py) so any test file can use it
    without re-defining the capture class.
    """
    capture = CapturingEmailService()
    app.dependency_overrides[email_module.get_email_service] = lambda: capture
    yield capture
    app.dependency_overrides.pop(email_module.get_email_service, None)
