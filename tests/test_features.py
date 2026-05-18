"""Tests for the six stretch features added on top of Stage 7:
302→301 promote, PNG download header, sidebar search/sort/include_deleted,
analytics date-range, soft-delete restore, bulk delete.

Each feature gets at least: happy path, one negative-auth case where
relevant, and one "next observable behavior" check (cache, audit log,
filtered list) so a regression in the wiring surfaces.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import routes as routes_module
from app.models import ScanEvent


def _create(client, url="https://example.com", **extra):
    payload = {"url": url, **extra}
    r = client.post("/api/qr/create", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------- promote 302 → 301 ---------------------------


def test_promote_to_301_succeeds_and_audit_records_it(client):
    data = _create(client)
    token = data["token"]
    r = client.patch(f"/api/qr/{token}", json={"redirect_status": 301})
    assert r.status_code == 200
    assert r.json()["redirect_status"] == 301

    audit = client.get(f"/api/qr/{token}/audit").json()["items"]
    actions = [e["action"] for e in audit]
    assert "promote_to_301" in actions
    promote = next(e for e in audit if e["action"] == "promote_to_301")
    assert promote["before_value"] == "302"
    assert promote["after_value"] == "301"


def test_redirect_returns_301_after_promotion(client):
    data = _create(client, "https://example.com")
    token = data["token"]
    client.patch(f"/api/qr/{token}", json={"redirect_status": 301})

    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://example.com"


def test_promote_invalidates_cache_so_next_scan_picks_up_301(client):
    data = _create(client, "https://example.com")
    token = data["token"]
    # Warm cache with a 302 scan first.
    first = client.get(f"/r/{token}", follow_redirects=False)
    assert first.status_code == 302

    client.patch(f"/api/qr/{token}", json={"redirect_status": 301})

    # Cache must have been evicted on PATCH; next scan reads the new status.
    after = client.get(f"/r/{token}", follow_redirects=False)
    assert after.status_code == 301


def test_302_redirect_carries_no_store_cache_control(client):
    """302 must not be cached by intermediaries — every scan needs to
    hit us so Update / Delete propagate and analytics record."""
    data = _create(client, "https://example.com")
    r = client.get(f"/r/{data['token']}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("cache-control", "").startswith("no-store")


def test_301_redirect_carries_bounded_cache_control(client):
    """301 must cap browser caching at 5 minutes (max-age=300) so a
    future destination change still propagates within a coffee break.
    The catastrophic alternative (default 301 = forever-cache) means
    a single mistake locks the QR's destination on every browser
    that ever scanned it."""
    data = _create(client, "https://example.com")
    client.patch(f"/api/qr/{data['token']}", json={"redirect_status": 301})

    r = client.get(f"/r/{data['token']}", follow_redirects=False)
    assert r.status_code == 301
    cache_control = r.headers.get("cache-control", "")
    assert "max-age=300" in cache_control
    assert "must-revalidate" in cache_control


def test_promote_to_302_rejected_at_schema_layer(client):
    data = _create(client)
    r = client.patch(f"/api/qr/{data['token']}", json={"redirect_status": 302})
    # Pydantic field validator rejects anything but 301.
    assert r.status_code == 422


def test_second_promotion_to_301_is_409_not_silent_noop(client):
    data = _create(client)
    token = data["token"]
    client.patch(f"/api/qr/{token}", json={"redirect_status": 301})
    r = client.patch(f"/api/qr/{token}", json={"redirect_status": 301})
    assert r.status_code == 409


# ----------------------- PNG download header -------------------------


def test_image_download_param_sets_attachment_header(client):
    data = _create(client)
    token = data["token"]

    r_inline = client.get(f"/api/qr/{token}/image")
    assert r_inline.status_code == 200
    assert "attachment" not in r_inline.headers.get("content-disposition", "")

    r_download = client.get(f"/api/qr/{token}/image?download=1")
    assert r_download.status_code == 200
    cd = r_download.headers["content-disposition"]
    assert cd.startswith("attachment")
    assert f'filename="qr-{token}.png"' in cd
    assert r_download.headers["content-type"] == "image/png"


# ----------------------- sidebar list filters ------------------------


def test_my_qrs_search_matches_token_or_destination(client):
    _create(client, "https://alpha.example.com")
    _create(client, "https://beta.example.com")
    _create(client, "https://gamma.example.com")

    r = client.get("/api/qr/mine?search=beta")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert "beta" in items[0]["original_url"]


def test_my_qrs_sort_destination_orders_alphabetically(client):
    _create(client, "https://gamma.example.com")
    _create(client, "https://alpha.example.com")
    _create(client, "https://beta.example.com")

    items = client.get("/api/qr/mine?sort=destination").json()["items"]
    urls = [i["original_url"] for i in items]
    assert urls == sorted(urls)


def test_my_qrs_include_deleted_surfaces_soft_deleted_rows(client):
    live = _create(client)
    dead = _create(client)
    client.delete(f"/api/qr/{dead['token']}")

    default_tokens = {i["token"] for i in client.get("/api/qr/mine").json()["items"]}
    assert live["token"] in default_tokens
    assert dead["token"] not in default_tokens

    inclusive = client.get("/api/qr/mine?include_deleted=1").json()["items"]
    inclusive_tokens = {i["token"] for i in inclusive}
    assert live["token"] in inclusive_tokens
    assert dead["token"] in inclusive_tokens

    dead_entry = next(i for i in inclusive if i["token"] == dead["token"])
    assert dead_entry["is_deleted"] is True
    assert dead_entry["deleted_at"] is not None


def test_my_qrs_sort_pattern_validates_input(client):
    r = client.get("/api/qr/mine?sort=lol")
    assert r.status_code == 422


# ----------------------- analytics date-range ------------------------


def _seed_scans(client, token: str, day_dates: list[datetime]) -> None:
    """Write ScanEvent rows directly so we control `scanned_at`. Bypasses
    the redirect-handler dedup/batching logic entirely.

    The conftest overrides `get_db` with its own generator that yields
    a Session from the in-memory engine; we drive that generator
    manually to get a real Session out, then close it via the same
    contract `Depends(get_db)` uses inside FastAPI.
    """
    from app.database import get_db
    gen = client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        for d in day_dates:
            db.add(ScanEvent(token=token, scanned_at=d, ip_address="1.1.1.1"))
        db.commit()
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_analytics_from_to_filter_scopes_total_and_chart(client):
    data = _create(client)
    token = data["token"]
    _seed_scans(client, token, [
        datetime(2025, 12, 1, 10),
        datetime(2025, 12, 2, 11),
        datetime(2025, 12, 5, 12),
        datetime(2026, 1, 10, 9),
    ])

    all_time = client.get(f"/api/qr/{token}/analytics").json()
    assert all_time["total_scans"] == 4

    december = client.get(
        f"/api/qr/{token}/analytics?from=2025-12-01&to=2025-12-31"
    ).json()
    assert december["total_scans"] == 3
    assert december["from"] == "2025-12-01"
    assert december["to"] == "2025-12-31"
    days = {row["date"] for row in december["scans_by_day"]}
    assert days == {"2025-12-01", "2025-12-02", "2025-12-05"}


def test_analytics_date_pattern_rejects_garbage(client):
    data = _create(client)
    r = client.get(f"/api/qr/{data['token']}/analytics?from=yesterday")
    assert r.status_code == 422


def test_analytics_from_after_to_is_422(client):
    data = _create(client)
    r = client.get(
        f"/api/qr/{data['token']}/analytics?from=2026-02-01&to=2026-01-01"
    )
    assert r.status_code == 422


def test_analytics_works_on_soft_deleted_row(client):
    data = _create(client)
    token = data["token"]
    _seed_scans(client, token, [datetime(2025, 12, 1, 10)])
    client.delete(f"/api/qr/{token}")

    r = client.get(f"/api/qr/{token}/analytics")
    assert r.status_code == 200
    assert r.json()["total_scans"] == 1


# ----------------------- restore -------------------------------------


def test_restore_unmarks_is_deleted_and_clears_deleted_at(client):
    data = _create(client)
    token = data["token"]
    client.delete(f"/api/qr/{token}")

    info = client.get(
        f"/api/qr/mine?include_deleted=1"
    ).json()["items"]
    dead = next(i for i in info if i["token"] == token)
    assert dead["is_deleted"] is True

    r = client.post(f"/api/qr/{token}/restore")
    assert r.status_code == 200
    body = r.json()
    assert body["is_deleted"] is False

    # Redirect resumes — was 410 before restore.
    assert client.get(f"/r/{token}", follow_redirects=False).status_code == 302


def test_restore_on_live_row_is_409(client):
    data = _create(client)
    r = client.post(f"/api/qr/{data['token']}/restore")
    assert r.status_code == 409


def test_restore_logs_audit_action(client):
    data = _create(client)
    token = data["token"]
    client.delete(f"/api/qr/{token}")
    client.post(f"/api/qr/{token}/restore")

    actions = [e["action"] for e in client.get(f"/api/qr/{token}/audit").json()["items"]]
    assert "restore" in actions


def test_restore_anonymous_is_401(client):
    data = _create(client)
    client.delete(f"/api/qr/{data['token']}")
    client.cookies.clear()
    r = client.post(f"/api/qr/{data['token']}/restore")
    assert r.status_code == 401


# ----------------------- bulk delete ---------------------------------


def test_bulk_delete_soft_deletes_owned_tokens(client):
    a = _create(client)["token"]
    b = _create(client)["token"]
    c = _create(client)["token"]

    r = client.post("/api/qr/bulk-delete", json={"tokens": [a, b]})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 2
    assert set(body["tokens"]) == {a, b}

    # Default view excludes deleted; only c survives.
    survivors = {i["token"] for i in client.get("/api/qr/mine").json()["items"]}
    assert survivors == {c}


def test_bulk_delete_all_or_nothing_on_unknown_token(client):
    a = _create(client)["token"]
    r = client.post(
        "/api/qr/bulk-delete",
        json={"tokens": [a, "nopenope"]},
    )
    assert r.status_code == 404
    # First token must NOT be deleted — the request rolled back.
    items = client.get("/api/qr/mine").json()["items"]
    assert any(i["token"] == a for i in items)


def test_bulk_delete_idempotent_on_already_deleted(client):
    a = _create(client)["token"]
    b = _create(client)["token"]
    client.delete(f"/api/qr/{a}")

    r = client.post("/api/qr/bulk-delete", json={"tokens": [a, b]})
    assert r.status_code == 200
    # Only b was newly deleted; a's deletion is a no-op repeat.
    assert r.json()["deleted"] == 1
    assert r.json()["tokens"] == [b]


def test_bulk_delete_size_cap_rejects_over_100(client):
    r = client.post(
        "/api/qr/bulk-delete",
        json={"tokens": [f"tok{i:03d}" for i in range(101)]},
    )
    assert r.status_code == 422


def test_bulk_delete_anonymous_is_401(client):
    a = _create(client)["token"]
    client.cookies.clear()
    r = client.post("/api/qr/bulk-delete", json={"tokens": [a]})
    assert r.status_code == 401


# ----------------------- existing-shape regressions ------------------


def test_create_response_now_includes_redirect_status_default_302(client):
    # QRInfoResponse and QRSummary both gained `redirect_status` — make
    # sure fresh rows surface the documented default.
    data = _create(client)
    info = client.get(f"/api/qr/{data['token']}").json()
    assert info["redirect_status"] == 302


def test_short_url_auto_derives_from_request_host_in_dev(client, monkeypatch):
    """When BASE_URL env var isn't set (the dev default), short_url
    should follow whatever host:port the user actually accessed —
    NOT the hardcoded `http://localhost:8000`. Otherwise a dev who
    runs uvicorn on port 8001 sees a short URL that points at
    nothing (the foot-gun that prompted this auto-derive)."""
    from app import routes as routes_module

    # Force the auto-derive path even if the test env happens to
    # have BASE_URL set globally.
    monkeypatch.setattr(routes_module, "BASE_URL_AUTO", True)

    r = client.post("/api/qr/create", json={"url": "https://example.com"})
    data = r.json()
    # TestClient hits the app at http://testserver/ — short_url MUST
    # encode that, not the hardcoded fallback.
    assert data["short_url"].startswith("http://testserver/r/")
    assert data["qr_code_url"].startswith("http://testserver/api/qr/")


def test_short_url_uses_explicit_base_url_when_configured(client, monkeypatch):
    """When BASE_URL is explicit (production path), routes must use
    that string verbatim — request.base_url is the internal proxy
    address, not the public domain."""
    from app import routes as routes_module

    monkeypatch.setattr(routes_module, "BASE_URL_AUTO", False)
    monkeypatch.setattr(routes_module, "BASE_URL", "https://qr.example.com")

    r = client.post("/api/qr/create", json={"url": "https://example.com"})
    data = r.json()
    assert data["short_url"].startswith("https://qr.example.com/r/")
    assert data["qr_code_url"].startswith("https://qr.example.com/api/qr/")


def test_delete_populates_deleted_at(client):
    data = _create(client)
    token = data["token"]
    client.delete(f"/api/qr/{token}")
    items = client.get("/api/qr/mine?include_deleted=1").json()["items"]
    dead = next(i for i in items if i["token"] == token)
    assert dead["deleted_at"] is not None
