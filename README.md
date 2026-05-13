# QR Code Generator

A dynamic QR code service: submit a URL, get back a short token + scannable PNG. The QR encodes a short URL that 302-redirects through this server, so the destination can be modified after the QR has been printed.

> Built in stages from the [build-moat-live-sessions](https://github.com/bohr109/build-moat-live-sessions) QR exercise scaffold. See [DECISIONS.md](DECISIONS.md) for behavioral differences from the reference answer and the reasoning.

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

22 tests covering the 8 PROMPT scenarios plus regressions for URL normalization, blocklist, expired-link 410, and tz-aware ISO inputs. Each test uses an isolated in-memory SQLite DB and clears the redirect cache, so they're order-independent and parallel-safe.

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
- [ ] Stage 7 — rate limit on create
- [ ] Stage 8 — design-decision write-up
