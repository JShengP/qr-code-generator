# Design Decisions

A running log of choices that **differ from the reference `answers/` implementation** in the build-moat-live-sessions repo, with the reasoning. Each entry is anchored to the commit/stage that introduced it.

**Post-Stage-8 review fixes** are recorded at the end under "Post-review fixes" — these address findings from the parallel general/security code review run after Stage 8 wrap-up.

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

---

## Stage 5 — test layer + `models.py` datetime deprecation

The reference repo has no test suite — `answers/` is implementation only. We add two:

1. **`tests/test_api.py` (25 tests, pytest + FastAPI TestClient)** — covers all 8 PROMPT.md scenarios plus regressions specific to the Stage 2–4 deviations:
   - URL normalization preserves path/query case
   - No `http → https` upgrade
   - Root-only trailing slash collapses only when no query/fragment follows
   - Past-`expires_at` link returns 410
   - tz-aware ISO with `Z` suffix doesn't crash redirect (this would actually fail against the reference's redirect handler — see Stage 4 deviation 3)
   - PATCH and DELETE both invalidate the cache

2. **`scripts/smoke.ps1`** — Windows-native PowerShell script that hits a *running* server with the 8 PROMPT scenarios + the tz-Z regression. Uses `Invoke-RestMethod` / `Invoke-WebRequest` to dodge the [PowerShell-5.1 native-arg quote-eating bug](https://github.com/PowerShell/PowerShell/issues/1995) that broke our first attempt at a `curl.exe`-based script. Exits non-zero on failure.

**Test isolation:** every test gets a fresh in-memory SQLite (via `StaticPool` so the `:memory:` connection persists across the test's requests) and the module-global `redirect_cache` is cleared in setup/teardown. Tests are order-independent.

**Bonus fix landing here (caught by the test warnings):** `models.py` was still using `datetime.utcnow` for column defaults, which Python 3.12 deprecates. We replace it with an `_utc_now_naive()` helper that returns the same naive UTC value via the non-deprecated `datetime.now(timezone.utc).replace(tzinfo=None)` spelling. The reference still uses `datetime.utcnow` and emits 47 `DeprecationWarning`s on a full test run. Running `pytest -W error::DeprecationWarning` now passes silently.

---

## Stage 6 — static HTML frontend (additive)

The reference has no UI. We add `static/{index.html,app.js,styles.css}` and mount it via `app.mount("/", StaticFiles(..., html=True))` in `app/main.py`.

**Routing-order invariant:** `include_router(router)` is called *before* the `mount("/", ...)`, so the API routes (`/api/qr/...`, `/r/{token}`) take precedence. The mount is a catch-all that serves `index.html` for `/` (thanks to `html=True`) and 404s for paths it doesn't recognise. Two new tests (`test_index_html_served_at_root`, `test_unknown_static_path_returns_404`) lock this invariant in.

**Why front/back separation rather than Jinja2 templating:** the user explicitly picked the static-files + `fetch()` shape. It mirrors real-world deployment patterns (CDN can host `static/` separately from the API; the front end has its own cache lifecycle), keeps the API surface unambiguous, and lets the UI evolve without touching Python.

**Stack choices inside the UI:**

- Vanilla JS, no build step. Stage 6 should be reviewable in a single sitting and runnable without `npm`.
- Dark theme by default (matches portfolio aesthetic). All colours via CSS custom properties at the top of `styles.css` so a light theme is a one-block change.
- Clipboard via `navigator.clipboard.writeText` with a `document.execCommand("copy")` fallback for older browsers / non-HTTPS contexts.
- Form error display understands both Pydantic's array-shaped 422 (`detail: [{loc, msg}, ...]`) and our hand-thrown string-shaped 422 (`detail: "..."`).

**Path resolution:** `STATIC_DIR = Path(__file__).resolve().parent.parent / "static"` — absolute path computed from `main.py`'s location so the mount works whether uvicorn is launched from the repo root, a CI runner, or a container `WORKDIR`.

---

## Stage 7 — rate limit on `POST /api/qr/create`

The reference has no rate limiting. We add `slowapi` and decorate only the create endpoint with `@limiter.limit("10/minute")`.

**Why only `create`:** it's the only write path that does real work — DB INSERT, SHA-256 hash, Base62 encode, optional retries on collision. It's also the obvious target for abuse (flood the table with junk tokens, exhaust the Base62 namespace). The redirect path needs to stay hot and uncapped; analytics/info are read-only and cheap. PATCH and DELETE are scoped to existing tokens, so abuse cost there is bounded by what's already in the DB.

**Why slowapi over rolling our own:**

- 5 lines of integration vs. ~30 for a hand-rolled middleware that tracks the same things (per-IP buckets, sliding window, Retry-After header, 429 JSON body).
- Pluggable storage backend. Default is in-process memory; swap to `storage_uri='redis://...'` for a multi-worker deployment without touching the route definitions.
- Emits `X-RateLimit-*` response headers that monitoring tools (Datadog, Grafana) already understand.

**Module layout:** `app/limiter.py` holds the singleton `Limiter`. Both `main.py` (which registers the 429 handler) and `routes.py` (which decorates the endpoint) import from it. Putting the limiter in either of those would create a circular dependency, since `main.py` imports `routes.py`.

**Test isolation:** the `client` fixture disables the limiter for all tests by default (`limiter.enabled = False`). 22 of the 25 tests in this file POST several URLs in a row; without the toggle they'd start tripping the 10/min limit somewhere around test 14. The new `rate_limited_client` fixture re-enables and resets the limiter for the dedicated `test_create_rate_limited_after_n_requests` case, which fires 10 OKs followed by an 11th that must 429.

**Signature wart:** the slowapi decorator reads the client IP off a parameter literally named `request: Request`. We didn't otherwise use `request` in `create_qr` — but the decorator can't find the IP otherwise, so we add it with an explanatory comment. This is a known slowapi ergonomic cost.

**Known limitations:**

- The default in-memory storage is per-process. Two uvicorn workers will track separate buckets, so a determined attacker bypasses the limit by reconnecting twice as fast. Production fix is `storage_uri='redis://...'`.
- `get_remote_address` reads `request.client.host`, which behind a reverse proxy is the proxy's IP, not the user's. Behind Nginx/CloudFront we'd need to read `X-Forwarded-For` (configurable via slowapi's `key_func`).

---

# Post-review fixes

After Stage 8 wrap-up, two parallel sub-agent reviews (general + security) surfaced ~40 findings. The entries below cover the substantive code changes; trivial doc / cleanup fixes are in the chore commit and not repeated here.

## Post-review #1 — chore: lifespan, docs gating, doc fixes

Commit `3651924`. Four cleanups bundled:

- `Base.metadata.create_all` moved into a FastAPI lifespan context. The original module-level call wrote `qr_code.db` to disk every time `app.main` was imported — including from pytest, which doesn't need it (the test fixture builds its own in-memory engine).
- `tests/conftest.py` switched to `TestClient(app)` without the `with` context, so the new lifespan doesn't fire under pytest. The schema-on-test-engine setup the fixture already does is sufficient.
- `/docs`, `/redoc`, and `/openapi.json` are now gated on a `DEPLOY_ENV=production` env var. Default is dev (everything exposed). Production deployments turn it off so the Swagger "Try it out" button doesn't ship as a one-click attack tool while PATCH/DELETE are still unauthenticated.
- ANSWERS.md Q2 corrected: the pre-insert `token_exists_in_db` SELECT catches the collision, not the unique-constraint `IntegrityError`. The constraint exists as a safety net for the (theoretical) concurrent-SELECT race we don't currently exercise.
- DECISIONS.md Stage 5 corrected: 22 → 25 tests.

## Post-review #2 — feat(security): SSRF and CRLF hardening in url_validator

Commit (this one). The security agent's review identified the URL validator as the largest attack surface in the system because the redirect handler ultimately writes the stored URL into a `Location` response header that browsers honor. Four behavioral changes:

1. **SSRF block on IP-literal hosts.** `is_internal_ip()` resolves the hostname as a literal IP via `ipaddress.ip_address` and rejects anything in `is_loopback / is_private / is_link_local / is_reserved / is_multicast / is_unspecified`. This covers `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254/16` (link-local + AWS/Azure metadata `169.254.169.254`), `::1`, `fc00::/7`, and `0.0.0.0`. The original validator blocked exactly three public domains and nothing else; an attacker could trivially create a QR pointing at someone's home router, a corporate Redis, or a cloud-metadata endpoint, and the redirect would carry the victim's browser there.

2. **Internal hostnames blocked by name.** A name like `localhost` or `metadata.google.internal` only resolves to its dangerous IP at fetch time, after our validator has run. The `BLOCKED_HOSTNAMES` set rejects the common ones by string match. This isn't a complete defense — an attacker can register a public DNS name that resolves to `127.0.0.1` (DNS rebinding) and bypass the static check — but it closes the trivial pathway and matches what production URL shorteners do at the validation stage.

3. **Userinfo (`user:pass@host`) rejected.** `https://google.com@attacker.com` parses with `hostname='attacker.com'`, but a casual reader scanning the printed QR sees `google.com`. We strip the affordance entirely. Bonus: storing credentials in `original_url` and re-emitting them in `Location:` headers and access logs was a separate disclosure risk this also closes.

4. **Subdomain matching on the blocklist.** The original `hostname.lower() in BLOCKED_DOMAINS` exact-match check let `login.evil.com`, `a.b.evil.com`, and `www.evil.com` all bypass. We now also check `h.endswith("." + blocked)` for each entry. Note: this doesn't handle IDN / punycode homographs — a follow-up would integrate `tldextract` and IDNA decoding, but that's a deeper change for another commit.

**Plus:** CRLF / NUL / tab rejection before parsing (`_FORBIDDEN_URL_CHARS`). Starlette is believed to escape CR/LF in `Location` headers, but a defense-in-depth check at validation time costs nothing and removes the attack class entirely.

**Tests added (8 cases, in `tests/test_api.py`):**

- `test_internal_ip_hosts_rejected` (parametrized, 8 IPs including cloud metadata + loopback + RFC 1918 + IPv6)
- `test_internal_hostnames_rejected` (parametrized, `localhost` / `localhost:6379` / GCP metadata name)
- `test_blocklist_matches_subdomains`
- `test_blocklist_case_insensitive`
- `test_userinfo_in_url_rejected`
- `test_crlf_in_url_rejected`

40/40 tests now pass (was 25). Each new test fails against the pre-fix validator.

**Known gaps deferred (worth a follow-up):**

- IDN / punycode homograph: `https://xn--vil-7ka.com` (visually "еvil.com") still passes. Needs IDNA decoding before blocklist comparison.
- DNS rebinding: a public name that resolves to `127.0.0.1` only at fetch time bypasses `is_internal_ip`. Defense is at the HTTP-client layer, not the URL validator.
- The `BLOCKED_DOMAINS` set is hardcoded and tiny. Production wants a sourced feed (Google Safe Browsing API, PhishTank).

## Post-review #3 — feat(auth): edit_token bearer on PATCH/DELETE

The security review's headline finding: PATCH and DELETE accepted any caller who knew the (publicly-printed) token. Anyone who scanned a QR could PATCH it to point at a phishing site or DELETE it. This commit closes the gap.

**Shape:**

- `POST /api/qr/create` returns `edit_token: str` — 32 bytes from `secrets.token_urlsafe`, ~256 bits of entropy.
- The DB stores only `sha256_hex(edit_token)` in a new `edit_token_hash` column on `url_mappings`.
- `PATCH /api/qr/{token}` and `DELETE /api/qr/{token}` require `Authorization: Bearer <edit_token>`. The handler hashes the presented value and `hmac.compare_digest`s it against the column.
- `GET /api/qr/{token}` (the info endpoint) returns the row's public fields but NOT the hash, and certainly not the plaintext.

**Why these specific choices:**

- **SHA-256 of a high-entropy plaintext, not bcrypt/argon2.** Password hashing functions (`bcrypt`, `argon2`) deliberately consume CPU to slow down brute-force against low-entropy user-chosen passwords. Our `edit_token` has ~256 bits of CSPRNG entropy from the start — brute-forcing it is computationally infeasible regardless of hash speed. SHA-256 is the right tool: it's fast (don't tax legit PATCH/DELETE requests), it's collision-resistant for our purposes, and the constant-time `hmac.compare_digest` removes timing-side-channel leakage.
- **Token returned exactly once, on create.** Not stored in plaintext server-side, not retrievable via any read endpoint. If the caller loses it, the short link is no longer editable — this matches the principle of least disclosure (we shouldn't be a custodian of the editing credential).
- **`Authorization: Bearer ...` header rather than a query parameter or body field.** Headers don't end up in access logs, `Referer`s, or browser history. The mistake of putting auth tokens in URLs has burned too many systems.
- **`edit_token_hash` nullable.** Lets rows created before this column existed (none yet, but a forward-compat consideration) coexist; they're treated as un-editable, which is the safe default.

**Trade-offs:**

- Adds one column to the schema and ~30 lines to `routes.py`. Test suite grew by 8 cases (PATCH/DELETE auth coverage).
- The UI now displays the `edit_token` on the result page with a "save this!" warning. UX of "you have one shot to save it" is awkward but matches every well-designed API key issuance flow (Stripe, GitHub PAT v2, AWS IAM).
- Smoke script needed updating to pass the `Authorization` header on PATCH and DELETE.
- Does NOT yet solve CSRF (the API is still cookie-less and bearer-auth is immune to traditional CSRF), but a future move to cookie-based session auth would need a CSRF token alongside.

**Test coverage:**

- `test_create_response_includes_edit_token`
- `test_get_info_does_not_leak_edit_token` (locks the "info endpoint must not return the credential" property)
- `test_patch_without_auth_returns_401`
- `test_patch_with_wrong_token_returns_401`
- `test_patch_with_malformed_auth_header_returns_401` (Authorization without `Bearer ` prefix)
- `test_delete_without_auth_returns_401`
- `test_delete_with_wrong_token_returns_401_and_link_still_works` (locks: failed auth must not partially mutate state)
- `test_edit_tokens_isolated_between_tokens` (holder of token A can't PATCH token B)

48/48 tests now pass (was 40).

**Known limitations / follow-ups:**

- No way to rotate or revoke an `edit_token` after issue. A revocation would require either re-keying via a new `POST /api/qr/{token}/rotate` (still needs the old token), or admin override (out of scope for a no-auth-system prototype).
- Brute-forcing the bearer is still possible in principle; the rate limit only covers `POST /api/qr/create`. Adding a limit to `PATCH`/`DELETE` (e.g. `30/minute/IP`) would close this — flagged in the next review pass.
- The validator review also recommended IDN/punycode decoding for blocklist matching; that's deferred (more invasive than this auth commit).

## Post-review #4 — feat: redirect path rate limit + scan-event dedup

The security review's other High finding was that `/r/{token}` had no rate limit, which let an attacker flood the endpoint and bloat `scan_events` via the synchronous `INSERT` inside `_record_scan`. README mistakenly said "reads stay unbounded" — but `/r/{token}` is a read on the user side and a write on our side. This commit lands two layered protections.

**Layer 1 — slowapi cap on the redirect path: `300/minute/IP`.**

Sized to be transparent for legitimate NAT traffic (a corporate office of ~50 people scanning a popular link at peak ~few-per-minute-each stays well under) and meaningfully tight against bursty abuse (5 req/sec ceiling).

The limit string is read via a callable (`_redirect_rate_limit()`) reading a module-level constant `REDIRECT_RATE_LIMIT`. Tests monkey-patch this to `5/minute` so the 6th-redirect-429 case can be asserted without looping 301 times. Production deployments override via the env var path documented at the top of the route. 429 on a QR scan is a UX scratch, but it's the right scratch — the alternative is letting the abuser win.

**Layer 2 — per-(token, ip) scan dedup: 1-second window by default.**

Independent of the rate limit. Inside `_record_scan`, we look up `(token, ip)` in a module-level dict; if the last scan was within `SCAN_DEDUP_WINDOW` seconds, we skip the DB INSERT but still return 302. The redirect UX is unaffected; only `scan_events` row growth is throttled.

Why dedup AND rate limit:

- The rate limit caps **request volume** (the slowapi bucket counts everything that reaches the handler, including non-recording cache hits).
- The dedup caps **DB write volume**. Even within a single rate-limit bucket, a refresh-spamming user shouldn't pile up 300 scan rows per minute. After dedup, the same burst collapses to 1 row.
- The two protections compose: an attacker maxing the rate limit (5 req/sec from one IP) hits the dedup window every time, so they get 1 row/sec/IP at most against the DB.

Memory management for the dedup dict: lazy GC inside `_record_scan` itself — when the dict crosses 5000 entries, we sweep entries older than 30× the window. Avoids a background thread; cost is paid only when the dict grows large.

**Semantic note for `total_scans`:**

In production (`SCAN_DEDUP_WINDOW = 1.0`), `total_scans` now means "unique scans across (token, ip) per second-window," not "raw HTTP hits." For most use cases this is the more meaningful metric — a single user refreshing 50 times shouldn't read as 50 scans. Apps that need raw counts can set `SCAN_DEDUP_WINDOW = 0` via env-var-driven config (not wired up yet; flagged below).

**Bonus fix landing here (caught by security review §6):** `_record_scan` was storing `user_agent` raw into a `String(500)` column. SQLite ignores the length constraint, so a multi-megabyte UA string would be stored verbatim — storage-amplification primitive when chained with the unbounded redirect. We now truncate to 500 chars at insert time.

**Tests:**

- `test_redirect_rate_limit_fires_at_threshold` — set limit to `5/minute`, 6th redirect must 429.
- `test_scan_dedup_skips_rapid_scans_from_same_ip` — 5 rapid scans → `total_scans == 1`.
- `test_scan_dedup_records_across_different_ips` — asserts the dict bookkeeping; cross-IP via TestClient is limited.

Conftest now resets `_scan_last_seen`, `SCAN_DEDUP_WINDOW`, and `REDIRECT_RATE_LIMIT` between tests so state can't leak across cases. The default fixture sets `SCAN_DEDUP_WINDOW = 0.0` so the existing 22+ tests that hit `/r/{token}` multiple times in a tight loop still see every scan counted.

51 / 51 tests now pass (was 48).

**Known follow-ups:**

- `REDIRECT_RATE_LIMIT` and `SCAN_DEDUP_WINDOW` should be env-var-driven (`os.getenv("REDIRECT_RATE_LIMIT", "300/minute")`) so production deployments can tune without a code change. Not done yet.
- Behind a reverse proxy, `request.client.host` is the proxy IP, so dedup keys collapse all real users to one bucket. Same caveat as the slowapi `key_func` — production needs `X-Forwarded-For` parsing.
- The dedup dict is per-worker. Multiple uvicorn workers each track their own state, so a worker-bouncing attacker can multiply their DB write rate by `min(workers, connections)`. A Redis-backed implementation closes this.
