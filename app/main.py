from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .auth_routes import auth_router
from .config import IS_PRODUCTION
from .database import Base, engine
from .limiter import limiter
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create schema on startup; drain the scan-event buffer on shutdown.

    Moved out of module-import scope so that simply importing `app.main`
    in a test runner doesn't write `qr_code.db` to disk. The test fixture
    in `tests/conftest.py` builds its own in-memory engine and creates
    schema on it directly, and intentionally does NOT use TestClient as
    a context manager so this lifespan never fires under pytest.

    On shutdown we drain `_pending_scans` so a graceful uvicorn stop
    (SIGTERM under most process managers) commits any buffered rows
    instead of dropping them.
    """
    Base.metadata.create_all(bind=engine)
    yield
    # Shutdown: flush any buffered scan events using a fresh session,
    # since per-request sessions are already torn down here.
    from sqlalchemy.orm import Session

    from .routes import force_flush_pending_scans

    with Session(engine) as db:
        force_flush_pending_scans(db)


app = FastAPI(
    title="QR Code Generator Prototype",
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# slowapi wiring: the decorator on individual routes does the bucket
# check; we just register the shared limiter on app.state (slowapi
# reads it from there) and the 429 handler that produces a JSON
# response with Retry-After.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Order matters: API routes must register BEFORE the static catch-all so
# that POST /api/qr/create, GET /r/{token}, etc. take precedence over the
# StaticFiles mount at "/". The mount serves index.html on "/" via
# html=True, and falls through to 404 for paths it doesn't recognise.
app.include_router(router)
app.include_router(auth_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
