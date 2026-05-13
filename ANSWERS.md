# Design Question Answers

Answers to the 5 questions in [`PROMPT.md`](https://github.com/bohr109/build-moat-live-sessions/blob/main/qr_code_generator/PROMPT.md), informed by what we actually built across Stages 2–7. Each section has a draft I wrote based on the implementation; the **Your take** line at the bottom is reserved for you to add a personal angle before the live session.

---

## 1. Static vs Dynamic QR Code

> Why does this system use dynamic QR codes (encode short URL) instead of static (encode original URL directly)? When would you choose static instead?

**Why dynamic for this system:**

Three of the spec's requirements are impossible with static QR codes:

1. **"Users can modify the target URL after QR code creation"** — a static QR locks the destination at print time. If a poster is already printed, you can't change where it points unless the QR encodes a URL you control.
2. **Analytics** — every scan of a static QR hits the destination directly; your server never sees it. Dynamic QRs route every scan through your `/r/{token}` endpoint, which is the only place you can record scan time, user agent, and IP.
3. **Soft delete / expiration** — a static QR will keep working as long as the destination resolves. There's no way to "expire" or "revoke" it after distribution.

**The trade-off you accept for dynamic:**

- Extra network hop on every scan (server-side 302), measurable in real-world latency. For permanent redirects you'd save this by serving 301, but that breaks analytics — see Q3.
- The redirect server is now a hard dependency. If your domain or hosting dies, every printed QR turns into a 502.
- QR data is denser (≤ 25 chars for a typical short URL vs. potentially hundreds for a full URL), which is actually a positive — denser = simpler pattern = easier to scan from a distance or a curved surface.

**When static wins:**

- The destination genuinely never changes: Wi-Fi credentials at a café, a business card vCard, a permanent product manual PDF on S3.
- You don't control redirect infrastructure: a print campaign that runs once and is throwaway.
- Privacy: static QRs leak no scan data to the QR issuer. For a contact-sharing QR on someone's badge, that's a feature.
- Offline contexts: museum exhibits, hiking signage where mobile data might not work but the destination is `geo:lat,lon` or a vCard.

The fact that this exercise demands modification + analytics + expiration is what forces dynamic. If the requirement were just "make QR codes for fixed URLs," static is the right call.

**Your take:**

---

## 2. Token Generation

> How will you generate short URL tokens? What happens when two different URLs produce the same token? How does collision probability change as the number of tokens grows?

**Our approach** (`app/token_gen.py`):

```python
nonce = secrets.token_bytes(8).hex()
digest = hashlib.sha256(f"{url}|{nonce}".encode()).digest()
token = base62_encode(digest)[:TOKEN_LENGTH]   # TOKEN_LENGTH = 7
```

Looped up to `MAX_RETRIES = 10`; we check the DB after each attempt and accept the first token that's unused.

**Why this shape:**

- **7-char Base62** = 62⁷ ≈ 3.52 × 10¹² distinct tokens. Short enough to print legibly under a QR; large enough that the birthday paradox doesn't catch up until we're at multi-millions of records.
- **Base62 (`a-zA-Z0-9`)** is URL-safe with no characters that need percent-encoding. Base64's `+/=` would force encoding in URLs, which makes the short URL longer than necessary.
- **SHA-256 of `(url + nonce)`** rather than pure randomness gives us avalanche behavior on the nonce — even small nonce changes produce uncorrelated outputs in the Base62 space.
- **`secrets.token_bytes` over `time.time()`** as the nonce: a timestamp nonce makes two concurrent requests for the same URL within the same second produce identical tokens and burn every retry. CSPRNG nonces are independent across attempts and processes. [DECISIONS.md Stage 2](DECISIONS.md) walks through this in detail.

**What happens on a collision:**

Two different URLs producing the same 7-char token would mean their full 32-byte SHA-256 digests agree in the first ~42 bits worth of Base62 output. When the second insert hits, SQLAlchemy raises an `IntegrityError` on the unique constraint, the retry loop catches it via `token_exists_in_db`, picks a new nonce, and re-hashes. The first URL keeps its token; the second gets a fresh one. From the user's perspective, nothing observable happens — just an extra ~50 µs of CPU.

**How probability scales (birthday paradox):**

For `N` existing tokens in a `K`-sized namespace, the probability that the next token collides is roughly `N / K`. For K = 62⁷:

| Tokens stored (N) | P(next collision) | P(at least one in history, ~N²/2K) |
|---|---|---|
| 1,000 | 2.8 × 10⁻¹⁰ | ≈ 0 |
| 100,000 | 2.8 × 10⁻⁸ | ≈ 0.0014% |
| 1,000,000 | 2.8 × 10⁻⁷ | ≈ 0.14% |
| 10,000,000 | 2.8 × 10⁻⁶ | ≈ 14% |

The "14% by 10M tokens" looks scary but is fine — the cost of a collision is one retry, not a failure. We're nowhere near exhausting the namespace; we just need to keep retrying past collisions, which we do. If we ever stored ~100M tokens we'd want to bump `TOKEN_LENGTH` to 8 (gives us 62× more room).

**Your take:**

---

## 3. Redirect Strategy

> Why 302 (temporary) instead of 301 (permanent)? What are the trade-offs for analytics, URL modification, and latency?

**We use 302.** A 301 would break three things this system needs.

**Why 302:**

| Concern | 302 (what we do) | 301 (alternative) |
|---|---|---|
| Analytics | Every scan hits `/r/{token}`. We can record it. | Browsers and most HTTP clients cache 301s aggressively (often permanently). Subsequent scans never reach our server, so analytics become a one-shot count. |
| URL modification (`PATCH`) | Next scan picks up the new destination immediately. | The cached 301 in the browser still points at the old URL. Some users would never see the update without clearing cache. |
| Soft delete → 410 | A 410 response after a previous 302 has clear semantics: "yes I used to redirect here, now I don't." | A 410 after a 301 leaves clients confused — the cached 301 says one thing, the live response another. |

**Trade-off for latency:**

301 lets the browser shortcut us entirely on every scan after the first — that's potentially 50–200 ms saved per scan (one fewer DNS lookup + TCP+TLS+HTTP round trip to our server). For a high-volume, never-changing redirect that's real money on a cloud bill. We pay that cost in exchange for keeping every scan observable and revocable.

**When you might pick 301 anyway:**

- The redirect genuinely won't change (a `bit.ly`-style "permanent" plan paid for accordingly).
- Analytics aren't a product requirement.
- You want SEO link-equity transferred from the short URL to the canonical destination — 301 transfers it, 302 doesn't.

A hybrid that's seen in production: serve 302 by default, but offer "promote to 301" as a one-time finalization. Our current API doesn't model this, but it would be a single boolean column on `url_mappings` and a branch in `redirect()`.

**Your take:**

---

## 4. URL Normalization

> What normalization rules do you need? Why is `http://Example.com/` and `https://example.com` potentially the same URL?

**Why those two strings *might* be the same destination:**

- **`http` vs `https`**: most servers in 2026 redirect 80 → 443 transparently. From a user's perspective the resolved page is the same.
- **`Example.com` vs `example.com`**: DNS is case-insensitive (RFC 4343). The browser will resolve both to the same IP, the same TLS cert, the same server.
- **Trailing slash on bare root**: `example.com` and `example.com/` are server-side equivalent for the root resource. RFC 3986 treats an empty path as semantically equal to `/`.

So three layers of normalization potentially apply.

**What we normalize (`app/url_validator.py`):**

| Rule | Behavior | Why |
|---|---|---|
| Lowercase scheme | `HTTPS://` → `https://` | RFC 3986 says scheme is case-insensitive. |
| Lowercase hostname | `Example.COM` → `example.com` | RFC 3986 says authority host is case-insensitive. |
| **Preserve path case** | `/User/Repo` stays as-is | RFC 3986 leaves path case-sensitive at the protocol level. S3 keys, GitHub raw URLs, JWT-in-path tokens all rely on this. |
| **Preserve query case** | `?Q=AbC` stays as-is | Same reasoning; signed query strings (S3 presigned, OAuth) would break if lowercased. |
| Drop bare-root `/` | `https://x.com/` → `https://x.com` | But only when no query/fragment follows — `https://x.com/?q=1` keeps the `/` so we never produce `https://x.com?q=1` which some servers reject. |
| **Do NOT** upgrade `http → https` | `http://example.com` stays http | Not every target speaks TLS on 443. Forced upgrade silently breaks legit redirects. Bit.ly and TinyURL don't do this. |

This is more conservative than the reference answer, which lowercases the entire URL and force-upgrades to https. [DECISIONS.md Stage 3](DECISIONS.md) documents why we deviated.

**The blocklist piece (also in this module):**

- Length cap at 2048 chars (matches browser address-bar limits).
- Scheme restricted to `http`/`https` — rejects `javascript:`, `data:`, `file:`, `ftp:`. This is the most important security gate; a `javascript:` URL in a QR code is an XSS vector against anyone who scans it.
- Hardcoded domain blocklist for known-bad hosts.

**Your take:**

---

## 5. Error Semantics

> What should happen when someone scans a deleted link vs a non-existent link? Should the HTTP status codes be different?

**Yes, different codes — and the difference matters.**

| Scenario | Status | Semantics |
|---|---|---|
| Token was created, then `DELETE`d | **410 Gone** | "This resource existed at this URI. It has been intentionally removed. Stop asking; update your bookmarks." |
| Token was created, then `expires_at` passed | **410 Gone** | Same intent — the resource was here, it's now permanently unavailable. |
| Token never existed | **404 Not Found** | "We have no record of this. Maybe you typed it wrong, maybe it's from a different system." |

**Why the distinction matters in practice:**

- **Caches and CDNs** treat 410 differently from 404. RFC 9110 §15.5.21 says 410 means "the resource is intentionally gone" and caches SHOULD treat the response as cacheable. 404 is "unknown" — caches won't necessarily commit to it.
- **Search engines** drop 410'd URLs from their index aggressively; 404'd URLs they retry on the assumption you might fix the typo. For a URL shortener, that's the difference between "this short link is dead and SEO removes the trail" vs "this short link looks broken and Google keeps probing for months."
- **Crawlers and analytics tools** make different decisions: a 410 sends the right signal to stop scanning periodically; a 404 doesn't.

For an end user staring at the browser, both look identical — a "this page doesn't work" screen. But for everything *between* the human and our server (caches, browsers' history dedup, link checkers, security scanners), the distinction is real and saves traffic.

**Operational implication:**

Once you commit to this distinction, you have to be honest about which 410 you mean. We return slightly different `detail` strings:

```
410 Gone — this link has been deleted
410 Gone — this link has expired
```

So a debugging human (or a log analyser) can tell the two apart, even though the status code is the same. A future enhancement could split them — `410` vs a custom `499`-ish — but that's over-engineering for the savings.

**Your take:**

---

## Cross-references

- All code-level deviations from the `answers/` reference and their reasoning → [DECISIONS.md](DECISIONS.md)
- Test suite covering each of the above → [tests/test_api.py](tests/test_api.py)
- Stage-by-stage commit log → [Git history](https://github.com/JShengP/qr-code-generator/commits/main)
