"""Production-readiness spine: public inquiry → persist → email → Notion job
(idempotent stamp/patch) → failed-job admin recovery, plus focused regressions
for scheduling date-override determinism and gallery section ownership.

Drives shipped handlers (TestClient + jobs._execute + jobs.retry). Notion and
mail network are mocked at module seams only — never reimplemented here.
"""

import datetime as dt
import json
import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, mailer, notion_sync, scheduling, security
from app.gcal import FreeBusyQuery
from app.main import app

pytestmark = pytest.mark.integration

_UTC = dt.UTC


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(client):
    r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _wipe_lead(email: str) -> None:
    """Session DB is shared — leave zero lead/job/throttle residue."""
    db.run("DELETE FROM jobs WHERE kind IN ('notion_sync_inquiry','inquiry_owner_email')")
    db.run("DELETE FROM inquiries WHERE email=?", (email,))
    db.run(
        "DELETE FROM pin_attempts WHERE gallery_id=?",
        (security.INQUIRY_BUCKET_CONTACT,),
    )


def _last_inquiry_job():
    return db.one("SELECT * FROM jobs WHERE kind='notion_sync_inquiry' ORDER BY id DESC LIMIT 1")


# ── criterion 1: inquiry-to-lead path ────────────────────────────────────────


def test_contact_post_persists_lead_enqueues_job_and_thanks(client, monkeypatch):
    email = "reliability-wire@example.com"
    _wipe_lead(email)
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    sent = []
    monkeypatch.setattr(
        mailer, "send", lambda to, subject, body, reply_to="": sent.append((to, subject))
    )
    try:
        r = client.post(
            "/contact",
            data={
                "name": "Rel Wire",
                "email": email,
                "message": "Need a menu shoot next month.",
                "business": "Cafe Rel",
            },
        )
        assert r.status_code == 200
        assert "Thanks" in r.text
        assert 'data-analytics-view="Contact Success"' in r.text

        inq = db.one("SELECT * FROM inquiries WHERE email=?", (email,))
        assert inq is not None
        assert inq["name"] == "Rel Wire"
        assert inq["business"] == "Cafe Rel"
        assert "menu shoot" in inq["message"]
        # Owner notify is a durable job; visitor ack may already be in `sent`.
        assert inq["emailed"] == 0
        oe = db.one("SELECT * FROM jobs WHERE kind='inquiry_owner_email' ORDER BY id DESC LIMIT 1")
        assert oe is not None
        assert json.loads(oe["payload"]) == {"inquiry_id": inq["id"]}
        jobs._execute(oe["id"])
        inq = db.one("SELECT * FROM inquiries WHERE id=?", (inq["id"],))
        assert inq["emailed"] == 1
        assert ("kevin@example.com", "New inquiry — Rel Wire") in sent

        job = _last_inquiry_job()
        assert job is not None
        assert job["status"] in ("queued", "running", "done")
        payload = json.loads(job["payload"])
        assert payload == {"inquiry_id": inq["id"]}
    finally:
        _wipe_lead(email)


def test_contact_honeypot_and_throttle_do_not_write_leads(client, monkeypatch):
    email_bot = "reliability-bot@example.com"
    email_throttle = "reliability-throttle@example.com"
    _wipe_lead(email_bot)
    _wipe_lead(email_throttle)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: None)
    try:
        r = client.post(
            "/contact",
            data={
                "name": "Bot",
                "email": email_bot,
                "message": "spam",
                "website": "https://evil.example",
            },
        )
        assert r.status_code == 200 and "Thanks" in r.text
        assert db.one("SELECT COUNT(*) AS n FROM inquiries WHERE email=?", (email_bot,))["n"] == 0

        for i in range(3):
            r = client.post(
                "/contact",
                data={
                    "name": f"User{i}",
                    "email": f"reliability-ok{i}@example.com",
                    "message": "hello",
                },
            )
            assert r.status_code == 200, i
        r = client.post(
            "/contact",
            data={"name": "Blocked", "email": email_throttle, "message": "one more"},
        )
        assert r.status_code == 429
        assert "chance to reply" in r.text
        assert (
            db.one("SELECT COUNT(*) AS n FROM inquiries WHERE email=?", (email_throttle,))["n"] == 0
        )
    finally:
        for e in (
            email_bot,
            email_throttle,
            "reliability-ok0@example.com",
            "reliability-ok1@example.com",
            "reliability-ok2@example.com",
        ):
            _wipe_lead(e)


def test_contact_email_failure_still_stores_lead_and_enqueues_job(client, monkeypatch):
    email = "reliability-smtp-down@example.com"
    _wipe_lead(email)
    monkeypatch.setattr(mailer, "configured", lambda: True)

    def boom(*a, **k):
        raise OSError("smtp unavailable")

    monkeypatch.setattr(mailer, "send", boom)
    try:
        r = client.post(
            "/contact",
            data={"name": "Lee SMTP", "email": email, "message": "Portrait session?"},
        )
        assert r.status_code == 200 and "Thanks" in r.text
        inq = db.one("SELECT * FROM inquiries WHERE email=?", (email,))
        assert inq is not None
        assert inq["name"] == "Lee SMTP"
        assert inq["emailed"] == 0  # admin notify never landed
        job = _last_inquiry_job()
        assert job is not None
        assert json.loads(job["payload"]) == {"inquiry_id": inq["id"]}
    finally:
        _wipe_lead(email)


# ── criterion 2: Notion job idempotency + failed-job recovery ────────────────


def test_notion_sync_inquiry_job_stamps_then_patches_idempotently(monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "leads-db")
    created, patched = [], []

    def create_page(database_id, props):
        page_id = f"pg-{len(created) + 1}"
        created.append((database_id, props, page_id))
        return page_id

    monkeypatch.setattr(notion_sync, "_create_page", create_page)
    monkeypatch.setattr(
        notion_sync, "_patch_page", lambda page_id, props: patched.append((page_id, props))
    )

    iid = db.run(
        """INSERT INTO inquiries (name, email, message, service)
           VALUES (?,?,?,?)""",
        ("Idem Lead", "reliability-idem@example.com", "hello", "Food & Beverage"),
    )
    job_id = None
    try:
        job_id = jobs.enqueue("notion_sync_inquiry", {"inquiry_id": iid})
        jobs._execute(job_id)
        job = db.one("SELECT status, error FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "done", "error": None}
        row = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (iid,))
        assert row["notion_page_id"] == "pg-1"
        assert len(created) == 1 and created[0][0] == "leads-db"

        # Second run of the same handler with stamp present → patch only.
        job2 = jobs.enqueue("notion_sync_inquiry", {"inquiry_id": iid})
        jobs._execute(job2)
        assert len(created) == 1
        assert patched[-1][0] == "pg-1"
        assert patched[-1][1]["Status"]["select"]["name"] == "New"
        assert (
            db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (iid,))["notion_page_id"]
            == "pg-1"
        )
    finally:
        if job_id is not None:
            db.run("DELETE FROM jobs WHERE kind='notion_sync_inquiry'")
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_notion_sync_inquiry_race_keeps_first_stamp(monkeypatch):
    """Conditional stamp: a racing second create must not clobber the first page id."""
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "leads-db")
    created = []

    def create_page(database_id, props):
        page_id = f"race-pg-{len(created) + 1}"
        created.append(page_id)
        # Simulate a concurrent worker stamping the winner after our create
        # returned but before our conditional UPDATE.
        if len(created) == 1:
            db.run(
                "UPDATE inquiries SET notion_page_id=? WHERE id=?",
                ("race-winner", props["Mise ID"]["number"]),
            )
        return page_id

    monkeypatch.setattr(notion_sync, "_create_page", create_page)
    monkeypatch.setattr(notion_sync, "_patch_page", lambda *a, **k: None)

    iid = db.run(
        "INSERT INTO inquiries (name, email, message) VALUES (?,?,?)",
        ("Race Lead", "reliability-race@example.com", "race"),
    )
    try:
        notion_sync.sync_inquiry(iid)
        assert created == ["race-pg-1"]
        assert (
            db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (iid,))["notion_page_id"]
            == "race-winner"
        )
    finally:
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_failed_notion_sync_inquiry_surfaces_and_admin_retry_recovers(admin_client, monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "leads-db")

    def boom(*a, **k):
        raise RuntimeError("notion api down")

    monkeypatch.setattr(notion_sync, "_create_page", boom)
    monkeypatch.setattr(notion_sync, "_patch_page", boom)

    iid = db.run(
        "INSERT INTO inquiries (name, email, message) VALUES (?,?,?)",
        ("Fail Lead", "reliability-fail@example.com", "need help"),
    )
    job_id = None
    try:
        job_id = jobs.enqueue("notion_sync_inquiry", {"inquiry_id": iid})
        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            jobs._execute(job_id)
            job = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (job_id,))
            expected = "failed" if attempt == jobs.MAX_ATTEMPTS else "queued"
            assert job["status"] == expected
            assert job["attempts"] == attempt
            assert "notion api down" in (job["error"] or "")

        # Lead row is durable even when the mirror job hard-fails.
        assert (
            db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (iid,))["notion_page_id"]
            is None
        )
        assert db.one("SELECT email FROM inquiries WHERE id=?", (iid,))["email"] == (
            "reliability-fail@example.com"
        )

        page = admin_client.get("/admin/jobs")
        assert page.status_code == 200
        assert "notion_sync_inquiry" in page.text
        assert "notion api down" in page.text
        assert f"/admin/jobs/{job_id}/retry" in page.text

        # Only failed jobs are retryable via the shipped retry path.
        assert jobs.retry(job_id) is True
        job = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "queued", "attempts": 0, "error": None}
        assert jobs.retry(job_id) is False  # not failed anymore

        # Recover: create succeeds and stamps the page id.
        monkeypatch.setattr(notion_sync, "_create_page", lambda db_id, props: "pg-recovered")
        jobs._execute(job_id)
        job = db.one("SELECT status, error, attempts FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "done", "error": None, "attempts": 1}
        assert (
            db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (iid,))["notion_page_id"]
            == "pg-recovered"
        )

        # Admin HTTP retry of a non-failed job → 404 (real recovery path).
        r = admin_client.post(f"/admin/jobs/{job_id}/retry", follow_redirects=False)
        assert r.status_code == 404
    finally:
        if job_id is not None:
            db.run("DELETE FROM jobs WHERE id=?", (job_id,))
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


# ── criterion 3: scheduling overrides + gallery ownership ────────────────────


def test_global_and_event_scoped_date_overrides_are_deterministic(monkeypatch):
    day = dt.date(2036, 3, 10)
    ref = dt.datetime(2036, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    eid = db.run(
        """INSERT INTO event_types
           (slug, name, duration_min, slot_step_min, min_notice_hours,
            booking_window_days, active)
           VALUES (?,?,?,?,?,?,1)""",
        ("rel-override", "Rel Override", 60, 60, 0, 365),
    )
    db.run(
        """INSERT INTO availability_rules
           (event_type_id, weekday, start_min, end_min) VALUES (?,?,?,?)""",
        (eid, day.weekday(), 540, 780),
    )
    try:
        et = scheduling.event_by_slug("rel-override")
        open_slots = scheduling.slots_for_day(et, day)
        assert len(open_slots) >= 1

        # Global block rejects every booking on that day.
        db.run(
            "INSERT INTO date_overrides (event_type_id, day, available) VALUES (NULL,?,0)",
            (day.isoformat(),),
        )
        assert scheduling.slots_for_day(et, day) == []
        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(et, "2036-03-10 14:00:00", "Ada", "ada-rel@example.com", "", "", "UTC")

        # Event-scoped custom hours beat the global block (ORDER BY event_type_id
        # IS NULL — event-specific preferred).
        db.run(
            """INSERT INTO date_overrides
               (event_type_id, day, available, start_min, end_min)
               VALUES (?,?,1,720,780)""",
            (eid, day.isoformat()),
        )
        slots = scheduling.slots_for_day(et, day)
        # 12:00 America/New_York on 2036-03-10 is EDT (UTC-4) → 16:00 UTC
        assert [s["utc"] for s in slots] == ["2036-03-10 16:00:00"]

        # Blocked event-scoped override (available=0) rejects book + empty slots.
        db.run("DELETE FROM date_overrides WHERE event_type_id=?", (eid,))
        db.run(
            "INSERT INTO date_overrides (event_type_id, day, available) VALUES (?,?,0)",
            (eid, day.isoformat()),
        )
        assert scheduling.slots_for_day(et, day) == []
        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(et, "2036-03-10 14:00:00", "Ada", "ada-rel@example.com", "", "", "UTC")
    finally:
        db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM date_overrides WHERE event_type_id=? OR day=?", (eid, day.isoformat()))
        db.run("DELETE FROM availability_rules WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_gallery_section_assignment_rejects_cross_gallery_without_side_effects(admin_client):
    """Asset/section assignment must refuse foreign or missing sections; no write."""
    admin_client.post(
        "/admin/galleries",
        data={"title": "Rel Owner Target", "client_name": ""},
        follow_redirects=False,
    )
    target = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin_client.post(
        "/admin/galleries",
        data={"title": "Rel Owner Foreign", "client_name": ""},
        follow_redirects=False,
    )
    foreign = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    target_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (target["id"],),
    )
    foreign_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (foreign["id"],),
    )
    missing_section_id = db.one("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM sections")["id"]
    asset_id = db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, stored) VALUES (?,?,?,?,?)",
        (target["id"], target_section["id"], "photo", "rel-owned.jpg", "rel-owned.jpg"),
    )
    foreign_asset_id = db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, stored) VALUES (?,?,?,?,?)",
        (foreign["id"], foreign_section["id"], "photo", "rel-foreign.jpg", "rel-foreign.jpg"),
    )
    try:
        for invalid in (foreign_section["id"], missing_section_id, 0):
            r = admin_client.post(
                f"/admin/galleries/{target['id']}/assets/{asset_id}/section",
                data={"section_id": str(invalid)},
                follow_redirects=False,
            )
            assert r.status_code == 400
            assert r.json()["detail"] == "unknown section"
            assert (
                db.one("SELECT section_id FROM assets WHERE id=?", (asset_id,))["section_id"]
                == target_section["id"]
            )

            r = admin_client.post(
                f"/admin/galleries/{target['id']}/assets/bulk-section",
                data={"section_id": str(invalid), "asset_ids": [str(asset_id)]},
                follow_redirects=False,
            )
            assert r.status_code == 400
            assert r.json()["detail"] == "unknown section"
            assert (
                db.one("SELECT section_id FROM assets WHERE id=?", (asset_id,))["section_id"]
                == target_section["id"]
            )

        # Forged path with foreign asset id must not reassign that asset.
        r = admin_client.post(
            f"/admin/galleries/{target['id']}/assets/{foreign_asset_id}/section",
            data={"section_id": str(target_section["id"])},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert (
            db.one("SELECT section_id FROM assets WHERE id=?", (foreign_asset_id,))["section_id"]
            == foreign_section["id"]
        )
    finally:
        db.run("DELETE FROM assets WHERE id IN (?,?)", (asset_id, foreign_asset_id))
        db.run("DELETE FROM sections WHERE gallery_id IN (?,?)", (target["id"], foreign["id"]))
        db.run("DELETE FROM galleries WHERE id IN (?,?)", (target["id"], foreign["id"]))
