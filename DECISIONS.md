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
