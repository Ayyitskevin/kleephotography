"""Prospect capture against disposable SQLite; no lifespan or background senders."""

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import config, db, ratelimit
from app.admin import prospects
from app.main import app

ROOT = "/admin/studio/prospects"
FORM = {
    "business": "Synthetic Bistro",
    "contact_name": "Test Chef",
    "email": "chef@prospect.example",
    "phone": "(828) 555-0123",
    "specialty": "fb",
    "source": "Synthetic website research",
    "qualification": "potential_fit",
    "notes": "A fixture, not a real lead.",
}


@pytest.fixture
def admin():
    ratelimit._hits.clear()
    client = TestClient(app)  # deliberately no lifespan or background workers
    assert client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield client
    client.close()
    db.run("DELETE FROM audit_log WHERE entity_type='prospect'")
    db.run("DELETE FROM prospects")


@pytest.fixture
def existing():
    rows = []

    def make(kind, **overrides):
        v = FORM | overrides
        if kind == "client":
            row_id = db.run(
                "INSERT INTO clients (name, company, email, phone) VALUES (?,?,?,?)",
                (v["contact_name"], v["business"], v["email"], v["phone"]),
            )
            rows.append(("clients", row_id))
        else:
            row_id = db.run(
                "INSERT INTO inquiries (name, business, email, phone, message, dismissed_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    v["contact_name"],
                    v["business"],
                    v["email"],
                    v["phone"],
                    "Fixture inquiry",
                    "2026-01-01" if kind == "archived" else None,
                ),
            )
            rows.append(("inquiries", row_id))
        return row_id

    yield make
    for table, row_id in rows:
        db.run(f"DELETE FROM {db.ident(table, {'clients', 'inquiries'})} WHERE id=?", (row_id,))


def counts():
    tables = (
        "clients",
        "inquiries",
        "projects",
        "invoices",
        "payments",
        "messages",
        "jobs",
        "emails_log",
    )
    return {t: db.one(f"SELECT COUNT(*) AS n FROM {db.ident(t, tables)}")["n"] for t in tables}


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(828) 555-0123", "18285550123"),
        ("+1 828-555-0123", "18285550123"),
        ("+44 20 7946 0958", "442079460958"),
        ("", ""),
    ],
)
def test_phone_identity(raw, expected):
    assert prospects._phone_key(raw) == expected


@pytest.mark.integration
def test_routes_require_admin_and_cross_origin_posts_are_rejected(admin):
    anonymous = TestClient(app)
    try:
        for url in (ROOT, ROOT + "/new", ROOT + "/1"):
            assert anonymous.get(url, follow_redirects=False).status_code == 303
        for url in (ROOT, ROOT + "/review", ROOT + "/1"):
            assert anonymous.post(url, data=FORM, follow_redirects=False).status_code == 303
        assert (
            admin.post(ROOT, data=FORM, headers={"Origin": "https://other.example"}).status_code
            == 403
        )
        assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0
    finally:
        anonymous.close()


@pytest.mark.integration
def test_review_is_read_only_then_save_creates_only_prospect_and_audit(admin):
    before = counts()
    response = admin.post(ROOT + "/review", data=FORM)
    assert response.status_code == 200
    assert "No exact matches" in response.text
    assert "Save prospect" in response.text
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='prospect'")["n"] == 0
    response = admin.post(ROOT, data=FORM, follow_redirects=False)
    assert response.status_code == 303
    row = db.one("SELECT * FROM prospects")
    assert {key: row[key] for key in FORM} == FORM
    assert row["email_key"] == "chef@prospect.example"
    assert row["phone_key"] == "18285550123"
    assert counts() == before
    event = db.one("SELECT * FROM audit_log WHERE entity_type='prospect'")
    assert event["entity_id"] == row["id"] and event["action"] == "create"
    assert json.loads(event["diff_json"]) == FORM
    page = admin.get(response.headers["location"])
    assert page.status_code == 200 and "Outbound prospect" in page.text
    assert "Synthetic Bistro" in admin.get(ROOT).text


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["prospect", "client", "inquiry", "archived"])
@pytest.mark.parametrize("match_by", ["email", "phone", "business"])
def test_duplicates_across_all_records_preserve_entries_and_write_nothing(
    admin, existing, kind, match_by
):
    if kind == "prospect":
        assert admin.post(ROOT, data=FORM, follow_redirects=False).status_code == 303
    else:
        existing(kind)
    other = FORM | {
        "business": "Different Bistro",
        "contact_name": "Another Chef",
        "email": "else@prospect.example",
        "phone": "8285550199",
    }
    if match_by == "business":
        other.update(business="  SYNTHETIC   bistro ", contact_name=" TEST   CHEF ")
    elif match_by == "email":
        other["email"] = " CHEF@PROSPECT.EXAMPLE "
    else:
        other["phone"] = "+1 828-555-0123"
    before = counts()
    n = db.one("SELECT COUNT(*) AS n FROM prospects")["n"]
    response = admin.post(ROOT, data=other, follow_redirects=False)
    assert response.status_code == 409
    assert "Review existing records" in response.text
    assert "not been saved" in response.text and "Synthetic website research" in response.text
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == n
    assert counts() == before
    if kind in {"inquiry", "archived"}:
        tab = "archived" if kind == "archived" else "all"
        assert f"/admin/inbox?tab={tab}&amp;sel=" in response.text


@pytest.mark.integration
def test_new_record_between_review_and_save_is_detected(admin, existing):
    assert "No exact matches" in admin.post(ROOT + "/review", data=FORM).text
    existing("inquiry")
    assert admin.post(ROOT, data=FORM).status_code == 409
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0


@pytest.mark.integration
def test_concurrent_double_submit_creates_one_record(admin):
    cookies = dict(admin.cookies)

    def save():
        client = TestClient(app)
        client.cookies.update(cookies)
        try:
            return client.post(ROOT, data=FORM, follow_redirects=False).status_code
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(), range(2)))
    assert sorted(results) == [303, 409]
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='prospect'")["n"] == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "override",
    [
        {"business": "   "},
        {"source": ""},
        {"email": "not-an-address"},
        {"phone": "8285550123 ext 9"},
        {"phone": "123"},
        {"specialty": "insurance"},
        {"qualification": "booked"},
        {"notes": "x" * 4001},
        {"business": "x" * 201},
    ],
)
def test_invalid_forms_do_not_write(admin, override):
    for route in (ROOT + "/review", ROOT):
        response = admin.post(route, data=FORM | override)
        assert response.status_code == 400
        assert 'role="alert"' in response.text
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0


@pytest.mark.integration
def test_research_can_start_without_contact_details_and_duplicate_is_still_blocked(admin):
    data = FORM | {
        "contact_name": "",
        "email": "",
        "phone": "",
        "specialty": "",
        "qualification": "unreviewed",
    }
    assert admin.post(ROOT, data=data, follow_redirects=False).status_code == 303
    row = db.one("SELECT * FROM prospects")
    assert row["email_key"] is None and row["phone_key"] is None
    assert admin.post(ROOT, data=data, follow_redirects=False).status_code == 409
    assert (
        admin.post(
            ROOT, data=data | {"business": "Another business"}, follow_redirects=False
        ).status_code
        == 303
    )


@pytest.mark.integration
def test_edit_qualification_after_inbound_arrives_and_block_identity_collision(admin, existing):
    location = admin.post(ROOT, data=FORM, follow_redirects=False).headers["location"]
    existing("inquiry")
    response = admin.post(
        location,
        data=FORM | {"qualification": "not_fit", "notes": "Not a match for this service."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.one("SELECT qualification FROM prospects")["qualification"] == "not_fit"
    assert db.one(
        "SELECT diff_json FROM audit_log WHERE entity_type='prospect' AND action='update'"
    )
    existing(
        "client",
        email="second@prospect.example",
        phone="",
        business="Another",
        contact_name="Other",
    )
    assert admin.post(location, data=FORM | {"email": "second@prospect.example"}).status_code == 409
    assert db.one("SELECT email FROM prospects")["email"] == FORM["email"]
    assert admin.get(ROOT + "/999999").status_code == 404
    assert admin.post(ROOT + "/999999", data=FORM).status_code == 404


@pytest.mark.integration
def test_html_is_escaped_and_gets_do_not_change_record(admin):
    data = FORM | {
        "business": "<script>alert(1)</script>",
        "notes": "</textarea><script>bad()</script>",
    }
    location = admin.post(ROOT, data=data, follow_redirects=False).headers["location"]
    before = dict(db.one("SELECT * FROM prospects"))
    for url in (location, ROOT, ROOT + "?q=%3Cscript%3E"):
        response = admin.get(url)
        assert response.status_code == 200
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "&lt;/textarea&gt;" in admin.get(location).text
    assert dict(db.one("SELECT * FROM prospects")) == before
    assert "No prospects match" in admin.get(ROOT + "?q=missing").text
    assert "No prospects match" in admin.get(ROOT + "?q=%25").text  # % is literal


@pytest.mark.integration
def test_capture_rolls_back_if_audit_fails(admin, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(prospects.audit, "log", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        admin.post(ROOT, data=FORM)
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0


@pytest.mark.integration
def test_database_rejects_duplicate_keys(admin):
    admin.post(ROOT, data=FORM)
    with pytest.raises(sqlite3.IntegrityError):
        db.run(
            "INSERT INTO prospects (business, source, business_key, contact_key, email_key) VALUES (?,?,?,?,?)",
            ("Another", "research", "another", "other", "chef@prospect.example"),
        )


@pytest.mark.integration
def test_migration_preserves_existing_records_and_reruns_safely(tmp_path, monkeypatch):
    original = db.MIGRATIONS_DIR
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for path in original.glob("*.sql"):
        if path.name < "084_":
            shutil.copy(path, old_migrations / path.name)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "upgrade.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old_migrations)
    db.migrate()
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Existing client",))
    before = tuple(db.one("SELECT * FROM clients WHERE id=?", (cid,)))
    monkeypatch.setattr(db, "MIGRATIONS_DIR", original)
    db.migrate()
    db.migrate()
    assert tuple(db.one("SELECT * FROM clients WHERE id=?", (cid,))) == before
    assert db.one("SELECT COUNT(*) AS n FROM prospects")["n"] == 0
    assert (
        db.one(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE name='084_outbound_prospects.sql'"
        )["n"]
        == 1
    )
