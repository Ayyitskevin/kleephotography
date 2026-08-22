"""Admin odds and ends: audit export, gallery input validation, tasks board."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db
from app.admin import tasks as admin_tasks
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


@pytest.mark.integration
def test_transfer_settings_round_trip_the_require_pin_toggle(admin_client):
    """create_transfer chooses require_pin once; the settings form is the only
    place to change it afterwards. Checkbox off posts nothing → 0, on → 1."""
    create = admin_client.post(
        "/admin/transfers",
        data={"title": "Toggle drop", "require_pin": "true"},
        follow_redirects=False,
    )
    assert create.status_code == 303
    gid = int(create.headers["location"].rsplit("/", 1)[-1])
    try:
        page = admin_client.get(f"/admin/galleries/{gid}")
        assert page.status_code == 200
        assert 'name="require_pin"' in page.text

        row = db.one("SELECT pin, require_pin FROM galleries WHERE id=?", (gid,))
        assert row["require_pin"] == 1
        form = {"title": "Toggle drop", "pin": row["pin"]}

        off = admin_client.post(
            f"/admin/galleries/{gid}/settings", data=form, follow_redirects=False
        )
        assert off.status_code == 303
        assert db.one("SELECT require_pin FROM galleries WHERE id=?", (gid,))["require_pin"] == 0

        on = admin_client.post(
            f"/admin/galleries/{gid}/settings",
            data={**form, "require_pin": "true"},
            follow_redirects=False,
        )
        assert on.status_code == 303
        assert db.one("SELECT require_pin FROM galleries WHERE id=?", (gid,))["require_pin"] == 1
    finally:
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


@pytest.mark.integration
def test_gallery_settings_leave_require_pin_alone(admin_client, gallery):
    """Regular galleries render no require_pin control (their PIN gate is
    unconditional), so a routine settings save must not zero the stored 1."""
    page = admin_client.get(f"/admin/galleries/{gallery}")
    assert page.status_code == 200
    assert 'name="require_pin"' not in page.text

    saved = admin_client.post(
        f"/admin/galleries/{gallery}/settings",
        data={"title": "Misc gallery", "pin": "1234"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert db.one("SELECT require_pin FROM galleries WHERE id=?", (gallery,))["require_pin"] == 1


@pytest.mark.integration
def test_task_board_offers_a_confirm_guarded_delete(admin_client):
    tid = db.run("INSERT INTO tasks (title) VALUES (?)", ("Deletable board task",))
    try:
        page = admin_client.get("/admin/tasks")
        assert page.status_code == 200
        assert f'formaction="/admin/tasks/{tid}/delete"' in page.text
        card = page.text.split("Deletable board task")[1]
        assert "data-confirm=" in card.split("</form>")[0]

        response = admin_client.post(f"/admin/tasks/{tid}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert db.one("SELECT id FROM tasks WHERE id=?", (tid,)) is None
        assert "Deletable board task" not in admin_client.get("/admin/tasks").text
    finally:
        db.run("DELETE FROM tasks WHERE id=?", (tid,))


@pytest.mark.integration
def test_content_caption_card_counts_past_the_rendered_window(admin_client):
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Caption Card Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Caption retainer"))
    plan_id = db.run(
        "INSERT INTO recurring_plans (project_id, title) VALUES (?,?)", (pid, "Monthly captions")
    )
    try:
        con = db.connect()
        try:
            # Approved packs are the oldest, so the newest-60 window would miss
            # them entirely and the split would read 0 approved.
            con.executemany(
                """INSERT INTO retainer_captions (plan_id, period, label, body, status)
                   VALUES (?,?,?,?,?)""",
                [
                    (plan_id, f"2035-{n // 12 + 1:02d}", "IG", f"caption {n}", status)
                    for n, status in enumerate(["approved"] * 3 + ["draft"] * 58)
                ],
            )
            con.commit()
        finally:
            con.close()

        page = admin_client.get(f"/admin/content?client={cid}")
        assert page.status_code == 200
        assert '<div class="fin-card-val">61</div>' in page.text
        assert '<span class="fin-card-sub">3 approved · 58 draft</span>' in page.text
        assert "Newest 60 of 61" in page.text
    finally:
        db.run("DELETE FROM retainer_captions WHERE plan_id=?", (plan_id,))
        db.run("DELETE FROM recurring_plans WHERE id=?", (plan_id,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


@pytest.mark.integration
def test_bench_stats_split_photos_from_films(admin_client, gallery):
    for kind, name in (("photo", "one.jpg"), ("photo", "two.jpg"), ("video", "reel.mp4")):
        db.run(
            """INSERT INTO assets (gallery_id, kind, filename, stored, status)
               VALUES (?,?,?,?, 'ready')""",
            (gallery, kind, name, f"stored-{name}"),
        )
    page = admin_client.get(f"/admin/galleries/{gallery}")
    assert page.status_code == 200
    assert (
        '<span class="gd-stat-n">2</span><span class="gd-stat-l">Photos &middot; 1 film</span>'
        in page.text
    )


@pytest.mark.integration
def test_calendar_month_query_keeps_the_local_day_edge_booking(admin_client, monkeypatch):
    """The month band is UTC and the grid is local: 2035-08-01 01:00 UTC is
    still July 31 in America/New_York, so the guard band must fetch it while
    a booking three months out never leaves SQLite."""
    event_id = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        ("cal-band-consult", "Consult", 30),
    )
    edge_utc, far_utc = "2035-08-01 01:00:00", "2035-10-05 15:00:00"
    try:
        for token, name, start in (
            ("cal-band-edge", "Edge Booking", edge_utc),
            ("cal-band-far", "Far Booking", far_utc),
        ):
            db.run(
                """INSERT INTO bookings (token, event_type_id, name, email, start_utc, end_utc)
                   VALUES (?,?,?,?,?,?)""",
                (token, event_id, name, "edge@example.com", start, start),
            )

        fetched: list[str] = []
        real_all = db.all_

        def spy(sql, params=()):
            rows = real_all(sql, params)
            if "FROM bookings" in sql:
                fetched.extend(r["start_utc"] for r in rows)
            return rows

        monkeypatch.setattr(admin_tasks.db, "all_", spy)
        page = admin_client.get("/admin/calendar?year=2035&month=7")
        assert page.status_code == 200
        assert "21:00 Consult · Edge Booking" in page.text
        assert fetched == [edge_utc]
    finally:
        db.run("DELETE FROM bookings WHERE event_type_id=?", (event_id,))
        db.run("DELETE FROM event_types WHERE id=?", (event_id,))
