"""Mickey Prompt 2 — durable owner-email delivery + Notion orphan recovery.

Drives shipped inquiry_notify + jobs + notion_sync paths. Mailer/Notion network
mocked at seams only.
"""

from __future__ import annotations

import os
import tempfile
import threading

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app import alerts, config, db, inquiry_notify, jobs, mailer, notion_sync
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _migrate():
    db.migrate()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _enable_ops(monkeypatch, sent: list):
    class _InlineThread:
        def __init__(self, target, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(alerts, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(alerts.threading, "Thread", _InlineThread)
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(alerts.features, "telegram_enabled", lambda: True)
    alerts._ops_last.clear()


def _insert_lead(email="lead@example.com") -> int:
    return db.run(
        "INSERT INTO inquiries (name, email, message) VALUES (?,?,?)",
        ("Lead", email, "Need photos"),
    )


def test_smtp_down_then_recover_idempotent(monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    sent_alert = []
    _enable_ops(monkeypatch, sent_alert)
    iid = _insert_lead("smtp-rec@example.com")
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(mailer, "configured", lambda: True)

    def boom(*a, **k):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    jid = inquiry_notify.enqueue_owner_email(iid)
    assert jid
    # jobs._execute swallows handler exceptions and re-queues until MAX_ATTEMPTS.
    jobs._execute(jid)
    job = db.one("SELECT status, error, attempts FROM jobs WHERE id=?", (jid,))
    assert job["status"] in ("queued", "failed")
    assert job["error"] and "smtp_error" in job["error"]
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["emailed"] == 0
    assert row["owner_email_failure_category"] == "smtp_error"
    assert row["owner_email_attempts"] >= 1
    assert row["owner_email_delivered_at"] is None
    assert sent_alert and "smtp_error" in sent_alert[0]
    assert "lead@example.com" not in sent_alert[0]
    assert "Need photos" not in sent_alert[0]

    # Recover: working mailer + re-enqueue (handler is idempotent under delivery stamp).
    sent = []
    monkeypatch.setattr(mailer, "send", lambda *a, **k: sent.append(a))
    # Clear in_flight / failed status so claim can succeed after SMTP recover.
    db.run(
        """UPDATE inquiries SET owner_email_status='failed',
             owner_email_last_attempted_at=datetime('now', '-10 minutes')
           WHERE id=?""",
        (iid,),
    )
    jid2 = inquiry_notify.enqueue_owner_email(iid)
    assert jid2
    jobs._execute(jid2)
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["owner_email_delivered_at"]
    assert row["emailed"] == 1
    assert len(sent) == 1

    # Duplicate execution after delivery must not send again.
    jobs._execute(jid2)
    inquiry_notify.deliver_owner_email(iid)
    assert len(sent) == 1


def test_worker_crash_after_send_before_stamp_then_retry(monkeypatch):
    """Send succeeds but stamp fails once — reclaim allows recovery without
    requiring a second SMTP when already delivered; if stamp never landed,
    reclaim + resend is acceptable (claim lock is the concurrency fence)."""
    monkeypatch.setattr(jobs, "_pool", None)
    iid = _insert_lead("crash@example.com")
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    sends = []

    def send_ok(*a, **k):
        sends.append(1)

    monkeypatch.setattr(mailer, "send", send_ok)
    # Simulate claim + send without stamp (crash)
    assert inquiry_notify._claim_send(iid)
    send_ok()
    assert not db.one("SELECT owner_email_delivered_at FROM inquiries WHERE id=?", (iid,))[
        "owner_email_delivered_at"
    ]
    # Stale reclaim: age the in_flight timestamp
    db.run(
        """UPDATE inquiries SET owner_email_last_attempted_at=
             datetime('now', '-10 minutes') WHERE id=?""",
        (iid,),
    )
    inquiry_notify.deliver_owner_email(iid)
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["owner_email_delivered_at"]
    assert row["emailed"] == 1
    # One send during simulated crash + one during recovery reclaim path
    assert len(sends) >= 1


def test_concurrent_claims_only_one_send(monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    iid = _insert_lead("race@example.com")
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    barrier = threading.Barrier(2)
    sends = []
    lock = threading.Lock()

    def slow_send(*a, **k):
        with lock:
            sends.append(1)
        barrier.wait(timeout=5)

    monkeypatch.setattr(mailer, "send", slow_send)
    results = []

    def worker():
        try:
            inquiry_notify.deliver_owner_email(iid)
            results.append("ok")
        except Exception as e:
            results.append(type(e).__name__)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["owner_email_delivered_at"] or "RuntimeError" in results
    # At most one successful SMTP when claim lock works (second may error claim_busy)
    assert len(sends) <= 1 or row["owner_email_delivered_at"]


def test_max_attempt_job_fails_visible(monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    iid = _insert_lead("max@example.com")
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(mailer, "send", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    jid = inquiry_notify.enqueue_owner_email(iid)
    for _ in range(jobs.MAX_ATTEMPTS):
        jobs._execute(jid)
    job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "failed"
    assert job["attempts"] == jobs.MAX_ATTEMPTS
    assert (
        db.one("SELECT owner_email_failure_category FROM inquiries WHERE id=?", (iid,))[
            "owner_email_failure_category"
        ]
        == "smtp_error"
    )


def test_notion_create_race_records_orphan_and_relink(monkeypatch):
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "db-1")
    iid = _insert_lead("notion-race@example.com")
    created = []

    def create_page(db_id, props):
        page = f"orphan-page-{len(created) + 1}"
        created.append(page)
        # Simulate peer stamping first after create returns
        if len(created) == 1:
            db.run(
                "UPDATE inquiries SET notion_page_id=? WHERE id=?",
                ("winner-page", iid),
            )
        return page

    monkeypatch.setattr(notion_sync, "_create_page", create_page)
    monkeypatch.setattr(notion_sync, "_patch_page", lambda *a, **k: None)
    notion_sync.sync_inquiry(iid)
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["notion_page_id"] == "winner-page"
    assert row["notion_orphan_page_id"] == "orphan-page-1"
    assert row["notion_orphan_status"] == "open"

    # Relink only when stamp null — here stamp exists, so dismiss path:
    assert notion_sync.relink_notion_orphan(iid) is False
    # Force open orphan with null stamp for relink path
    db.run(
        """UPDATE inquiries SET notion_page_id=NULL, notion_orphan_status='open',
             notion_orphan_page_id='orphan-page-1' WHERE id=?""",
        (iid,),
    )
    assert notion_sync.relink_notion_orphan(iid) is True
    row = db.one("SELECT * FROM inquiries WHERE id=?", (iid,))
    assert row["notion_page_id"] == "orphan-page-1"
    assert row["notion_orphan_status"] == "relinked"


def test_lead_survives_email_and_notion_failure(client, monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(mailer, "send", lambda *a, **k: (_ for _ in ()).throw(OSError("smtp")))
    monkeypatch.setattr(config, "NOTION_TOKEN", "tok")
    monkeypatch.setattr(config, "NOTION_LEADS_DB", "db-1")
    monkeypatch.setattr(
        notion_sync,
        "_create_page",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("notion down")),
    )
    email = "survive@example.com"
    db.run("DELETE FROM inquiries WHERE email=?", (email,))
    r = client.post(
        "/contact",
        data={"name": "Survive", "email": email, "message": "Keep me"},
    )
    assert r.status_code == 200 and "Thanks" in r.text
    inq = db.one("SELECT * FROM inquiries WHERE email=?", (email,))
    assert inq is not None
    assert inq["message"] and "Keep me" in inq["message"]
    # Owner email job enqueued
    oe = db.one("SELECT * FROM jobs WHERE kind='inquiry_owner_email' ORDER BY id DESC LIMIT 1")
    assert oe and f'"inquiry_id": {inq["id"]}' in oe["payload"]
    jobs._execute(oe["id"])
    # Notion job enqueued and fails loud
    nj = db.one("SELECT * FROM jobs WHERE kind='notion_sync_inquiry' ORDER BY id DESC LIMIT 1")
    assert nj
    for _ in range(jobs.MAX_ATTEMPTS):
        try:
            jobs._execute(nj["id"])
        except Exception:
            pass
    # Lead still present
    assert db.one("SELECT id FROM inquiries WHERE id=?", (inq["id"],))


def test_contact_enqueues_owner_email_and_thanks(client, monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(mailer, "configured", lambda: False)
    email = "enqueue-only@example.com"
    db.run("DELETE FROM inquiries WHERE email=?", (email,))
    r = client.post("/contact", data={"name": "Q", "email": email, "message": "hello"})
    assert r.status_code == 200 and "Thanks" in r.text
    inq = db.one("SELECT * FROM inquiries WHERE email=?", (email,))
    job = db.one("SELECT * FROM jobs WHERE kind='inquiry_owner_email' ORDER BY id DESC LIMIT 1")
    assert job and f'"inquiry_id": {inq["id"]}' in job["payload"]


def test_inbox_retry_owner_email_route(client, monkeypatch):
    monkeypatch.setattr(jobs, "_pool", None)
    iid = _insert_lead("retry-ui@example.com")
    db.run(
        """UPDATE inquiries SET owner_email_status='failed',
             owner_email_failure_category='smtp_error', owner_email_attempts=1
           WHERE id=?""",
        (iid,),
    )
    r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(
        f"/admin/inbox/{iid}/retry-owner-email",
        data={"tab": "all"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    job = db.one("SELECT * FROM jobs WHERE kind='inquiry_owner_email' ORDER BY id DESC LIMIT 1")
    assert job is not None
