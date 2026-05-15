# Programmatic access (API tokens)

The web UI covers 99% of normal use. Sometimes you want a **script** —
not a browser — to do the same things. This is what API tokens are for.

## When you'd use it

A short, opinionated list of real situations:

- **Daily auto-update** — a cron job at 06:00 that points your
  conference-room QR at today's slide deck URL.
- **CI deploy hook** — every successful merge re-points the staging
  QR at the freshly built preview URL.
- **Cross-device editing** — change the destination from your phone
  via a `curl` command in Termux without signing in.
- **Bulk operations** — a script that runs through 50 owned QRs and
  rotates them all to a campaign URL on launch day.

For all of these, your browser session cookie is useless — the
script isn't a browser. The API token is the alternative
credential: a long random string that lets a non-browser client
authenticate **for that one specific QR**.

## How to get one

1. Open the QR in the web UI (sign in, click it in the **My QR
   codes** sidebar, or create a fresh one).
2. Next to the **Token** field there's an **API token** button.
   Click it.
3. In the modal that opens, click **Generate new token**.
4. The server issues a fresh 256-bit random string. **Copy it
   immediately** — we never display it again. The database stores
   only a SHA-256 hash; nobody (including us) can recover the
   plaintext after this moment.
5. Click **Close** when you're done copying. Inside the modal
   there's a collapsible **Example curl** block with ready-to-paste
   commands using your actual token.

## How to use it

Send the token in the `Authorization` header, prefixed with
`Bearer `:

```bash
# Change destination
curl -X PATCH https://your-host.example/api/qr/Xedis7d \
  -H "Authorization: Bearer YOUR-TOKEN-HERE" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://new-destination.example/"}'

# Update expiry
curl -X PATCH https://your-host.example/api/qr/Xedis7d \
  -H "Authorization: Bearer YOUR-TOKEN-HERE" \
  -H "Content-Type: application/json" \
  -d '{"expires_at": "2026-12-31T23:59:59Z"}'

# Soft-delete (row stays in DB, /r/{token} returns 410)
curl -X DELETE https://your-host.example/api/qr/Xedis7d \
  -H "Authorization: Bearer YOUR-TOKEN-HERE"

# Rotate (invalidates the old token, returns a new one)
curl -X POST https://your-host.example/api/qr/Xedis7d/rotate-edit-token \
  -H "Authorization: Bearer YOUR-TOKEN-HERE"
```

The corresponding `Authorization`-free endpoints (everything in the
[README endpoints table](../README.md#endpoints) marked `none` for
auth) work over plain `curl` without any header.

## Security model

### The pattern is industry-standard

What we do is the same pattern several widely-used systems use for
their access tokens:

| System | Storage | Lost it? |
|---|---|---|
| GitHub Personal Access Token | server stores hash only | revoke + regenerate |
| AWS Secret Access Key | shown ONCE at create time | rotate (new key replaces) |
| Stripe API key (Live) | hash stored server-side | roll the key |
| Cloudflare API Token | shown ONCE | regenerate |
| **edit_token here** | SHA-256 hash in `url_mappings.edit_token_hash` | rotate via UI or API |

So **256-bit CSPRNG plaintext + single-round SHA-256 hash + one-time
display + rotate-to-recover** is a well-tested shape, not an
invention.

### Why SHA-256 instead of bcrypt / argon2

A common pushback: "shouldn't password-style hashes be used here for
defense in depth?" The honest answer is no, and writing it out:

- `bcrypt` / `argon2` exist to slow down brute force against
  **low-entropy** inputs (`letmein123`-class passwords).
- The token is **256 bits drawn from the OS CSPRNG**
  (`secrets.token_urlsafe(32)`). Brute-forcing it is 2²⁵⁶ trial
  hashes — physically infeasible regardless of the per-attempt
  hash cost.
- Slowing per-attempt hashing from SHA-256 (~100 ns) to bcrypt
  (~100 ms) buys 0 real security here and adds ~100 ms latency to
  every legitimate API call. That trade only makes sense when
  per-attempt cost is the bottleneck — for high-entropy random
  tokens, it isn't.

Full reasoning in [`DECISIONS.md` → "Post-review #3"](../DECISIONS.md).

### What the design defends against

| Threat | Defended? | How |
|---|---|---|
| **DB dump leak** | ✅ | Plaintext token never stored. Hash alone is unusable — no rainbow table for 256-bit random preimages. |
| **Timing attack on the comparison** | ✅ | `hmac.compare_digest` runs in constant time. |
| **Token in server logs / backups** | ✅ | Only the hash ever lands on disk. |
| **Brute force via the API** | ✅ | Mutation endpoints rate-limited (`30/minute/IP`) on top of the cryptographic infeasibility. |
| **Forensic gap after misuse** | ✅ | `audit_logs` records every PATCH / DELETE / rotate with IP, timestamp, and before/after values. |

### What the design does NOT defend against

Being honest about it matters more than pretending it covers
everything:

| Threat | Status | Notes |
|---|---|---|
| **Token sniffed in transit** | ❌ | Plain HTTP exposes the bearer to any device on the same network. Production deployment MUST use HTTPS. |
| **Token escapes via the user side** (screenshot, paste in chat, push to a public git repo, browser autofill DB stolen) | ❌ | Same as every bearer credential ever. User responsibility. |
| **Phishing** (user pasting the token into a malicious form) | ❌ | Standard credential phishing risk. No technical defense possible. |
| **Server compromise while running** | ❌ | An attacker on the running process can read inbound bearers in cleartext. Same as any HTTP API. |
| **Session-cookie theft → attacker calls rotate** | ❌ | If a signed-in session is stolen, the attacker can rotate the QR's bearer and lock the real owner out. Session security (HttpOnly + Secure + SameSite) does what it can; that's a session-auth problem, not a token-storage problem. |
| **No token expiration** | ❌ | Once issued, a token is valid forever until rotated. Stripe / GitHub PAT offer optional expiry; we don't. See "Possible hardenings" below. |

### Production deployment checklist (must-do)

Three non-negotiables before exposing this service to real users:

1. **HTTPS everywhere.** Run behind a TLS-terminating reverse proxy
   (fly.io, Render, Cloudflare, Nginx + certbot). HTTP-only deployment
   makes the entire bearer model leak by design.
2. **Session cookie's `Secure` flag.** The app already sets `secure=True`
   when `DEPLOY_ENV=production` — confirm that env var is set on the
   production host.
3. **`X-Forwarded-For` parsing.** Behind a reverse proxy,
   `request.client.host` is the proxy's IP, so the rate-limit
   bucket and audit-log `ip_address` collapse all real users to one
   row. Configure slowapi's `key_func` and audit's IP source to
   read the forwarded header. Currently noted as a follow-up in
   [`DECISIONS.md` "Stage 7 known limitations"](../DECISIONS.md).

### Possible hardenings (optional, not in scope yet)

Each item is a real improvement; whether to add it depends on the
threat model for the deployment:

| Hardening | Comparable to | Effort |
|---|---|---|
| `expires_at` on `url_mappings.edit_token_hash` (auto-revoke after N days, default 90) | GitHub PAT optional expiry | ~30 min |
| Multiple active tokens per QR with optional `name` labels ("cron", "ci", "phone") | AWS allows 2 simultaneous access keys for zero-downtime rotation | ~1 hr |
| Optional IP allowlist per token (only accept Bearer from listed CIDRs) | Cloudflare API tokens | ~30 min |
| Re-confirm-by-email when rotating a token for a long-active QR | GitHub PAT step-up for sensitive ops | ~1 hr |
| `audit_logs.user_agent` column for stronger forensics | GitHub audit log | ~10 min |

### Practical advice for end users (paste this into the modal copy or your own README)

> The edit token is like a key to one specific QR code. We never keep
> a copy — what's in the database is just the shape of the key (a
> SHA-256 hash). If you lose it, we can't make another that looks the
> same; you can only cut a fresh key (rotate), which automatically
> retires the old one.
>
> The moment you click Generate is your only chance to record it.
> Practical tips:
>
> - Paste it into a password manager (1Password, Bitwarden, etc.) or
>   into the secret store of your CI / cron host (GitHub Actions
>   secrets, AWS Secrets Manager, `.env` not committed to git).
> - Never paste it into Slack, Discord, an email, or any commit message.
> - When using it from a script, always send it over HTTPS — over plain
>   HTTP anyone on the network can read it.
> - If you suspect it leaked, immediately Generate a new token. The
>   old one stops working the instant the new one is issued.

## FAQ

### What if I lose the token?

Re-open the modal and click **Generate new token** again. The old
token stops working the instant the rotation commits; the new one
replaces it. You can do this from the web UI as the owner — you
don't need to know the old token to issue a new one.

### Is it tied to my account?

No — it's tied to **one specific QR**. Each QR has its own token.
This is intentional: you can hand the token for one QR to a
contractor or a script without giving up control of the rest of
your account. The bearer model says **possession = authority for
this resource**.

### Is it secure?

- Token is 256 bits drawn from the OS CSPRNG (`secrets.token_urlsafe`).
  Brute force is computationally infeasible.
- Server stores only `sha256(token)` in `url_mappings.edit_token_hash`.
  Compromising the DB does not reveal usable tokens.
- Comparison uses `hmac.compare_digest` — constant time, no
  side-channel leakage.
- `POST`/`PATCH`/`DELETE` paths are rate-limited (`30/minute/IP`,
  `10/minute/IP` for create).
- Every PATCH/DELETE/rotate writes an `audit_logs` row with the
  caller's IP, the action, the before/after values, and the
  timestamp. Visible via `GET /api/qr/{token}/audit` (owner-only).

### Can I have multiple tokens for one QR?

No — one active token per QR. Rotating issues a new one and
invalidates the previous one. If you need multiple independent
clients to PATCH the same QR concurrently, share the same token
between them; the rate limit applies per-IP and per-endpoint so
two clients from different IPs don't fight for a bucket.

### What if my account gets deleted?

Today: the QR row stays, the token stays valid. Account deletion
is out of scope for the prototype. A production system would
cascade-soft-delete the user's QRs and revoke their tokens at the
same time.

### Can I use this from JavaScript in a browser?

You **can** — `fetch(url, { headers: { Authorization: 'Bearer ...' } })`
works fine. But you don't usually need to: if the user is signed in
to the same site in that browser, the session cookie already handles
auth (owner shortcut). The bearer is the right answer for **scripts
that aren't browsers** — anything where the cookie jar isn't carrying
your session.

### Where do I see what's been changed?

`GET /api/qr/{token}/audit` returns the full mutation history as JSON,
or look at the **History** section in the result panel when the QR is
open in the web UI.

## What the bearer-auth path explicitly does NOT cover

- **Creating new QRs.** `POST /api/qr/create` requires a signed-in
  session (the user identity is what owns the row). There's no
  "API token for creating" — that would essentially be a per-account
  API key, which is a different (and broader) primitive. Not in scope
  yet.
- **Reading other people's QRs.** Bearer auth is scoped to the
  specific QR whose token you hold. It cannot list someone else's
  QRs or read their audit log.
- **Anything in the UI auth surface** (login, OAuth, session
  management). Use cookies in the browser; use bearers from scripts.
