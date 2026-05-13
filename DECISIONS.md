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
