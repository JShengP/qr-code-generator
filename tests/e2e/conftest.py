"""Playwright e2e fixtures.

We spawn a real uvicorn subprocess against a temp SQLite file so the
tests exercise the actual rendered DOM + cookie handling. This is
the layer that the in-process pytest suite can't reach — it caught
the recurring "hidden + display: flex" CSS bugs only via manual
browser testing, and that's the exact gap these tests close.

Architecture:

  1. session-scoped `app_server` fixture picks a free port, writes
     a clean temp DB, spawns `uvicorn app.main:app --port <port>`,
     and polls /api/auth/me until ready. Yields (base_url, db_path).
  2. function-scoped `signed_in_page` seeds a User + UserSession
     in that DB directly, injects the cookie onto the Playwright
     browser context, and `page.goto(base_url)` lands on a signed-
     in page. Tests then drive the UI normally.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable


def _free_port() -> int:
    """Reserve a free TCP port by binding then immediately closing."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(url: str, timeout_s: float = 20.0) -> None:
    """Poll until the server starts answering, or raise."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError) as e:
            last_err = e
        time.sleep(0.25)
    raise RuntimeError(
        f"e2e server did not become ready within {timeout_s}s: {last_err!r}"
    )


@pytest.fixture(scope="session")
def app_server(tmp_path_factory):
    """Spawn uvicorn against a clean temp DB. Yields (base_url, db_path)."""
    port = _free_port()
    db_path = tmp_path_factory.mktemp("e2e_db") / "qr_test.db"
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "BASE_URL": base_url,
        # Loosen rate limits so the e2e suite doesn't trip them.
        "CREATE_RATE_LIMIT": "1000/minute",
        "REDIRECT_RATE_LIMIT": "10000/minute",
        "MUTATION_RATE_LIMIT": "1000/minute",
        # No GitHub OAuth in tests
        "GITHUB_CLIENT_ID": "",
        "GITHUB_CLIENT_SECRET": "",
    }

    proc = subprocess.Popen(
        [
            PYTHON,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_ready(f"{base_url}/api/auth/me")
        yield base_url, db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _seed_user_and_session(db_path: Path, email: str = "e2e@example.com") -> str:
    """Insert a User + UserSession directly via SQLAlchemy. Returns the
    session token to be set as a cookie on the Playwright context."""
    # Import lazily so the conftest itself doesn't pull in app.* at
    # collection time (subprocess loads them fresh).
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import User, UserSession

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing is None:
            user = User(email=email, provider="email")
            db.add(user)
            db.flush()
        else:
            user = existing
        session_token = token_urlsafe(32)
        db.add(
            UserSession(
                id=session_token,
                user_id=user.id,
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=1),
            )
        )
        db.commit()
        return session_token
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def signed_in_page(app_server, page):
    """Yield a Playwright `page` with a valid session cookie attached
    and the home page already loaded.

    The browser context is per-test (pytest-playwright default), so
    each test starts from a fresh cookie jar and a freshly seeded
    user session.
    """
    base_url, db_path = app_server
    session_token = _seed_user_and_session(db_path)

    page.context.add_cookies(
        [
            {
                "name": "qrs_session",
                "value": session_token,
                "url": base_url,
            }
        ]
    )
    page.goto(base_url)
    yield page, base_url


@pytest.fixture
def anonymous_page(app_server, page):
    """Yield a Playwright `page` with NO cookie attached. Lands on the
    anonymous-state UI (sign-in prompt, no Create form)."""
    base_url, _ = app_server
    page.context.clear_cookies()
    page.goto(base_url)
    yield page, base_url
