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
