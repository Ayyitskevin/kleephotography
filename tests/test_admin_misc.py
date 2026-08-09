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


@pytest.fixture
def gallery():
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("MiscG01", "Misc gallery", "1234"),
    )
    yield gid
    db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM galleries WHERE id=?", (gid,))


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


@pytest.mark.integration
def test_bulk_portfolio_rejects_a_non_numeric_asset_id(admin_client, gallery):
    aid = db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'hero.jpg', 'stored-hero.jpg', 'ready')""",
        (gallery,),
    )
    response = admin_client.post(
        f"/admin/galleries/{gallery}/assets/bulk-portfolio",
        data={"asset_ids": [str(aid), "not-an-id"], "portfolio_tag": "food"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    row = db.one("SELECT portfolio, portfolio_tag FROM assets WHERE id=?", (aid,))
    assert (row["portfolio"], row["portfolio_tag"]) == (0, None)


@pytest.mark.integration
def test_gallery_settings_rejects_a_non_iso_expiry_and_keeps_empty_as_never(admin_client, gallery):
    db.run("UPDATE galleries SET expires_at=? WHERE id=?", ("2035-01-05", gallery))
    form = {"title": "Misc gallery", "pin": "1234"}

    response = admin_client.post(
        f"/admin/galleries/{gallery}/settings",
        data={**form, "expires_at": "next Friday"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    row = db.one("SELECT expires_at FROM galleries WHERE id=?", (gallery,))
    assert row["expires_at"] == "2035-01-05"

    response = admin_client.post(
        f"/admin/galleries/{gallery}/settings",
        data={**form, "expires_at": "20350106"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.one("SELECT expires_at FROM galleries WHERE id=?", (gallery,))
    assert row["expires_at"] == "2035-01-06"

    response = admin_client.post(
        f"/admin/galleries/{gallery}/settings",
        data={**form, "expires_at": "  "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.one("SELECT expires_at FROM galleries WHERE id=?", (gallery,))
    assert row["expires_at"] is None
