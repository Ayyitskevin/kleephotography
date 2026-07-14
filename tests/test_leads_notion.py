"""Inquiry → Notion Leads mirror (one-way WINDOW). Covers: the dry-run plan is
faithful (exact payload, zero writes), the sync stays dormant without config,
create-then-patch idempotency via the stamped page id, and both public intake
routes enqueueing the sync job. No network anywhere — Notion calls are
monkeypatched at the module seam (_create_page/_patch_page)."""

import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app import booking_notify, config, db, notion_sync
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def inquiry_id():
    iid = db.run(
        """INSERT INTO inquiries (name, email, business, message, service, shoot_date)
           VALUES (?,?,?,?,?,?)""",
        ("Test Lead", "lead@example.com", "Test Co", "Need photos", "Food & Beverage", None),
    )
    yield iid
    db.run("DELETE FROM inquiries WHERE id=?", (iid,))


@pytest.fixture()
def booking_id():
    db.run("DELETE FROM event_types WHERE slug='notion-observe-test'")
    eid = db.run(
        """INSERT INTO event_types
           (slug, name, duration_min, active, creates_notion_session)
           VALUES (?,?,?,?,?)""",
        ("notion-observe-test", "Observed Shoot", 60, 1, 1),
    )
    bid = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, phone, notes, start_utc, end_utc, tz)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "NotionObserve01",
            eid,
            "Observe Client",
            "notion-observe@example.com",
            "555-0101",
            "Keep this mocked.",
            "2027-02-01 15:00:00",
            "2027-02-01 16:00:00",
            "America/New_York",
        ),
    )
    yield bid
    db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
    db.run("DELETE FROM event_types WHERE id=?", (eid,))
    db.run("DELETE FROM inquiries WHERE email='notion-observe@example.com'")


def test_dry_run_builds_create_plan_without_writing(inquiry_id, monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "db-123")
    plan = notion_sync.sync_inquiry(inquiry_id, dry_run=True)
    assert plan["armed"] is True
    assert plan["action"] == "create"
    assert plan["target"] == "notion leads db db-123"
    p = plan["properties"]
    assert p["Name"]["title"][0]["text"]["content"] == "Test Lead"
    assert p["Email"]["email"] == "lead@example.com"
    assert p["Niche"]["select"]["name"] == "Food & Beverage"
    assert p["Status"]["select"]["name"] == "New"
    assert p["Mise ID"]["number"] == inquiry_id
    # Zero writes: no page id stamped, row untouched.
    row = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (inquiry_id,))
    assert row["notion_page_id"] is None


def test_dormant_without_config_is_a_quiet_noop(inquiry_id, monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "")
    assert notion_sync.sync_inquiry(inquiry_id) is None
    row = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (inquiry_id,))
    assert row["notion_page_id"] is None


def test_create_stamps_page_id_then_patches_status(inquiry_id, monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "db-123")
    created, patched = [], []
    monkeypatch.setattr(
        notion_sync, "_create_page", lambda db_id, props: created.append((db_id, props)) or "pg-1"
    )
    monkeypatch.setattr(
        notion_sync, "_patch_page", lambda page_id, props: patched.append((page_id, props))
    )

    notion_sync.sync_inquiry(inquiry_id)
    assert len(created) == 1 and created[0][0] == "db-123"
    row = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (inquiry_id,))
    assert row["notion_page_id"] == "pg-1"

    # Second sync after a dismiss patches Status on the SAME page — no duplicate.
    db.run("UPDATE inquiries SET dismissed_at=datetime('now') WHERE id=?", (inquiry_id,))
    notion_sync.sync_inquiry(inquiry_id)
    assert len(created) == 1
    assert patched == [("pg-1", {"Status": {"select": {"name": "Dismissed"}}})]


def test_booking_notion_create_update_and_session_reschedule_are_idempotent(
    booking_id, monkeypatch
):
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_BOOKINGS_DB", "bookings-db")
    monkeypatch.setattr(config, "NOTION_SESSIONS_DB", "sessions-db")
    created, patched = [], []

    def create_page(database_id, props):
        page_id = f"{database_id}-page-{len(created) + 1}"
        created.append((database_id, props, page_id))
        return page_id

    monkeypatch.setattr(notion_sync, "_create_page", create_page)
    monkeypatch.setattr(
        notion_sync, "_patch_page", lambda page_id, props: patched.append((page_id, props))
    )

    notion_sync.sync_booking(booking_id)
    notion_sync.sync_booking(booking_id)
    booking_page = db.one("SELECT notion_page_id FROM bookings WHERE id=?", (booking_id,))[
        "notion_page_id"
    ]
    assert booking_page == "bookings-db-page-1"
    assert [row[0] for row in created].count("bookings-db") == 1
    assert patched[-1][0] == booking_page
    assert patched[-1][1]["Status"]["select"]["name"] == "Confirmed"
    db.run("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    notion_sync.sync_booking(booking_id)
    assert patched[-1][1]["Status"]["select"]["name"] == "Cancelled"
    db.run("UPDATE bookings SET status='confirmed' WHERE id=?", (booking_id,))

    notion_sync.sync_session_for_booking(booking_id)
    notion_sync.sync_session_for_booking(booking_id)
    session_page = db.one("SELECT notion_session_id FROM bookings WHERE id=?", (booking_id,))[
        "notion_session_id"
    ]
    assert session_page == "sessions-db-page-2"
    assert [row[0] for row in created].count("sessions-db") == 1

    original = db.one("SELECT * FROM bookings WHERE id=?", (booking_id,))
    replacement = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, phone, notes, start_utc, end_utc,
            tz, reschedule_of)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "NotionObserve02",
            original["event_type_id"],
            original["name"],
            original["email"],
            original["phone"],
            original["notes"],
            "2027-02-02 15:00:00",
            "2027-02-02 16:00:00",
            original["tz"],
            booking_id,
        ),
    )
    notion_sync.sync_session_for_booking(replacement)
    linked = db.one("SELECT notion_session_id FROM bookings WHERE id=?", (replacement,))
    assert linked["notion_session_id"] == session_page
    assert [row[0] for row in created].count("sessions-db") == 1


def test_booking_mail_failure_generates_operator_signal(booking_id, monkeypatch):
    sent_alerts = []
    monkeypatch.setattr(booking_notify.mailer, "configured", lambda: True)
    monkeypatch.setattr(
        booking_notify.mailer,
        "send",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("smtp unavailable")),
    )
    monkeypatch.setattr(
        booking_notify.alerts,
        "ops_alert",
        lambda signature, text: sent_alerts.append((signature, text)),
    )
    monkeypatch.setattr(booking_notify, "_link_studio", lambda *args: None)
    monkeypatch.setattr(booking_notify.notion_sync, "sync_booking", lambda *args: None)
    monkeypatch.setattr(booking_notify.notion_sync, "sync_session_for_booking", lambda *args: None)
    monkeypatch.setattr(booking_notify.gcal, "on_booking_confirmed", lambda *args: None)

    booking_notify.confirm(booking_id)

    assert len(sent_alerts) == 2
    assert {signature.rsplit(":", 1)[-1] for signature, _ in sent_alerts} == {
        "client confirmation",
        "operator confirmation",
    }
    assert all("contact the client manually" in text for _, text in sent_alerts)


def test_missing_inquiry_fails_loud():
    with pytest.raises(ValueError):
        notion_sync.sync_inquiry(999999)


def _last_inquiry_job():
    return db.one("SELECT * FROM jobs WHERE kind='notion_sync_inquiry' ORDER BY id DESC LIMIT 1")


def _wipe(email: str) -> None:
    """Session db is shared with test_smoke's pristine-baseline assertions —
    leave zero rows behind (inquiries, their jobs, throttle bookkeeping)."""
    db.run("DELETE FROM jobs WHERE kind='notion_sync_inquiry'")
    db.run("DELETE FROM inquiries WHERE email=?", (email,))
    db.run("DELETE FROM pin_attempts")


def test_contact_post_enqueues_sync_job(client):
    try:
        r = client.post(
            "/contact",
            data={
                "name": "Wire Test",
                "email": "wire@example.com",
                "message": "Testing the lead wire.",
            },
        )
        assert r.status_code == 200
        inq = db.one("SELECT id FROM inquiries WHERE email='wire@example.com'")
        job = _last_inquiry_job()
        assert job is not None
        assert f'"inquiry_id": {inq["id"]}' in job["payload"]
    finally:
        _wipe("wire@example.com")


def test_lead_form_post_enqueues_sync_job(client):
    fid = db.run(
        "INSERT INTO forms (slug, title, kind, active) VALUES ('wire-test','Wire Test','lead',1)"
    )
    try:
        r = client.post(
            "/forms/wire-test",
            data={"name": "Form Wire", "email": "formwire@example.com"},
        )
        assert r.status_code == 200
        inq = db.one("SELECT id FROM inquiries WHERE email='formwire@example.com'")
        assert inq is not None
        job = _last_inquiry_job()
        assert f'"inquiry_id": {inq["id"]}' in job["payload"]
    finally:
        db.run("DELETE FROM form_submissions WHERE form_id=?", (fid,))
        db.run("DELETE FROM forms WHERE id=?", (fid,))
        _wipe("formwire@example.com")
