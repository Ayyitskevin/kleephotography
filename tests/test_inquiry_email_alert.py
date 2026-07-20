"""Owner-email delivery failures on the public inquiry path.

When SMTP fails (or the mailer is off), the lead must still persist and the
Notion job must still enqueue — with a privacy-safe, deduplicated operator
signal and truthful Admin Inbox recovery guidance. Never log the visitor
message, email address, or SMTP payload in the alert text.
"""

import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app import alerts, config, db, jobs, mailer, security
from app.main import app

pytestmark = pytest.mark.integration

VISITOR_EMAIL = "smtp-fail-visitor@example.com"
VISITOR_MESSAGE = "PRIVATE_LEAD_BODY_SENTINEL_do_not_leak"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client(client):
    r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _wipe(email: str = VISITOR_EMAIL) -> None:
    db.run("DELETE FROM jobs WHERE kind='notion_sync_inquiry'")
    db.run("DELETE FROM inquiries WHERE email=?", (email,))
    db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (security.INQUIRY_BUCKET_CONTACT,))


def _enable_ops(monkeypatch, sent: list[str]) -> None:
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


def test_contact_smtp_failure_stores_lead_enqueues_job_and_alerts(client, monkeypatch):
    _wipe()
    sent: list[str] = []
    _enable_ops(monkeypatch, sent)
    monkeypatch.setattr(jobs, "_pool", None)  # durable enqueue only — no live Notion
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")

    def boom(*a, **k):
        raise OSError("smtp unavailable")

    monkeypatch.setattr(mailer, "send", boom)
    try:
        r = client.post(
            "/contact",
            data={
                "name": "Smtp Fail",
                "email": VISITOR_EMAIL,
                "message": VISITOR_MESSAGE,
                "business": "Cafe Private",
            },
        )
        assert r.status_code == 200 and "Thanks" in r.text
        inq = db.one("SELECT * FROM inquiries WHERE email=?", (VISITOR_EMAIL,))
        assert inq is not None
        assert inq["emailed"] == 0
        assert VISITOR_MESSAGE in inq["message"]
        job = db.one("SELECT * FROM jobs WHERE kind='notion_sync_inquiry' ORDER BY id DESC LIMIT 1")
        assert job is not None
        assert f'"inquiry_id": {inq["id"]}' in job["payload"]

        assert len(sent) == 1
        body = sent[0]
        assert f"Inquiry #{inq['id']}" in body
        assert "smtp_error" in body
        assert f"/admin/inbox?sel={inq['id']}" in body
        # Privacy: never surface visitor PII or lead body in the operator channel.
        assert VISITOR_EMAIL not in body
        assert VISITOR_MESSAGE not in body
        assert "Cafe Private" not in body
        assert "smtp unavailable" not in body
    finally:
        _wipe()


def test_smtp_failure_alert_is_deduped_per_inquiry(monkeypatch):
    sent: list[str] = []
    _enable_ops(monkeypatch, sent)
    alerts.inquiry_owner_email_failed(42, "smtp_error")
    alerts.inquiry_owner_email_failed(42, "smtp_error")
    alerts.inquiry_owner_email_failed(42, "smtp_error")
    assert len(sent) == 1
    assert "Inquiry #42" in sent[0]
    # A different inquiry still gets its own signal.
    alerts.inquiry_owner_email_failed(43, "smtp_error")
    assert len(sent) == 2
    assert "Inquiry #43" in sent[1]


def test_mailer_unconfigured_uses_global_dedupe_signature(monkeypatch):
    sent: list[str] = []
    _enable_ops(monkeypatch, sent)
    alerts.inquiry_owner_email_failed(10, "mailer_not_configured")
    alerts.inquiry_owner_email_failed(11, "mailer_not_configured")
    # Config gap is fleet-wide — one alert per cooldown, not one per lead.
    assert len(sent) == 1
    assert "mailer not configured" in sent[0].lower()
    assert "Inquiry #10" in sent[0]


def test_successful_owner_email_skips_ops_alert(client, monkeypatch):
    _wipe("ok-visitor@example.com")
    sent: list[str] = []
    _enable_ops(monkeypatch, sent)
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(mailer, "send", lambda *a, **k: None)
    try:
        r = client.post(
            "/contact",
            data={
                "name": "Ok Visitor",
                "email": "ok-visitor@example.com",
                "message": "All good here.",
            },
        )
        assert r.status_code == 200
        inq = db.one("SELECT * FROM inquiries WHERE email=?", ("ok-visitor@example.com",))
        assert inq["emailed"] == 1
        assert sent == []
    finally:
        _wipe("ok-visitor@example.com")


def test_inbox_surfaces_email_failure_and_recovery_path(admin_client, monkeypatch):
    monkeypatch.setattr(mailer, "configured", lambda: True)
    iid = db.run(
        """INSERT INTO inquiries (name, email, business, message, service, emailed)
           VALUES (?,?,?,?,?,0)""",
        ("Alex", "alex-smtp@example.com", "Alex Co", "Need stills", "Real Estate"),
    )
    try:
        page = admin_client.get(f"/admin/inbox?sel={iid}")
        assert page.status_code == 200
        assert "Owner notification not delivered" in page.text
        assert "lead is stored" in page.text.lower() or "Lead is stored" in page.text
        assert "Owner email failed" in page.text or "Reply from Inbox" in page.text

        # Recovery: mark emailed (as Inbox reply does) — health flips ok.
        db.run("UPDATE inquiries SET emailed=1 WHERE id=?", (iid,))
        page2 = admin_client.get(f"/admin/inbox?sel={iid}")
        assert "Notification sent" in page2.text or "replied from Inbox" in page2.text
        assert "Owner notification not delivered" not in page2.text
    finally:
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_contact_mailer_off_still_stores_and_signals(client, monkeypatch):
    _wipe("unconfigured@example.com")
    sent: list[str] = []
    _enable_ops(monkeypatch, sent)
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(mailer, "configured", lambda: False)
    try:
        r = client.post(
            "/contact",
            data={
                "name": "No Mailer",
                "email": "unconfigured@example.com",
                "message": VISITOR_MESSAGE,
            },
        )
        assert r.status_code == 200 and "Thanks" in r.text
        inq = db.one("SELECT * FROM inquiries WHERE email=?", ("unconfigured@example.com",))
        assert inq is not None and inq["emailed"] == 0
        job = db.one(
            "SELECT payload FROM jobs WHERE kind='notion_sync_inquiry' ORDER BY id DESC LIMIT 1"
        )
        assert f'"inquiry_id": {inq["id"]}' in job["payload"]
        assert len(sent) == 1
        assert "mailer not configured" in sent[0].lower()
        assert VISITOR_MESSAGE not in sent[0]
        assert "unconfigured@example.com" not in sent[0]
    finally:
        _wipe("unconfigured@example.com")
