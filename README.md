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

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/qr/create` | Create a short URL + QR code (optional `expires_at`) |
| `GET` | `/r/{token}` | 302 redirect to the original URL (cache → DB → 404/410) |
| `GET` | `/api/qr/{token}` | Metadata (URL, timestamps, deletion/expiry state) |
| `PATCH` | `/api/qr/{token}` | Update the target URL and/or expiration |
| `DELETE` | `/api/qr/{token}` | Soft delete; subsequent redirects return 410 |
| `GET` | `/api/qr/{token}/image` | PNG of the QR code that encodes the short URL |
| `GET` | `/api/qr/{token}/analytics` | Total scans + scans-by-day breakdown |

### Rate limiting

| Endpoint | Limit | Notes |
|---|---|---|
| `POST /api/qr/create` | **10 / min / IP** | Hardest path: hash + retry + DB write. |
| `GET /r/{token}` | **300 / min / IP** | Plus per-(token, ip) 1-second dedup on the scan-event INSERT, so refresh-spam can't bloat `scan_events`. |

Both via [`slowapi`](https://github.com/laurentS/slowapi). 11th create / 301st redirect within the window returns `429 Too Many Requests` with `Retry-After`. Default backend is in-process memory; swap to Redis (`storage_uri='redis://...'` in `app/limiter.py`) for multi-worker deployments.

**`PATCH` and `DELETE` require auth.** The create response includes a one-time `edit_token` (~256 bits, returned only on creation); subsequent PATCH/DELETE calls must include `Authorization: Bearer <edit_token>`. The DB stores only the SHA-256 hash. Losing the `edit_token` means losing the ability to edit the link.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
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
│   ├── main.py            FastAPI app: API + StaticFiles + 429 handler
│   ├── routes.py          7 endpoints (create / redirect / info / patch / delete / image / analytics)
│   ├── schemas.py         Pydantic request/response models
│   ├── models.py          SQLAlchemy tables: url_mappings, scan_events
│   ├── database.py        SQLite engine + session factory
│   ├── token_gen.py       SHA-256 + Base62 + collision retry
│   ├── url_validator.py   normalize + length + scheme + blocklist
│   └── limiter.py         shared slowapi Limiter
├── static/                vanilla-JS UI served at /
├── tests/                 25 pytest cases, in-process via TestClient
├── scripts/smoke.ps1      PowerShell end-to-end against a running server
├── .github/workflows/     CI runs pytest on push/PR
├── DECISIONS.md           code-level deviations from answers/ with reasoning
├── ANSWERS.md             5 PROMPT.md design-question write-ups
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

25 tests covering the 8 PROMPT scenarios plus regressions for URL normalization, blocklist, expired-link 410, tz-aware ISO inputs, and the rate limit. Each test uses an isolated in-memory SQLite DB, clears the redirect cache, and disables the rate limiter so they're order-independent and parallel-safe.

CI runs the same suite on every push to `main` and every PR — see `.github/workflows/test.yml`.

**`scripts/smoke.ps1` (Windows, against a running server)** — for manual verification end-to-end:

```powershell
# Terminal 1
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# Terminal 2
.\scripts\smoke.ps1
```

Hits the running server with the 8 PROMPT.md scenarios and prints PASS/FAIL per assertion. Exits non-zero on any failure so it can gate a release.

## Roadmap

- [x] Stage 1 — initial scaffold
- [x] Stage 2 — `feat(token)`: SHA-256 + Base62 + collision retry
- [x] Stage 3 — `feat(url)`: normalize + blocklist
- [x] Stage 4 — `feat(redirect)`: cache → DB → 404/410 fallback
- [x] Stage 5 — pytest suite + PowerShell smoke script
- [x] Stage 6 — static HTML frontend (vanilla JS + `fetch`)
- [x] Stage 7 — rate limit on create (`slowapi`, 10/minute per IP)
- [x] Stage 8 — design-decision write-up + CI + architecture docs
