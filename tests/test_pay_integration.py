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
    metadata=None,
    session_id=None,
    currency="usd",
):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": etype,
            "data": {
                "object": {
                    "id": session_id or f"cs_{event_id}",
                    "object": "checkout.session",
                    "payment_status": payment_status,
                    "amount_total": amount,
                    "currency": currency,
                    "metadata": (
                        metadata
                        if metadata is not None
                        else {"invoice_id": str(invoice_id), "kind": kind}
                    ),
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


def _post_signed(
    client,
    body,
    secret="whsec_test",
    *,
    bind=True,
    authorized_amount=None,
    authorized_kind=None,
    authorized_currency="usd",
    authorized_session=None,
):
    event = json.loads(body)
    session = event["data"]["object"]
    metadata = session.get("metadata") or {}
    invoice_id = metadata.get("invoice_id")
    if bind and invoice_id and str(invoice_id).isdigit():
        db.run(
            """UPDATE invoices
                  SET stripe_session_id=?, stripe_checkout_amount_cents=?,
                      stripe_checkout_kind=?, stripe_checkout_currency=?
                WHERE id=?""",
            (
                authorized_session or session["id"],
                session["amount_total"] if authorized_amount is None else authorized_amount,
                authorized_kind or metadata.get("kind"),
                authorized_currency,
                int(invoice_id),
            ),
        )
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


def test_pay_reuses_open_checkout_for_repeated_request(client, monkeypatch):
    """A double-click must not create two independently payable sessions."""
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")
    created = []
    sessions = {}

    class FakeSession:
        status = "open"
        payment_status = "unpaid"
        amount_total = 90000
        currency = "usd"

        def __init__(self, number):
            self.id = f"cs_repeat_{number}"
            self.url = f"https://checkout.stripe.test/{self.id}"

    def fake_create(**kwargs):
        session = FakeSession(len(created) + 1)
        created.append(kwargs)
        sessions[session.id] = session
        return session

    monkeypatch.setattr(pay.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "retrieve",
        lambda session_id, **kwargs: sessions[session_id],
    )
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))

    first = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)
    second = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)

    assert first.status_code == 303 and second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert len(created) == 1
    assert created[0]["idempotency_key"].startswith(f"mise:invoice:{iid}:full:90000:")
    _cleanup_money_chain(cid, pid, iid)


@pytest.mark.parametrize("current_total", [90000, 100000])
def test_pay_never_replaces_complete_unpaid_ach_session(client, monkeypatch, current_total):
    """Completed ACH remains collectible until success/failure webhook resolution."""
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")

    class PendingAch:
        id = "cs_ach_pending"
        status = "complete"
        payment_status = "unpaid"
        url = None

    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "retrieve",
        lambda session_id, **kwargs: PendingAch(),
    )
    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "create",
        lambda **kwargs: pytest.fail("created a second collectible Checkout session"),
    )
    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "expire",
        lambda *args, **kwargs: pytest.fail("attempted to expire completed ACH"),
    )
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    db.run(
        """UPDATE invoices
              SET stripe_session_id='cs_ach_pending', stripe_checkout_amount_cents=90000,
                  stripe_checkout_kind='full', stripe_checkout_currency='usd', total_cents=?
            WHERE id=?""",
        (current_total, iid),
    )
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))

    r = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)

    assert r.status_code == 409
    assert r.json() == {"detail": "payment is still processing"}
    bound = db.one("SELECT stripe_session_id FROM invoices WHERE id=?", (iid,))
    assert bound["stripe_session_id"] == "cs_ach_pending"
    _cleanup_money_chain(cid, pid, iid)


def test_pay_reuses_only_paid_complete_session(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")

    class PaidSession:
        id = "cs_paid_complete"
        status = "complete"
        payment_status = "paid"
        url = None

    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "retrieve",
        lambda session_id, **kwargs: PaidSession(),
    )
    monkeypatch.setattr(
        pay.stripe.checkout.Session,
        "create",
        lambda **kwargs: pytest.fail("replaced a completed paid Checkout session"),
    )
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    db.run(
        """UPDATE invoices
              SET stripe_session_id='cs_paid_complete', stripe_checkout_amount_cents=90000,
                  stripe_checkout_kind='full', stripe_checkout_currency='usd'
            WHERE id=?""",
        (iid,),
    )
    inv = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))

    r = client.post(f"/i/{inv['slug']}/pay", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == f"/i/{inv['slug']}?thanks=1"
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


def test_webhook_acks_amount_mismatch_without_applying_it(client, monkeypatch):
    """Acknowledged, NOT applied.

    A non-2xx here made Stripe retry for ~3 days and counted toward
    auto-disabling the endpoint — after which real payment events stop arriving.
    Retrying could never change the outcome, so the 409 spent the endpoint's
    health for nothing. What must not change: no payment row, no status move.
    """
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client,
        _checkout_event("evt_bad_amt", iid, "full", 1),
        authorized_amount=90000,
    )
    assert r.status_code == 200
    assert r.json()["unapplied"] == "checkout_mismatch"
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "sent"
    # The anomaly is durable, not just a log line: Stripe's retries used to be
    # the only thing keeping it visible.
    row = db.one(
        "SELECT action, diff_json FROM audit_log WHERE entity_type='invoice' AND entity_id=? "
        "ORDER BY id DESC LIMIT 1",
        (iid,),
    )
    assert row["action"] == "stripe_checkout_mismatch"
    assert "90000" in row["diff_json"]
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_acks_kind_mismatch_without_applying_it(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="sent"
    )
    # Owed is deposit/30000; claiming balance with that amount is still wrong kind.
    r = _post_signed(
        client,
        _checkout_event("evt_bad_kind", iid, "balance", 30000),
        authorized_kind="deposit",
    )
    assert r.status_code == 200
    assert r.json()["unapplied"] == "checkout_mismatch"
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_acks_stale_session_without_applying_payment(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client,
        _checkout_event("evt_stale", iid, "full", 90000),
        authorized_session="cs_authoritative",
    )
    assert r.status_code == 200
    assert r.json()["unapplied"] == "stale_session"
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_acks_wrong_currency_without_applying_payment(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client,
        _checkout_event("evt_eur", iid, "full", 90000, currency="eur"),
        authorized_currency="usd",
    )
    assert r.status_code == 200
    assert r.json()["unapplied"] == "checkout_mismatch"
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_acks_unbound_checkout_without_applying_payment(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client,
        _checkout_event("evt_unbound", iid, "full", 90000),
        bind=False,
    )
    assert r.status_code == 200
    assert r.json()["unapplied"] == "checkout_not_bound"
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_retry_same_event_is_duplicate_after_deposit(client, monkeypatch):
    """Stripe retries must stay idempotent even after owed kind flips to balance."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "security_alert", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="sent"
    )
    body = _checkout_event("evt_dep_retry", iid, "deposit", 30000)
    first = _post_signed(client, body)
    assert first.status_code == 200 and first.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "deposit_paid"
    retry = _post_signed(client, body)
    assert retry.status_code == 200 and retry.json().get("duplicate") is True
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 1
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_acks_settled_invoice_and_still_alerts(client, monkeypatch):
    """Money may really have been taken here, so acknowledging must not go quiet."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    fired = []
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: fired.append((a, k)))
    cid, pid, iid = _seed_money_chain(
        project_status="retainer_paid", total=90000, deposit=0, inv_status="paid"
    )
    r = _post_signed(client, _checkout_event("evt_settled", iid, "full", 90000))
    assert r.status_code == 200
    assert r.json()["unapplied"] == "already_settled"
    assert fired, "acknowledged the payment without telling anyone"
    assert "already_settled" in fired[0][0]
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_deposit_then_balance_settles(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "security_alert", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="sent"
    )
    dep = _post_signed(client, _checkout_event("evt_dep_ok", iid, "deposit", 30000))
    assert dep.status_code == 200 and dep.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "deposit_paid"
    bal = _post_signed(client, _checkout_event("evt_bal_ok", iid, "balance", 60000))
    assert bal.status_code == 200 and bal.json() == {"ok": True}
    inv = db.one("SELECT status, paid_at FROM invoices WHERE id=?", (iid,))
    assert inv["status"] == "paid" and inv["paid_at"]
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 2
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "retainer_paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_async_payment_succeeded_settles_ach(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
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


def test_webhook_async_payment_failed_releases_binding_and_allows_retry(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_pay")
    fired = []
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: fired.append((a, k)))
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    failed = _post_signed(
        client,
        _checkout_event(
            "evt_ach_failed",
            iid,
            "full",
            90000,
            payment_status="unpaid",
            etype="checkout.session.async_payment_failed",
            session_id="cs_ach_failed",
        ),
    )

    assert failed.status_code == 200
    assert failed.json() == {"ok": True, "payment_failed": True}
    inv = db.one(
        """SELECT stripe_session_id, stripe_checkout_amount_cents,
                  stripe_checkout_kind, stripe_checkout_currency
             FROM invoices WHERE id=?""",
        (iid,),
    )
    assert tuple(inv) == (None, None, None, None)
    assert fired and fired[0][0][2] == "async_payment_failed"
    audit_row = db.one(
        """SELECT action, diff_json FROM audit_log
             WHERE entity_type='invoice' AND entity_id=? ORDER BY id DESC LIMIT 1""",
        (iid,),
    )
    assert audit_row["action"] == "stripe_async_payment_failed"
    assert "cs_ach_failed" in audit_row["diff_json"]

    class Replacement:
        id = "cs_after_ach_failure"
        status = "open"
        payment_status = "unpaid"
        url = "https://checkout.stripe.test/cs_after_ach_failure"

    monkeypatch.setattr(pay.stripe.checkout.Session, "create", lambda **kwargs: Replacement())
    slug = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))["slug"]
    retry = client.post(f"/i/{slug}/pay", follow_redirects=False)
    assert retry.status_code == 303
    assert retry.headers["location"] == Replacement.url
    assert (
        db.one("SELECT stripe_session_id FROM invoices WHERE id=?", (iid,))["stripe_session_id"]
        == "cs_after_ach_failure"
    )

    db.run("DELETE FROM audit_log WHERE entity_type='invoice' AND entity_id=?", (iid,))
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_stale_async_failure_cannot_release_current_session(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    failed = _post_signed(
        client,
        _checkout_event(
            "evt_old_ach_failed",
            iid,
            "full",
            90000,
            payment_status="unpaid",
            etype="checkout.session.async_payment_failed",
            session_id="cs_old_ach",
        ),
        authorized_session="cs_current",
    )

    assert failed.status_code == 200
    assert failed.json()["unapplied"] == "stale_session"
    bound = db.one("SELECT stripe_session_id FROM invoices WHERE id=?", (iid,))
    assert bound["stripe_session_id"] == "cs_current"
    db.run("DELETE FROM audit_log WHERE entity_type='invoice' AND entity_id=?", (iid,))
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_payment_advances_project_to_retainer_paid(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(client, _checkout_event("evt_adv_pay", iid, "full", 90000))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "retainer_paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_does_not_rewind_a_later_stage_project(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    cid, pid, iid = _seed_money_chain(project_status="session_planning", total=90000)
    r = _post_signed(client, _checkout_event("evt_norewind_pay", iid, "full", 90000))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_ach_pending_records_nothing(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(
        client, _checkout_event("evt_ach_pend2", iid, "full", 90000, payment_status="unpaid")
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "pending": True}
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 0
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "sent"
    _cleanup_money_chain(cid, pid, iid)


# ── Webhook body cap + foreign sessions ─────────────────────────────────────


def test_webhook_rejects_oversized_body(client, monkeypatch):
    """Cap fires on Content-Length before the body is read/signature-checked."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = client.post("/webhooks/stripe", content=b"x" * (pay._MAX_BODY + 1))
    assert r.status_code == 413


def test_webhook_caps_body_without_content_length(client, monkeypatch):
    """A missing Content-Length must not bypass the cap — enforced post-read."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = client.post("/webhooks/stripe", content=iter([b"x" * (pay._MAX_BODY + 1)]))
    assert r.status_code == 413


def test_webhook_ignores_foreign_session_without_invoice_id(client, monkeypatch):
    """A checkout session not created by this app (same Stripe account) is a
    200 no-op — a KeyError 500 would make Stripe disable the endpoint."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = _post_signed(client, _checkout_event("evt_foreign_meta", None, None, 5000, metadata={}))
    assert r.status_code == 200 and r.json() == {"ok": True, "ignored": "no invoice_id"}
    assert (
        db.one("SELECT COUNT(*) AS n FROM payments WHERE stripe_event_id=?", ("evt_foreign_meta",))[
            "n"
        ]
        == 0
    )


def test_webhook_ignores_non_numeric_invoice_id(client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = _post_signed(
        client,
        _checkout_event(
            "evt_foreign_junk",
            None,
            None,
            5000,
            metadata={"invoice_id": "not-an-id", "kind": "full"},
        ),
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "ignored": "no invoice_id"}
    assert (
        db.one("SELECT COUNT(*) AS n FROM payments WHERE stripe_event_id=?", ("evt_foreign_junk",))[
            "n"
        ]
        == 0
    )


def test_webhook_acks_unknown_invoice(client, monkeypatch):
    """Same reasoning as the mismatches: a missing invoice never arrives later.

    The invoice row exists before Checkout is created, so there is no race that
    a retry would win — 404ing this just burned deliveries against the
    endpoint's auto-disable budget.
    """
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    fired = []
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: fired.append(a))
    missing = 99_123_456
    r = _post_signed(client, _checkout_event("evt_unknown_inv", missing, "full", 5000))
    assert r.status_code == 200
    assert r.json()["unapplied"] == "unknown_invoice"
    assert fired, "acknowledged an unknown invoice silently"
    row = db.one(
        "SELECT action FROM audit_log WHERE entity_type='invoice' AND entity_id=? "
        "ORDER BY id DESC LIMIT 1",
        (missing,),
    )
    assert row["action"] == "stripe_unknown_invoice"
    db.run("DELETE FROM audit_log WHERE entity_type='invoice' AND entity_id=?", (missing,))


def test_webhook_still_rejects_a_bad_signature(client, monkeypatch):
    """The retry-storm fix must not have swallowed the one thing Stripe SHOULD
    retry — or, worse, started acking forged payloads.

    A signature failure is not a permanent business condition: it usually means
    the secret was rotated or misconfigured, and acking would silently discard
    real payments.
    """
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = client.post(
        "/webhooks/stripe",
        content=_checkout_event("evt_forged", 1, "full", 1000),
        headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
    )
    assert r.status_code == 400


def test_webhook_applies_a_legitimate_payment_unchanged(client, monkeypatch):
    """Guard against over-acking: the happy path must still record money."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    r = _post_signed(client, _checkout_event("evt_good_full", iid, "full", 90000))
    assert r.status_code == 200 and "unapplied" not in r.json()
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"] == 1
    assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "paid"
    _cleanup_money_chain(cid, pid, iid)


def test_webhook_stages_notion_sync_inside_the_payment_transaction(client, monkeypatch):
    """The sync job must be as durable as the payment itself.

    jobs.enqueue writes its own row in a separate transaction, so a crash between
    the payment commit and that call left the payment recorded with no sync job —
    and nothing to notice: Stripe's retry short-circuits on the duplicate event
    id, the sweeper only re-offers rows that exist, and Notion stays silently
    stale. Staging commits the job with the payment.
    """
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    dispatched = []
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: dispatched.extend(ids))
    cid, pid, iid = _seed_money_chain(project_status="proposal_sent", total=90000)
    watermark = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM jobs")["m"]
    r = _post_signed(client, _checkout_event("evt_notion_stage", iid, "full", 90000))
    assert r.status_code == 200

    job = db.one(
        "SELECT id, kind, payload, status FROM jobs WHERE kind='notion_sync_invoice' "
        "AND id > ? ORDER BY id DESC LIMIT 1",
        (watermark,),
    )
    assert job is not None, "payment committed without a durable sync job"
    assert job["status"] == "queued"
    assert f'"invoice_id": {iid}' in job["payload"]
    # Committed first, then offered to the pool — never the other way round.
    assert dispatched == [job["id"]]
    db.run("DELETE FROM jobs WHERE id=?", (job["id"],))
    _cleanup_money_chain(cid, pid, iid)


def test_a_rolled_back_payment_leaves_no_orphan_sync_job(client, monkeypatch):
    """The other half of atomicity: no job for a payment that never landed.

    A duplicate event rolls the whole transaction back. Before staging, the
    enqueue sat outside it and could not be rolled back with it.
    """
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(pay.alerts, "payment_anomaly", lambda *a, **k: None)
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)
    cid, pid, iid = _seed_money_chain(
        project_status="proposal_sent", total=90000, deposit=30000, inv_status="sent"
    )
    watermark = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM jobs")["m"]
    body = _checkout_event("evt_notion_dupe", iid, "deposit", 30000)
    assert _post_signed(client, body).status_code == 200

    before = db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='notion_sync_invoice' AND id > ?",
        (watermark,),
    )["n"]
    assert before == 1
    # Stripe redelivers the same event: the INSERT trips the unique event id and
    # the transaction unwinds — including the staged job.
    retry = _post_signed(client, body)
    assert retry.status_code == 200 and retry.json().get("duplicate") is True
    after = db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='notion_sync_invoice' AND id > ?",
        (watermark,),
    )["n"]
    assert after == before, "a rolled-back retry still left a sync job behind"
    db.run("DELETE FROM jobs WHERE id > ?", (watermark,))
    _cleanup_money_chain(cid, pid, iid)
