"""Shared Limiter instance.

Lives in its own module so both `app.main` (which registers the
exception handler) and `app.routes` (which decorates endpoints) can
import it without a circular dependency.

Default backend is in-process memory. For a multi-worker deployment
swap `storage_uri='redis://...'` so all workers share counters.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
