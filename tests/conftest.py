"""Shared fixtures: isolated in-memory SQLite + cache reset per test.

The production app writes to a file-based SQLite. For tests we override
the `get_db` dependency to point at an in-memory database (one per test
function), and we clear the module-global `redirect_cache` before and
after each test so state can't leak between cases.
"""
from __future__ import annotations

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

    # Disable rate limiting for the bulk of tests — most of them post
    # many URLs in a tight loop and would otherwise trip the bucket.
    # The dedicated rate-limit test re-enables it locally.
    limiter.enabled = False

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    routes_module.redirect_cache.clear()
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
