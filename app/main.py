from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .database import Base, engine
from .limiter import limiter
from .routes import router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="QR Code Generator Prototype")

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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
