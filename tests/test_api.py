"""End-to-end API tests covering PROMPT.md scenarios plus regressions
for the Stage 2–4 deviations from the reference answer.

These exercise the live FastAPI app via TestClient — no separate server
needed. Run with: pytest -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# PROMPT.md scenarios — these mirror the 8 curl commands in the spec.
# ---------------------------------------------------------------------------


def test_create_returns_token_and_links(client):
    r = client.post("/api/qr/create", json={"url": "https://example.com"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["token"]) == 7
    assert data["short_url"].endswith(f"/r/{data['token']}")
    assert data["qr_code_url"].endswith(f"/api/qr/{data['token']}/image")
    assert data["original_url"] == "https://example.com"


def test_redirect_302_to_original_url(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()["token"]
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com"


def test_get_qr_info_returns_metadata(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()["token"]
    r = client.get(f"/api/qr/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["token"] == token
    assert body["original_url"] == "https://example.com"
    assert body["is_deleted"] is False
    assert body["expires_at"] is None


def test_patch_url_changes_redirect_target(client):
    token = client.post("/api/qr/create", json={"url": "https://old.example.com"}).json()["token"]
    r = client.patch(f"/api/qr/{token}", json={"url": "https://new.example.com"})
    assert r.status_code == 200
    assert r.json()["original_url"] == "https://new.example.com"

    r2 = client.get(f"/r/{token}", follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"] == "https://new.example.com"


def test_delete_then_redirect_410(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()["token"]
    assert client.delete(f"/api/qr/{token}").status_code == 200

    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 410


def test_redirect_unknown_token_404(client):
    r = client.get("/r/NOPE123", follow_redirects=False)
    assert r.status_code == 404


def test_image_returns_png(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()["token"]
    r = client.get(f"/api/qr/{token}/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG magic bytes
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_analytics_counts_scans(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()["token"]
    for _ in range(3):
        client.get(f"/r/{token}", follow_redirects=False)
    r = client.get(f"/api/qr/{token}/analytics")
    assert r.status_code == 200
    body = r.json()
    assert body["token"] == token
    assert body["total_scans"] == 3


# ---------------------------------------------------------------------------
# Regressions for Stage 3 — URL normalization deviations from answers/.
# ---------------------------------------------------------------------------


def test_normalization_lowercases_scheme_and_host(client):
    r = client.post("/api/qr/create", json={"url": "HTTPS://Example.COM/Hello"})
    assert r.json()["original_url"] == "https://example.com/Hello"


def test_normalization_preserves_path_case(client):
    """Reference would mangle GitHub URLs by lowercasing the path. We don't."""
    r = client.post("/api/qr/create", json={"url": "https://github.com/User/Repo"})
    assert r.json()["original_url"] == "https://github.com/User/Repo"


def test_normalization_keeps_query_case(client):
    r = client.post("/api/qr/create", json={"url": "https://x.com/?Q=AbC"})
    assert r.json()["original_url"] == "https://x.com/?Q=AbC"


def test_no_http_to_https_upgrade(client):
    """Reference force-upgrades; we honour user intent (some targets don't speak TLS)."""
    r = client.post("/api/qr/create", json={"url": "http://example.com"})
    assert r.json()["original_url"] == "http://example.com"


def test_root_trailing_slash_collapses_only_when_no_query(client):
    """Bare root '/' is dropped; with a query the '/' is kept to avoid `x.com?q=1`."""
    a = client.post("/api/qr/create", json={"url": "https://x.com/"}).json()
    assert a["original_url"] == "https://x.com"

    b = client.post("/api/qr/create", json={"url": "https://x.com/?q=1"}).json()
    assert b["original_url"] == "https://x.com/?q=1"


@pytest.mark.parametrize(
    "bad_url",
    ["ftp://example.com", "javascript:alert(1)", "not a url"],
)
def test_invalid_scheme_rejected(client, bad_url):
    r = client.post("/api/qr/create", json={"url": bad_url})
    assert r.status_code == 422


def test_blocked_domain_rejected(client):
    r = client.post("/api/qr/create", json={"url": "https://evil.com"})
    assert r.status_code == 422


def test_overlength_url_rejected(client):
    long_url = "https://x.com/" + "a" * 3000
    r = client.post("/api/qr/create", json={"url": long_url})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Regressions for Stage 4 — TTL-aware cache + tz coercion.
# ---------------------------------------------------------------------------


def test_expired_link_returns_410_via_db(client):
    """expires_at set in the past must yield 410 on redirect."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    token = client.post(
        "/api/qr/create",
        json={"url": "https://example.com", "expires_at": past},
    ).json()["token"]

    # First redirect: cache miss (just created with past expiry warms cache,
    # but our redirect handler still sees the past expiry on the cache hit
    # and falls through to the DB path which 410s).
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 410


def test_tz_aware_iso_z_suffix_does_not_crash(client):
    """Regression: Pydantic parses `2026-12-31T23:59:59Z` as tz-aware.

    Reference code passes it straight to the model and the redirect path
    then crashes with `TypeError: can't compare offset-naive and
    offset-aware datetimes`. Our `_to_naive_utc` coercion fixes this.
    """
    r = client.post(
        "/api/qr/create",
        json={"url": "https://example.com", "expires_at": "2099-12-31T23:59:59Z"},
    )
    assert r.status_code == 200
    token = r.json()["token"]

    r2 = client.get(f"/r/{token}", follow_redirects=False)
    assert r2.status_code == 302


def test_cache_invalidated_on_patch(client):
    """PATCH must evict cache so the next redirect sees the new URL."""
    token = client.post("/api/qr/create", json={"url": "https://a.com"}).json()["token"]
    # Warm cache
    client.get(f"/r/{token}", follow_redirects=False)
    # Update
    client.patch(f"/api/qr/{token}", json={"url": "https://b.com"})
    # Next redirect must show new URL (cache must have been invalidated)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.headers["location"] == "https://b.com"


def test_cache_invalidated_on_delete(client):
    """DELETE must evict cache so subsequent redirect 410s instead of serving stale."""
    token = client.post("/api/qr/create", json={"url": "https://a.com"}).json()["token"]
    client.get(f"/r/{token}", follow_redirects=False)  # warm cache
    client.delete(f"/api/qr/{token}")
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# Static front-end — mount at "/" must not shadow the API.
# ---------------------------------------------------------------------------


def test_index_html_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "create-form" in r.text


def test_unknown_static_path_returns_404(client):
    """The StaticFiles catch-all must 404 paths it doesn't have."""
    r = client.get("/this-file-does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Stage 7 — rate limit on /api/qr/create.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stage 9 (post-review) — SSRF / CRLF / userinfo / subdomain blocking.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "internal_url",
    [
        "http://127.0.0.1/admin",
        "http://127.1.2.3/",
        "http://[::1]/",
        "http://10.0.0.5:8080/",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # AWS / Azure metadata
        "http://0.0.0.0/",
    ],
)
def test_internal_ip_hosts_rejected(client, internal_url):
    """SSRF surface: literal IPs that point at our own network must be 422."""
    r = client.post("/api/qr/create", json={"url": internal_url})
    assert r.status_code == 422, f"expected 422 for {internal_url}, got {r.status_code}"


@pytest.mark.parametrize(
    "name_url",
    ["http://localhost/", "http://localhost:6379/", "http://metadata.google.internal/"],
)
def test_internal_hostnames_rejected(client, name_url):
    """SSRF surface: well-known internal hostnames must be 422 by name."""
    r = client.post("/api/qr/create", json={"url": name_url})
    assert r.status_code == 422


def test_blocklist_matches_subdomains(client):
    """Subdomains of a blocked registrable domain must also be blocked."""
    for url in [
        "https://login.evil.com",
        "https://a.b.evil.com",
        "https://www.evil.com/path",
    ]:
        r = client.post("/api/qr/create", json={"url": url})
        assert r.status_code == 422, f"expected 422 for {url}"


def test_blocklist_case_insensitive(client):
    """Hostname comparison must be case-insensitive."""
    r = client.post("/api/qr/create", json={"url": "https://EVIL.com/x"})
    assert r.status_code == 422


def test_userinfo_in_url_rejected(client):
    """`user:pass@host` is a phishing primitive — reject outright."""
    for url in [
        "https://google.com@attacker.com/",
        "https://admin:secret@example.com/",
    ]:
        r = client.post("/api/qr/create", json={"url": url})
        assert r.status_code == 422


def test_crlf_in_url_rejected(client):
    """CRLF must not be smuggled into the URL — it would land in Location."""
    for url in [
        "https://example.com/foo\r\nX-Injected: yes",
        "https://example.com/\nfoo",
        "https://example.com/\tfoo",
    ]:
        r = client.post("/api/qr/create", json={"url": url})
        assert r.status_code == 422, f"expected 422 for {url!r}"


def test_create_rate_limited_after_n_requests(rate_limited_client):
    """11th create within a minute from the same IP should be 429.

    The limit is 10/minute. We loop 10 OK requests, the 11th must 429.
    """
    for i in range(10):
        r = rate_limited_client.post(
            "/api/qr/create", json={"url": f"https://example{i}.com"}
        )
        assert r.status_code == 200, f"req {i} unexpectedly failed: {r.status_code}"

    r = rate_limited_client.post(
        "/api/qr/create", json={"url": "https://example-overflow.com"}
    )
    assert r.status_code == 429
