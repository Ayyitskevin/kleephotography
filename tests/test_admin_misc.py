"""Admin odds and ends: audit export, gallery input validation, tasks board."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"password": "test-pw"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


@pytest.mark.integration
def test_audit_csv_exports_past_the_viewer_window(admin_client):
    """The CSV is the dispute/accountant evidence file: it carries the whole
    append-only trail, not the 500 newest rows the on-screen viewer shows."""
    marker = "csv_export_probe"
    seeded = 501
    try:
        con = db.connect()
        try:
            con.executemany(
                "INSERT INTO audit_log (entity_type, entity_id, action, actor) VALUES (?,?,?,?)",
                [(marker, n, "create", "admin") for n in range(seeded)],
            )
            con.commit()
        finally:
            con.close()

        response = admin_client.get("/admin/audit.csv")
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0] == ["Time", "Event", "Detail", "Amount", "Actor"]
        probe_rows = [r for r in rows[1:] if marker.replace("_", " ") in r[2]]
        assert len(probe_rows) == seeded

        total = db.one("SELECT COUNT(*) AS n FROM audit_log")["n"]
        assert len(rows) - 1 == total

        # The viewer keeps its window — that cap is deliberate.
        page = admin_client.get("/admin/audit")
        assert page.status_code == 200
        assert page.text.count("audit-ic") <= 500
    finally:
        db.run("DELETE FROM audit_log WHERE entity_type=?", (marker,))
