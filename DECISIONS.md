# Design Decisions

A running log of choices that **differ from the reference `answers/` implementation** in the build-moat-live-sessions repo, with the reasoning. Each entry is anchored to the commit/stage that introduced it.

---

## Stage 2 — `generate_token`: CSPRNG nonce instead of timestamp

**Reference (`answers/app/token_gen.py`):**
```python
nonce = f"{int(time.time())}_{attempt}"
hash_input = url + nonce
```

**Here:**
```python
nonce = secrets.token_bytes(8).hex()
hash_input = f"{url}|{nonce}"
```

**Why we changed it:**

1. **Concurrent-request safety.** Two requests for the same URL inside the same second would, in the reference, produce the *same* hash on attempt 0 — both burn through every retry on identical inputs until the loop gives up. `secrets.token_bytes` draws from the OS CSPRNG, so each attempt is independent regardless of clock or process.
2. **No reliance on wall clock.** Container restarts, frozen clocks, or NTP step-backs don't affect us. The reference's `int(time.time())` would mask itself across restarts.
3. **Separator hardening.** Using `|` between URL and nonce prevents a (theoretical) injection where a URL ending in digits would be ambiguous about where the nonce starts.

**Trade-offs:**

- The reference is marginally easier to reason about during a live whiteboard ("what's the input?" → "URL plus the time"). Ours requires the reader to know `secrets`.
- The reference is more reproducible for tests (you can freeze `time.time()`); ours needs to monkey-patch `secrets.token_bytes` if you want determinism.

Net: we pick robustness over teachability since this code runs in production-shaped concurrency, not on a slide.

---

## Stage 3 — `validate_url`: scoped normalization, no scheme upgrade

**Reference (`answers/app/url_validator.py`):**
```python
normalized = url.lower().rstrip("/")
if parsed.scheme == "http":
    normalized = normalized.replace("http://", "https://", 1)
```

**Here:** parse with `urlparse`, lowercase only `scheme` and `hostname`, preserve port/userinfo, keep path/query/fragment untouched, collapse the trailing slash only on bare root (and only when no query/fragment follows), reassemble with `urlunparse`.

**Why we changed it — four behavioral differences:**

1. **Path case is preserved.** `url.lower()` mangles case-sensitive paths. `https://github.com/User/Repo` and `https://github.com/user/repo` resolve to different repositories; lowercasing the path would silently corrupt destination URLs. S3 keys, JWT-in-path tokens, and GitHub raw URLs all have the same property. We lowercase only the scheme and host, which RFC 3986 explicitly defines as case-insensitive.

2. **Query string case is preserved.** Same reasoning — many APIs use case-sensitive query values (signatures, base64 payloads, OAuth tokens). The reference would break a signed S3 link.

3. **No `http → https` upgrade.** Not every target supports TLS on 443. Forcing the upgrade means a fraction of redirects silently 404 or hang on a refused connection. Mainstream URL shorteners (bit.ly, tinyurl) don't do this. If the user submits `http://`, we honour their intent and let the downstream server decide whether to redirect them to HTTPS.

4. **Trailing slash is query-aware.** The reference's `rstrip("/")` would turn `https://x.com/?q=1` into `https://x.com?q=1`. Per RFC 3986 the latter is technically valid, but a non-trivial fraction of servers (nginx with strict regex routes, some CDNs) reject it. We only drop the slash on bare root, and only when there's no query or fragment to glue back on.

**Additional improvement:** explicit `ValueError` for "missing hostname" instead of falling through to the blocklist branch (whose `None`-defensive `True` return masked the real reason).

**Trade-offs:**

- More code (the rebuild via `urlunparse` adds ~10 lines vs. a single `.lower().rstrip()`).
- Less aggressive deduplication. The reference would treat `HTTPS://Example.com/foo` and `https://example.com/Foo` as the same URL; we treat them as different. That's a feature, not a bug — but it's a feature with a cost in storage and analytics granularity.
- Doesn't help us actually dedupe by URL anyway, because `generate_token` injects a random nonce — same URL → different token regardless. So the normalization here is for *display consistency* and *downstream string comparison*, not for collision avoidance.

---

## Stage 4 — `redirect`: TTL-aware cache, naive-UTC time, tz coercion at boundary

**Reference (`answers/app/routes.py`):**

```python
redirect_cache: dict[str, str] = {}
...
if token in redirect_cache:
    _record_scan(token, request, db)
    return RedirectResponse(url=redirect_cache[token], status_code=302)
...
if mapping.expires_at and mapping.expires_at < datetime.utcnow():
    raise HTTPException(status_code=410, ...)
```

**Here:**

```python
redirect_cache: dict[str, tuple[str, datetime | None]] = {}
...
cached = redirect_cache.get(token)
if cached is not None:
    url, exp = cached
    if exp is None or exp > now:
        _record_scan(...); return RedirectResponse(url=url, status_code=302)
    redirect_cache.pop(token, None)  # evict expired
...
if mapping.expires_at is not None and mapping.expires_at <= _now_naive():
    raise HTTPException(status_code=410, ...)
```

**Why we changed it — three behavioral fixes:**

1. **Cache is TTL-aware.** The reference's cache stores only the URL, so a link that expires while it's still in cache continues to redirect 302 until the cache is invalidated for some unrelated reason. We store `(url, expires_at)` and re-check the expiry on every hit. Past-TTL entries are evicted and fall through to the DB path, which produces the canonical 410 response with the "expired" detail. Cost analysis: ~100 ns of clock + comparison overhead per cache hit, well under 1% of the per-request budget on FastAPI; the dominant cost in this path is the `_record_scan` INSERT, which both implementations share.

2. **`datetime.utcnow()` → `datetime.now(timezone.utc).replace(tzinfo=None)`** via a `_now_naive()` helper. `utcnow()` is deprecated in Python 3.12 and emits a `DeprecationWarning`. We keep the naive return so it stays comparable with the `DateTime` columns in `models.py` (which are declared without `timezone=True`).

3. **`_to_naive_utc` coercion at the API boundary.** Pydantic v2 parses ISO strings with a `Z` or `+00:00` suffix into tz-aware datetimes; the reference passes them straight into the model. Comparing a tz-aware `expires_at` to a naive `utcnow()` raises `TypeError`, so any client that sends `"expires_at": "2026-01-01T00:00:00Z"` would crash the redirect path at runtime. We normalize all incoming datetimes to naive UTC in `create_qr` and `update_qr` before they touch the DB.

**Minor tightening:** `< now` → `<= now` for the expiry check. The reference treats the exact-expiry instant as still-valid; we treat it as gone. Either is defensible, but `<=` matches the semantics most people expect from "expires at 2pm" ("at 2:00:00.000 it's already over").

**Trade-offs:**

- Cache stores 24 extra bytes per entry (datetime object). At 1M cached tokens, that's ~24 MB — trivial.
- More branches in the redirect handler (cache-hit-but-expired path is new). Readability cost paid up front in inline comments.
- If we migrate to Redis, the TTL-aware logic becomes redundant — Redis's `EXPIRE` handles eviction natively. The `_to_naive_utc` and `_now_naive` helpers can also retire if we switch to `DateTime(timezone=True)` columns.

**Known limitations (not fixed in this stage):**

- The in-memory dict is per-worker. In a multi-worker deployment, `delete_qr` only invalidates the worker that handles the DELETE; other workers serve stale until their cache entry naturally falls out. A real deployment needs a shared cache (Redis pub/sub or a TTL short enough to make staleness acceptable).
- TOCTOU between cache read and DB delete: a request that reads the cache milliseconds before `delete_qr` commits will still serve 302. Acceptable for a URL shortener; not acceptable for an auth system.

Validated end-to-end with `curl.exe` against a local uvicorn:
- POST create → `t5fWiKb` token returned
- GET `/r/{token}` → 302 to `https://example.com/Hello` (note: case-preserved path)
- GET `/r/NOPE123` → 404
- PATCH new URL → 200, `original_url` updated
- GET `/r/{token}` → 302 to `https://new-target.com`
- DELETE → 200
- GET `/r/{token}` → 410 "Gone — this link has been deleted"
