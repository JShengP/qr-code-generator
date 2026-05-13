"""Shared fixtures: isolated in-memory SQLite + cache reset per test.

The production app writes to a file-based SQLite. For tests we override
the `get_db` dependency to point at an in-memory database (one per test
function), and we clear the module-global `redirect_cache` before and
after each test so state can't leak between cases.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import routes as routes_module
from app.database import Base, get_db
from app.limiter import limiter
from app.main import app


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
