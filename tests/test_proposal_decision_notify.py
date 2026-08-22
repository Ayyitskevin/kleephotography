"""Owner notification on proposal accept / decline (app/proposal_notify.py).

The accept/decline UPDATE in app/public/docs.py commits first; the owner nudge
(Telegram line + owner email) is staged through the job queue so a down channel
can never fail the client's action. Pinned here:

* accept enqueues one ``proposal_decision_notify`` job and delivery sends both
  the Telegram line and the owner email;
* decline does the same, worded as a decline;
* the client's response survives every notify failure mode — the enqueue
  itself exploding, and SMTP failing at delivery (job parks and retries, the
  committed decision untouched);
* both channels unconfigured stays dormant: the job completes without a send
  and without an error;
* the contract-sign path stages no job of this kind (R3 touched only the
  accept/decline handlers).
"""

import hashlib
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app import alerts, config, db, features, jobs, mailer
from app.main import app
from tests.jobtest import drain_job, freeze_job_pool

pytestmark = pytest.mark.integration

_counter = itertools.count()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _seed_proposal():
    """One sent proposal plus the client/project it hangs off."""
    tag = f"pdn{next(_counter)}"
    cid = db.run(
        "INSERT INTO clients (name, email, company) VALUES (?,?,?)",
        (f"Dec {tag}", f"{tag}@d.test", f"Dec Co {tag}"),
    )
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, f"Dec {tag}"))
    slug = f"dec-{tag}"
    did = db.run(
        "INSERT INTO proposals (project_id, slug, title, line_items, total_cents, status) "
        "VALUES (?,?,?,?,?,'sent')",
        (pid, slug, f"Tasting menu {tag}", "[]", 250000),
    )
    return slug, did, pid, cid


def _cleanup(did, pid, cid):
    db.run("DELETE FROM jobs WHERE kind='proposal_decision_notify'")
    db.run("DELETE FROM proposals WHERE id=?", (did,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def _decision_job(proposal_id):
    return db.one(
        "SELECT * FROM jobs WHERE kind='proposal_decision_notify' AND payload LIKE ? "
        "ORDER BY id DESC LIMIT 1",
        (f'%"proposal_id": {proposal_id}%',),
    )


def _capture_telegram(monkeypatch, sent: list[str]) -> None:
    """Arm the dormant Telegram channel and run its daemon thread inline."""

    class _InlineThread:
        def __init__(self, target, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(alerts, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(alerts.threading, "Thread", _InlineThread)
    monkeypatch.setattr(features, "telegram_enabled", lambda: True)


def _capture_mail(monkeypatch, sent: list[dict]) -> None:
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(
        mailer,
        "send",
        lambda to, subject, body, reply_to="", ics=None: sent.append(
            {"to": to, "subject": subject, "body": body, "reply_to": reply_to}
        ),
    )


@pytest.mark.parametrize(("action", "decision"), (("accept", "accepted"), ("decline", "declined")))
def test_decision_enqueues_and_delivers_owner_notification(client, monkeypatch, action, decision):
    freeze_job_pool(monkeypatch)
    slug, did, pid, cid = _seed_proposal()
    telegram: list[str] = []
    mail: list[dict] = []
    _capture_telegram(monkeypatch, telegram)
    _capture_mail(monkeypatch, mail)
    try:
        r = client.post(f"/p/{slug}/{action}", follow_redirects=False)
        assert r.status_code == 303
        assert db.one("SELECT status FROM proposals WHERE id=?", (did,))["status"] == decision

        job = _decision_job(did)
        assert job is not None, "accept/decline must stage a proposal_decision_notify job"
        assert json.loads(job["payload"]) == {"proposal_id": did, "decision": decision}
        assert job["status"] == "queued"

        drain_job(job["id"])
        assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "done"

        admin_url = f"{config.BASE_URL}/admin/studio/proposals/{did}"
        assert len(telegram) == 1
        assert decision in telegram[0]
        assert "Tasting menu" in telegram[0]
        assert "$2,500.00" in telegram[0]
        assert admin_url in telegram[0]

        assert len(mail) == 1
        assert mail[0]["to"] == "kevin@example.com"
        assert decision in mail[0]["subject"]
        assert admin_url in mail[0]["body"]
        assert mail[0]["reply_to"].endswith("@d.test")  # reply lands with the client
    finally:
        _cleanup(did, pid, cid)


def test_accept_response_survives_notify_enqueue_failure(client, monkeypatch):
    """The staging call itself exploding must cost a log line, not the 303."""
    freeze_job_pool(monkeypatch)
    slug, did, pid, cid = _seed_proposal()
    attempts: list[str] = []

    def boom(kind, payload):
        attempts.append(kind)
        raise RuntimeError("queue down")

    monkeypatch.setattr(jobs, "enqueue", boom)
    try:
        r = client.post(f"/p/{slug}/accept", follow_redirects=False)
        assert r.status_code == 303
        row = db.one("SELECT status, accepted_at FROM proposals WHERE id=?", (did,))
        assert row["status"] == "accepted" and row["accepted_at"]
        # The notify WAS attempted — this is what fails on the pre-change code.
        assert attempts == ["proposal_decision_notify"]
    finally:
        _cleanup(did, pid, cid)


def test_smtp_failure_parks_a_retry_and_never_touches_the_decision(client, monkeypatch):
    freeze_job_pool(monkeypatch)
    slug, did, pid, cid = _seed_proposal()
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")

    def boom(*a, **k):
        raise OSError("smtp unavailable")

    monkeypatch.setattr(mailer, "send", boom)
    try:
        r = client.post(f"/p/{slug}/decline", follow_redirects=False)
        assert r.status_code == 303
        job = _decision_job(did)
        assert job is not None

        drain_job(job["id"])
        parked = db.one("SELECT status, next_attempt_at FROM jobs WHERE id=?", (job["id"],))
        assert parked["status"] == "queued" and parked["next_attempt_at"]
        assert db.one("SELECT status FROM proposals WHERE id=?", (did,))["status"] == "declined"

        # SMTP comes back → the parked retry delivers and the job completes.
        mail: list[dict] = []
        _capture_mail(monkeypatch, mail)
        drain_job(job["id"])
        assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "done"
        assert len(mail) == 1 and "declined" in mail[0]["subject"]
    finally:
        _cleanup(did, pid, cid)


def test_unconfigured_channels_stay_dormant_and_job_completes(client, monkeypatch):
    """Neither Telegram nor the mailer configured: no send, no error, job done.

    Uses the HTMX variant so the partial-response path is covered too."""
    freeze_job_pool(monkeypatch)
    slug, did, pid, cid = _seed_proposal()
    telegram: list[str] = []
    monkeypatch.setattr(alerts, "_send", lambda text: telegram.append(text))
    monkeypatch.setattr(features, "telegram_enabled", lambda: False)
    monkeypatch.setattr(mailer, "configured", lambda: False)
    try:
        r = client.post(f"/p/{slug}/accept", headers={"hx-request": "true"})
        assert r.status_code == 200
        assert db.one("SELECT status FROM proposals WHERE id=?", (did,))["status"] == "accepted"
        job = _decision_job(did)
        assert job is not None
        drain_job(job["id"])
        assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "done"
        assert telegram == []
    finally:
        _cleanup(did, pid, cid)


def test_contract_sign_stages_no_decision_notify(client, monkeypatch):
    """R3 touches only the proposal accept/decline handlers — signing a
    contract must not stage a proposal_decision_notify job."""
    freeze_job_pool(monkeypatch)
    tag = f"pdn{next(_counter)}"
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", (f"Sig {tag}", f"{tag}@s.test"))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, f"Sig {tag}"))
    slug = f"sig-{tag}"
    body = "AGREEMENT BODY"
    did = db.run(
        "INSERT INTO contracts (project_id, slug, title, body, body_sha256, status) "
        "VALUES (?,?,?,?,?,'sent')",
        (pid, slug, "Services Agreement", body, hashlib.sha256(body.encode()).hexdigest()),
    )
    try:
        r = client.post(
            f"/c/{slug}/sign",
            data={"signer_name": "Dana Client", "agree": "yes"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert db.one("SELECT status FROM contracts WHERE id=?", (did,))["status"] == "signed"
        assert db.one("SELECT id FROM jobs WHERE kind='proposal_decision_notify'") is None
    finally:
        db.run("DELETE FROM jobs WHERE kind='proposal_decision_notify'")
        db.run("DELETE FROM contracts WHERE id=?", (did,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))
