# QR Code Generator

A dynamic QR code service: submit a URL, get back a short token + scannable PNG. The QR encodes a short URL that 302-redirects through this server, so the destination can be modified after the QR has been printed.

> Built in stages from the [build-moat-live-sessions](https://github.com/bohr109/build-moat-live-sessions) QR exercise scaffold. See [DECISIONS.md](DECISIONS.md) for behavioral differences from the reference answer, and [ANSWERS.md](ANSWERS.md) for the 5 design-question write-ups.

![tests](https://github.com/JShengP/qr-code-generator/actions/workflows/test.yml/badge.svg)

<!-- TODO(stage-8): drop a screenshot of the UI here once you've taken one.
     Suggested: scan a real URL, capture the result panel at /, save to
     docs/screenshot.png, then uncomment the line below.
     ![UI screenshot](docs/screenshot.png)
-->

## Endpoints

### QR

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/qr/create` | **session** | Create a short URL + QR code (optional `expires_at`). Returns a one-time `edit_token` for programmatic clients. |
| `GET` | `/r/{token}` | none | 302 redirect to the original URL (cache → DB → 404/410). |
| `GET` | `/api/qr/{token}` | none | Metadata (URL, timestamps, deletion/expiry state). |
| `PATCH` | `/api/qr/{token}` | **session OR bearer** | Update target URL and/or expiration. Owner shortcut: a signed-in caller who owns the mapping skips the bearer. |
| `DELETE` | `/api/qr/{token}` | **session OR bearer** | Soft delete; row stays in DB, subsequent redirects return 410. Recorded in `audit_logs`. |
| `GET` | `/api/qr/{token}/image` | none | PNG of the QR code that encodes the short URL. |
| `GET` | `/api/qr/{token}/analytics` | none | Total scans + scans-by-day breakdown. |
| `POST` | `/api/qr/{token}/rotate-edit-token` | **session OR bearer** | Issue a fresh `edit_token`; old one is invalidated. |
| `GET` | `/api/qr/mine` | session | List the signed-in user's QRs (anonymous returns empty). |

### Auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/request-link` | Magic-link sign-in. Dev mode prints the link to the server console; production sends email. |
| `GET` | `/api/auth/verify?token=...` | Consume a magic link, create session, set `qrs_session` cookie. |
| `GET` | `/api/auth/me` | Current user (or `null` if signed out). |
| `POST` | `/api/auth/logout` | Delete session row + clear cookie. |
| `GET` | `/api/auth/github/login` | OAuth: redirect to GitHub authorize (only registered when `GITHUB_CLIENT_ID` set). |
| `GET` | `/api/auth/github/callback` | OAuth callback: exchange code, find-or-create user, set session. |
| `GET` | `/api/auth/github/available` | Probe whether GitHub OAuth is configured server-side. |

**Two ways to authenticate a mutation:**

1. **Browser users** — sign in via magic link or GitHub; the `qrs_session` cookie carries the credential. The UI never asks for an `edit_token`.
2. **Programmatic clients** (CI scripts, curl, automation) — keep the `edit_token` that `POST /api/qr/create` returns once, then send it as `Authorization: Bearer <token>` on `PATCH`/`DELETE`/`rotate-edit-token`. The DB stores only the SHA-256 hash; losing the plaintext means rotating to issue a fresh one.

**Accounts unify on email:** signing in via magic link and then via GitHub with the same email merges into one user row. GitHub primary-email matches link an existing magic-link account to the GitHub identity; differing emails create separate users.

### Rate limiting

| Endpoint | Limit | Notes |
|---|---|---|
| `POST /api/qr/create` | **10 / min / IP** | Hardest path: hash + retry + DB write. |
| `GET /r/{token}` | **300 / min / IP** | Plus per-(token, ip) 1-second dedup on the scan-event INSERT, so refresh-spam can't bloat `scan_events`. |
| `PATCH /api/qr/{token}` | **30 / min / IP** | Defense-in-depth on the bearer-token check. |
| `DELETE /api/qr/{token}` | **30 / min / IP** | Same as PATCH. |

All via [`slowapi`](https://github.com/laurentS/slowapi). Buckets are keyed by `(endpoint, IP)` so an attacker iterating tokens shares one bucket per handler. Default backend is in-process memory; set `RATE_LIMIT_STORAGE_URI=redis://...` to share buckets across uvicorn workers.

### Configuration (env vars)

All configuration is centralized in [`app/config.py`](app/config.py) and reads `os.environ` at import time. Every value has a dev-safe default — set the env var only to override.

| Env var | Default | What it does |
|---|---|---|
| `DEPLOY_ENV` | `dev` | `production` hides `/docs`, `/redoc`, `/openapi.json`. |
| `DATABASE_URL` | `sqlite:///./qr_code.db` | SQLAlchemy URL. Postgres / MySQL also work. |
| `BASE_URL` | `http://localhost:8000` | Public URL encoded into the QR + returned in `short_url`. |
| `CREATE_RATE_LIMIT` | `10/minute` | slowapi expression for `POST /api/qr/create`. |
| `REDIRECT_RATE_LIMIT` | `300/minute` | slowapi expression for `GET /r/{token}`. |
| `MUTATION_RATE_LIMIT` | `30/minute` | slowapi expression for `PATCH`/`DELETE`. |
| `RATE_LIMIT_STORAGE_URI` | `memory://` | `redis://host:6379/0` to share buckets across workers. |
| `SCAN_DEDUP_WINDOW` | `1.0` | Seconds; per-(token, ip) burst dedup on scan recording. |
| `SCAN_FLUSH_BATCH_SIZE` | `10` | Buffered scans flush after N rows. |
| `SCAN_FLUSH_INTERVAL` | `5.0` | Buffered scans flush after N seconds since last flush. |
| `SESSION_COOKIE_NAME` | `qrs_session` | Cookie name for the opaque session token. |
| `SESSION_TTL_DAYS` | `30` | How long a session row stays valid. |
| `MAGIC_LINK_TTL_MINUTES` | `15` | How long a magic link stays redeemable. |
| `AUTH_REQUEST_RATE_LIMIT` | `3/minute` | Per-IP cap on `POST /api/auth/request-link`. |
| `EMAIL_PROVIDER` | `(empty)` | `console` (default, prints magic link to stdout) or future `resend` / `smtp`. |
| `EMAIL_FROM` | `noreply@localhost` | Sender address for production email. |
| `GITHUB_CLIENT_ID` | `(empty)` | Set to enable GitHub OAuth. Register at <https://github.com/settings/developers>. |
| `GITHUB_CLIENT_SECRET` | `(empty)` | Paired with `GITHUB_CLIENT_ID`. |

A blank `.env.example` is checked in at the repo root listing every var name; copy to `.env`, fill values, and your shell can `source` it (or use a tool like `direnv`).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Browser / QR scanner                                            │
└──────────────┬──────────────────────────────┬───────────────────┘
               │ GET /                        │ GET /r/{token}
               │                              │ (the hot path)
               ▼                              ▼
       ┌──────────────┐              ┌────────────────────┐
       │ static/      │              │ redirect()         │
       │ index.html   │              │  ┌──────────────┐  │
       │ app.js       │              │  │ in-mem cache │  │  hit → 302
       │ styles.css   │              │  │ (url, exp)   │──┼──→ Location
       └──────┬───────┘              │  └──────┬───────┘  │
              │                      │         │ miss     │
              │ fetch                │         ▼          │
              │ POST /api/qr/create  │  ┌──────────────┐  │
              ▼                      │  │ SQLite       │  │  not found → 404
       ┌──────────────┐              │  │ url_mappings │──┼──→ deleted/expired → 410
       │ create_qr    │              │  │ + scan_events│  │  ok → 302 + cache warm
       │  (limited:   │              │  └──────────────┘  │
       │  10/min/IP)  │              └────────────────────┘
       └──────┬───────┘
              │  validate_url() → normalize + blocklist
              │  generate_token() → SHA-256 + CSPRNG nonce + Base62 + retry
              ▼
       ┌──────────────┐
       │ SQLite       │
       │ url_mappings │
       └──────────────┘
```

**Why this shape:**

- **In-memory cache fronting SQLite** — the redirect path is the hottest endpoint. Cache stores `(url, expires_at)` so it can serve permanent and time-limited links from memory and self-evict past-TTL entries on hit.
- **Hash-with-CSPRNG-nonce tokens** — 7-char Base62 (62⁷ ≈ 3.5 T) with per-attempt random nonces, falling back to DB-uniqueness-check + retry on collision.
- **Rate limit only on the create path** — the only write endpoint that does real CPU work (hash + retry). Reads stay unbounded.
- **Static frontend separate from API** — `static/` mounted under `StaticFiles`; the API isn't templated, so the UI can move to a CDN or be replaced by a SPA without touching Python.

## Project Structure

```
qr-code-generator/
├── app/
│   ├── main.py            FastAPI app: API + StaticFiles + 429 handler + lifespan
│   ├── config.py          single source of truth for env-driven settings
│   ├── routes.py          9 QR endpoints + analytics
│   ├── schemas.py         Pydantic request/response models
│   ├── models.py          SQLAlchemy: url_mappings, scan_events, users,
│   │                      user_sessions, magic_links, audit_logs
│   ├── database.py        SQLAlchemy engine + session factory
│   ├── token_gen.py       SHA-256 + Base62 + collision retry, + edit_token
│   ├── url_validator.py   normalize + length + scheme + SSRF + blocklist
│   ├── limiter.py         shared slowapi Limiter
│   ├── auth.py            get_current_user dependency
│   ├── auth_routes.py     /api/auth/* magic-link endpoints
│   ├── oauth_github.py    /api/auth/github/* OAuth flow
│   └── email_service.py   pluggable EmailService (Console / future Resend)
├── static/                vanilla-JS UI served at / (login, My QRs, edit)
├── tests/                 91 pytest cases (test_api / test_auth / test_audit)
├── scripts/smoke.ps1      end-to-end PowerShell script (needs session cookie)
├── .github/workflows/     CI runs pytest on push/PR
├── DECISIONS.md           code-level deviations from answers/ with reasoning
├── ANSWERS.md             5 PROMPT.md design-question write-ups
├── .env.example           every env var documented with blank value
├── requirements.txt       runtime deps
└── requirements-dev.txt   adds pytest + httpx
```

## Setup

Prerequisite: **Python 3.10+**.

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

- **Web UI**: <http://localhost:8000/> — paste a URL, get a QR + short link.
- **API docs**: <http://localhost:8000/docs>

## Testing

Two layers, each covering the same scenarios:

**pytest (in-process, no server required)** — for CI and quick iteration:

```bash
pip install -r requirements-dev.txt
pytest -v
```

91 tests across three files (`test_api.py` / `test_auth.py` / `test_audit.py`) covering:
- 8 PROMPT.md scenarios + regressions for the Stage 2–4 design choices
- URL normalization, blocklist (incl. IDN/punycode homographs), SSRF block
- Rate limits (create / redirect / mutation) firing at threshold
- edit_token bearer auth, owner-shortcut auth, rotation chain
- Magic-link sign-in flow + session cookie + logout
- QR ownership isolation between users
- Audit log records every mutation (create / patch_url / patch_expires / delete / rotate)

Each test gets a fresh in-memory SQLite, a pre-authenticated session (`test-runner@example.com`), reset caches, and disabled rate limiter. Tests that exercise anonymous behavior call `client.cookies.clear()` explicitly.

CI runs the same suite on every push to `main` and every PR — see `.github/workflows/test.yml`.

**`scripts/smoke.ps1` (Windows, against a running server)** — for manual verification end-to-end:

```powershell
# Terminal 1
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# Sign in once via http://127.0.0.1:8000/ in your browser, then
# DevTools -> Application -> Cookies -> copy the `qrs_session` value.

# Terminal 2
$env:QRS_SESSION_COOKIE = '<paste the cookie value>'
.\scripts\smoke.ps1
```

Hits the running server with 12 scenarios (PROMPT.md basics + tz-aware expiry + edit_token rotation + anonymous-create-rejected) and prints PASS/FAIL per assertion. Exits non-zero on any failure so it can gate a release.

## Roadmap

PROMPT.md core (Stages 1–8):

- [x] Stage 1 — initial scaffold
- [x] Stage 2 — `feat(token)`: SHA-256 + Base62 + collision retry
- [x] Stage 3 — `feat(url)`: normalize + blocklist
- [x] Stage 4 — `feat(redirect)`: cache → DB → 404/410 fallback
- [x] Stage 5 — pytest suite + PowerShell smoke script
- [x] Stage 6 — static HTML frontend (vanilla JS + `fetch`)
- [x] Stage 7 — rate limit on create (`slowapi`, 10/minute per IP)
- [x] Stage 8 — design-decision write-up + CI + architecture docs

Post-review hardening:

- [x] SSRF + CRLF + userinfo + subdomain blocklist in `validate_url`
- [x] `edit_token` bearer auth on PATCH/DELETE
- [x] Per-(token, ip) scan dedup + redirect rate limit
- [x] PATCH/DELETE rate limit
- [x] `cachetools.TTLCache` for the redirect cache
- [x] Batched + async-flush scan_event writes
- [x] Env-driven config (`app/config.py`)
- [x] IDN/punycode homograph fold on blocklist
- [x] `rotate-edit-token` endpoint

User identity layer:

- [x] Magic-link auth (users / user_sessions / magic_links tables)
- [x] Sign-in / sign-out UI with auth bar
- [x] QR ownership + `GET /api/qr/mine` + My-QRs sidebar
- [x] GitHub OAuth as second sign-in path (`oauth_github.py`)
- [x] `POST /api/qr/create` now requires authentication
- [x] `audit_logs` table — every create/patch/delete/rotate recorded
