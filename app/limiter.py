"""Shared Limiter instance.

Lives in its own module so both `app.main` (which registers the
exception handler) and `app.routes` (which decorates endpoints) can
import it without a circular dependency.

Default backend is in-process memory. For a multi-worker deployment
swap `storage_uri='redis://...'` so all workers share counters.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import RATE_LIMIT_STORAGE_URI

# key_style="endpoint" so the bucket is keyed by (IP, handler name) — NOT
# by URL path. With the default "url", every distinct `/api/qr/{token}`
# path gets its own bucket, which means an attacker iterating tokens
# effectively bypasses the rate limit. "endpoint" makes all PATCH calls
# from one IP share a bucket regardless of which token they're hitting.
#
# storage_uri defaults to in-process memory; set RATE_LIMIT_STORAGE_URI=
# redis://host:6379/0 (or similar) to share buckets across uvicorn
# workers in production.
limiter = Limiter(
    key_func=get_remote_address,
    key_style="endpoint",
    storage_uri=RATE_LIMIT_STORAGE_URI,
)
