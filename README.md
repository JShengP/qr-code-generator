# QR Code Generator

A dynamic QR code service: submit a URL, get back a short token + scannable PNG. The QR encodes a short URL that 302-redirects through this server, so the destination can be modified after the QR has been printed.

> Work in progress — built in stages from the [build-moat-live-sessions](https://github.com/anthropics/anthropic-cookbook) QR exercise scaffold.

## Status

| Endpoint | State |
|---|---|
| `POST /api/qr/create` | ⏳ depends on `generate_token` + `validate_url` |
| `GET  /r/{token}` | ⏳ TODO |
| `GET  /api/qr/{token}` | ✅ |
| `PATCH /api/qr/{token}` | ✅ |
| `DELETE /api/qr/{token}` | ✅ |
| `GET  /api/qr/{token}/image` | ✅ |
| `GET  /api/qr/{token}/analytics` | ✅ |

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

API docs: <http://localhost:8000/docs>

## Roadmap

- [ ] Stage 2 — `feat(token)`: SHA-256 + Base62 + collision retry
- [ ] Stage 3 — `feat(url)`: normalize + blocklist
- [ ] Stage 4 — `feat(redirect)`: cache → DB → 404/410 fallback
- [ ] Stage 5 — smoke-test script for full lifecycle
- [ ] Stage 6 — minimal HTML frontend
- [ ] Stage 7 — rate limit on create
- [ ] Stage 8 — design-decision write-up
