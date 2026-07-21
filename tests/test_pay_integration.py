"""Money-path integration tests — Checkout create + webhook invariants.

Extracted from the smoke monolith so cash invariants run without ordering on
earlier gallery/studio tests. Pure math stays in test_invoices_money.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.public import pay

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit

    ratelimit._hits.clear()
    yield


def _stripe_sig(payload: bytes, secret: str) -> str:
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _checkout_event(
    event_id,
    invoice_id,
    kind,
    amount,
    payment_status="paid",
    etype="checkout.session.completed",
):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": etype,
            "data": {
                "object": {
                    "id": f"cs_{event_id}",
                    "object": "checkout.session",
                    "payment_status": payment_status,
                    "amount_total": amount,
                    "metadata": {"invoice_id": str(invoice_id), "kind": kind},
                }
            },
        }
    ).encode()


def _seed_money_chain(*, project_status, total=90000, deposit=0, inv_status="sent"):
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Pay Diner", "pay@diner.test"))
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Pay path shoot", project_status),
    )
    iid = db.run(
        "INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents, status) "
        "VALUES (?,?,?,?,?,?)",
        (pid, f"pay-{pid}", "Pay invoice", total, deposit, inv_status),
    )
    return cid, pid, iid


def _cleanup_money_chain(cid, pid, iid):
    db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
    db.run("DELETE FROM invoices WHERE id=?", (iid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def _post_signed(client, body, secret="whsec_test"):
    return client.post(
        "/webhooks/stripe",
        content=body,
        headers={"stripe-signature": _stripe_sig(body, secret)},
    )


# ── Checkout Session.create ─────────────────────────────────────────────────


def test_pay_creates_checkout_with_deposit_amount_and_kind(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")
    captured = {}

    class FakeSession:
        id = "cs_deposit_1"
        url = "https://checkout.stripe.test/cs_deposit_1"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(pay.stripe.checkout.Session, "create", fake_create)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="viewed"
    )
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))
    r = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == FakeSession.url
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 30000
    assert captured["metadata"] == {"invoice_id": str(iid), "kind": "deposit"}
    assert (
        db.one("SELECT stripe_session_id FROM invoices WHERE id=?", (iid,))["stripe_session_id"]
        == "cs_deposit_1"
    )
    _cleanup_money_chain(cid, pid, iid)


def test_pay_creates_checkout_with_balance_after_deposit(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")
    captured = {}

    class FakeSession:
        id = "cs_balance_1"
        url = "https://checkout.stripe.test/cs_balance_1"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(pay.stripe.checkout.Session, "create", fake_create)
    cid, pid, iid = _seed_money_chain(
        project_status="retainer_paid", total=90000, deposit=30000, inv_status="deposit_paid"
    )
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))
    r = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)
    assert r.status_code == 303
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 60000
    assert captured["metadata"]["kind"] == "balance"
    _cleanup_money_chain(cid, pid, iid)


def test_pay_refuses_when_nothing_due(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")
    cid, pid, iid = _seed_money_chain(
        project_status="retainer_paid", total=90000, deposit=0, inv_status="paid"
    )
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))
    r = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)
    assert r.status_code == 400
    _cleanup_money_chain(cid, pid, iid)


# ── Webhook reconcile + ACH async ───────────────────────────────────────────


def test_webhook_rejects_amount_mismatch(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    monkeypatch.setattr(pay.alerts, "security_alert", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(client, _checkout_event("evt_bad_amt", iid, "full", 1))
    assert r.status_code == 409
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "sent"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_rejects_kind_mismatch(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    monkeypatch.setattr(pay.alerts, "security_alert", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="sent"
    )
    # Owed is deposit/30000; claiming balance with that amount is still wrong kind.
    r = _post_signed(client, _checkout_event("evt_bad_kind", iid, "balance", 30000))
    assert r.status_code == 409
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_async_payment_succeeded_settles_ach(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    pending = _post_signed(
        client, _checkout_event("evt_ach_pend", iid, "full", 90000, payment_status="unpaid")
    )
    assert pending.status_code == 200 and pending.json().get("pending") is True
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    settled = _post_signed(
        client,
        _checkout_event(
            "evt_ach_ok",
            iid,
            "full",
            90000,
            etype="checkout.session.async_payment_succeeded",
        ),
    )
    assert settled.status_code == 200 and settled.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "retainer_paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_payment_advances_project_to_retainer_paid(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(client, _checkout_event("evt_adv_pay", iid, "full", 90000))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "retainer_paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_does_not_rewind_a_later_stage_project(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    cid, pid, iid = _seed_money_chain(project_status="session_planning", total=90000)
    r = _post_signed(client, _checkout_event("evt_norewind_pay", iid, "full", 90000))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_ach_pending_records_nothing(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "enqueue", lambda *a, **k: 0)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client, _checkout_event("evt_ach_pend2", iid, "full", 90000, payment_status="unpaid")
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "pending": True}
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "sent"
    _cleanup_money_chain(cid, pid, iid)
