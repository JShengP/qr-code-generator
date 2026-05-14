"""Verify the audit_logs table records every mutation as expected.

These tests reach into the test DB via the dependency-overridden
session factory and assert on the rows directly — there's no public
API to read audit logs (yet), so we go through SQLAlchemy.
"""
from app.database import get_db
from app.main import app
from app.models import AuditLog, UrlMapping


def _audit_rows_for(client, token: str):
    """Return all audit_log rows for the mapping with the given public
    token, ordered by id (chronological)."""
    db_factory = app.dependency_overrides[get_db]
    db_gen = db_factory()
    db = next(db_gen)
    try:
        mapping = db.query(UrlMapping).filter(UrlMapping.token == token).first()
        if mapping is None:
            return []
        return (
            db.query(AuditLog)
            .filter(AuditLog.mapping_id == mapping.id)
            .order_by(AuditLog.id)
            .all()
        )
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


def test_create_writes_one_audit_row(client):
    r = client.post("/api/qr/create", json={"url": "https://example.com"})
    token = r.json()["token"]

    rows = _audit_rows_for(client, token)
    assert len(rows) == 1
    assert rows[0].action == "create"
    assert rows[0].before_value is None
    assert rows[0].after_value == "https://example.com"
    assert rows[0].user_id is not None  # client is auto-logged in


def test_patch_url_logs_before_and_after(client):
    token = client.post("/api/qr/create", json={"url": "https://before.example"}).json()[
        "token"
    ]
    client.patch(f"/api/qr/{token}", json={"url": "https://after.example"})

    rows = _audit_rows_for(client, token)
    actions = [r.action for r in rows]
    assert actions == ["create", "patch_url"]

    patch_row = rows[1]
    assert patch_row.before_value == "https://before.example"
    assert patch_row.after_value == "https://after.example"


def test_patch_expires_logs_iso_strings(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]
    client.patch(
        f"/api/qr/{token}",
        json={"expires_at": "2099-12-31T23:59:59"},
    )

    rows = _audit_rows_for(client, token)
    expires_row = next(r for r in rows if r.action == "patch_expires")
    assert expires_row.before_value is None  # never had an expires_at
    assert expires_row.after_value is not None
    assert "2099" in expires_row.after_value


def test_delete_logs_action(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]
    client.delete(f"/api/qr/{token}")

    rows = _audit_rows_for(client, token)
    actions = [r.action for r in rows]
    assert "delete" in actions
    delete_row = next(r for r in rows if r.action == "delete")
    # Delete records the action itself; no before/after value needed
    # (the mapping_id + action + timestamp is the full record).
    assert delete_row.before_value is None
    assert delete_row.after_value is None


def test_rotate_edit_token_logs_without_leaking_hashes(client):
    """Rotation is recorded, but neither the old nor new hash is
    written to the audit log — that would defeat the point of
    hashing them in the first place."""
    from tests.test_api import _drop_session, _auth

    token, edit_token, _ = (
        lambda r: (r.json()["token"], r.json()["edit_token"], r.json())
    )(client.post("/api/qr/create", json={"url": "https://example.com"}))

    # Use bearer path so we exercise rotate without owner shortcut.
    _drop_session(client)
    client.post(f"/api/qr/{token}/rotate-edit-token", headers=_auth(edit_token))

    rows = _audit_rows_for(client, token)
    rotate_row = next(r for r in rows if r.action == "rotate_edit_token")
    assert rotate_row.before_value is None
    assert rotate_row.after_value is None


def test_full_lifecycle_produces_complete_audit_trail(client):
    """One QR's whole story should be readable from the audit log."""
    token = client.post("/api/qr/create", json={"url": "https://step1.example"}).json()[
        "token"
    ]
    client.patch(f"/api/qr/{token}", json={"url": "https://step2.example"})
    client.patch(f"/api/qr/{token}", json={"url": "https://step3.example"})
    client.delete(f"/api/qr/{token}")

    rows = _audit_rows_for(client, token)
    timeline = [(r.action, r.before_value, r.after_value) for r in rows]
    assert timeline == [
        ("create", None, "https://step1.example"),
        ("patch_url", "https://step1.example", "https://step2.example"),
        ("patch_url", "https://step2.example", "https://step3.example"),
        ("delete", None, None),
    ]


def test_audit_log_records_ip_address(client):
    """Best-effort recording of the client IP for forensics."""
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]

    rows = _audit_rows_for(client, token)
    create_row = rows[0]
    # TestClient reports itself as "testclient" (no real IP), but
    # the field should be populated, not NULL.
    assert create_row.ip_address is not None


# ---------------------------------------------------------------------------
# GET /api/qr/{token}/audit — owner-only read endpoint
# ---------------------------------------------------------------------------


def _login(client, email_capture, email: str) -> None:
    """Helper duplicated from test_auth — keeps test_audit self-contained."""
    from urllib.parse import parse_qs, urlparse

    client.cookies.clear()
    client.post("/api/auth/request-link", json={"email": email})
    link = email_capture.sent[-1][1]
    token = parse_qs(urlparse(link).query)["token"][0]
    client.get(f"/api/auth/verify?token={token}", follow_redirects=False)


def test_audit_endpoint_owner_can_read_full_history(client):
    """Lifecycle: create -> patch_url -> delete should produce 3 entries
    that the owner can read in descending-by-time order."""
    create = client.post("/api/qr/create", json={"url": "https://step1.example"}).json()
    token = create["token"]
    client.patch(
        f"/api/qr/{token}", json={"url": "https://step2.example"}
    )
    client.delete(f"/api/qr/{token}")

    r = client.get(f"/api/qr/{token}/audit")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    # Newest first
    assert items[0]["action"] == "delete"
    assert items[1]["action"] == "patch_url"
    assert items[1]["before_value"] == "https://step1.example"
    assert items[1]["after_value"] == "https://step2.example"
    assert items[2]["action"] == "create"


def test_audit_endpoint_anonymous_returns_401(client):
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]
    client.cookies.clear()
    r = client.get(f"/api/qr/{token}/audit")
    assert r.status_code == 401
    assert "sign in" in r.json()["detail"].lower()


def test_audit_endpoint_non_owner_returns_403(client, email_capture):
    """A different signed-in user must get 403, not the data and not a
    404 (which would leak whether the token exists)."""
    token = client.post("/api/qr/create", json={"url": "https://owned.example"}).json()[
        "token"
    ]
    _login(client, email_capture, "stranger@example.com")
    r = client.get(f"/api/qr/{token}/audit")
    assert r.status_code == 403


def test_audit_endpoint_404_for_unknown_token(client):
    r = client.get("/api/qr/NOPE123/audit")
    assert r.status_code == 404


def test_audit_endpoint_readable_after_delete(client):
    """Deleting a QR doesn't remove the row (soft delete) and the
    audit trail must remain readable to the owner — that's the whole
    point of recording who-deleted-when."""
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]
    client.delete(f"/api/qr/{token}")

    # /api/qr/{token} (info endpoint) 404s after delete...
    assert client.get(f"/api/qr/{token}").status_code == 404
    # ...but audit still works for the owner.
    r = client.get(f"/api/qr/{token}/audit")
    assert r.status_code == 200
    actions = [e["action"] for e in r.json()["items"]]
    assert "create" in actions and "delete" in actions


def test_audit_endpoint_does_not_leak_ip_or_user_id(client):
    """The owner-facing schema deliberately omits ip_address and
    user_id — both are present in the DB but kept off the read API
    until we have a reason to expose them."""
    token = client.post("/api/qr/create", json={"url": "https://example.com"}).json()[
        "token"
    ]
    r = client.get(f"/api/qr/{token}/audit")
    body = r.json()["items"][0]
    assert "ip_address" not in body
    assert "user_id" not in body
    # Only these four keys.
    assert set(body.keys()) == {"action", "before_value", "after_value", "created_at"}
