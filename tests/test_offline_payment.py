"""Recording a payment received outside Stripe (cash, check, bank transfer).

Since #144 a hand move into Retainer Paid needs a deposit_paid or paid invoice,
and only the Stripe webhook could record a payment, so a retainer paid in cash
could never reach Retainer Paid. The admin action records one installment on one
invoice through the SAME function the webhook uses, so the money rules, the
invoice and project moves and the Notion sync stay identical, plus an audit row
that says how it was paid. Every refusal writes nothing and sits next to a
positive control.
"""

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import studio
from app.main import app
from app.public import pay

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 11, 8)


@pytest.fixture(scope="module")
def admin_client():
    with TestClient(app) as client:
        r = client.post(
            "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
        )
        assert r.status_code == 303
        yield client


@pytest.fixture(autouse=True)
def _pinned_clock_and_quiet_jobs(monkeypatch):
    monkeypatch.setattr(studio, "_today", lambda: TODAY)
    monkeypatch.setattr(pay.jobs, "dispatch", lambda ids: None)


@pytest.fixture
def chain():
    """client → project (contract_signed) → invoice; yields a factory, cleans up."""
    made = []

    def make(*, total=90000, deposit=30000, inv_status="sent", project_status="contract_signed"):
        cid = db.run(
            "INSERT INTO clients (name, email) VALUES (?,?)", ("Cash Client", "cash@x.test")
        )
        pid = db.run(
            "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
            (cid, "Cash shoot", project_status),
        )
        iid = db.run(
            "INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents, status) "
            "VALUES (?,?,?,?,?,?)",
            (pid, f"cash-{pid}", "Retainer", total, deposit, inv_status),
        )
        made.append((cid, pid, iid))
        return pid, iid

    yield make
    for cid, pid, iid in made:
        pay_ids = [r["id"] for r in db.all_("SELECT id FROM payments WHERE invoice_id=?", (iid,))]
        for p_id in pay_ids:
            db.run("DELETE FROM audit_log WHERE entity_type='payment' AND entity_id=?", (p_id,))
        db.run("DELETE FROM audit_log WHERE entity_type='project' AND entity_id=?", (pid,))
        db.run(
            "DELETE FROM jobs WHERE kind='notion_sync_invoice' AND payload LIKE ?", (f"%: {iid}}}",)
        )
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def _record(client, iid, *, method="cash", amount="300.00", received_on="2026-11-07", reference=""):
    return client.post(
        f"/admin/studio/invoices/{iid}/offline-payment",
        data={
            "method": method,
            "amount": amount,
            "received_on": received_on,
            "reference": reference,
        },
        follow_redirects=False,
    )


def _state(iid, pid):
    inv = db.one("SELECT status, paid_at FROM invoices WHERE id=?", (iid,))
    return {
        "invoice": inv["status"],
        "paid_at": inv["paid_at"],
        "project": db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"],
        "payments": db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?", (iid,))["n"],
        "audit": db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='payment' AND entity_id IN "
            "(SELECT id FROM payments WHERE invoice_id=?)",
            (iid,),
        )["n"],
    }


def _offline_audit(iid):
    rows = db.all_(
        "SELECT a.actor, a.action, a.diff_json FROM audit_log a JOIN payments p ON p.id=a.entity_id "
        "WHERE a.entity_type='payment' AND p.invoice_id=? ORDER BY a.id",
        (iid,),
    )
    return [(r["actor"], r["action"], json.loads(r["diff_json"])) for r in rows]


# ── what it records ─────────────────────────────────────────────────────────


def test_a_cash_deposit_is_recorded_and_moves_invoice_and_project_like_stripe(admin_client, chain):
    pid, iid = chain()
    emails_before = db.one("SELECT COUNT(*) AS n FROM emails_log")["n"]

    r = _record(
        admin_client,
        iid,
        method="cash",
        amount="300.00",
        received_on="2026-11-07",
        reference="paid at the consult",
    )

    assert r.status_code == 303
    pay_row = db.one("SELECT * FROM payments WHERE invoice_id=?", (iid,))
    assert pay_row["kind"] == "deposit" and pay_row["amount_cents"] == 30000
    assert pay_row["stripe_event_id"] is None and pay_row["stripe_session_id"] is None
    # Dated the day it was received, on the operator's wall clock (revenue months use localtime).
    assert (
        db.one(
            "SELECT date(created_at, 'localtime') AS d FROM payments WHERE id=?", (pay_row["id"],)
        )["d"]
        == "2026-11-07"
    )
    s = _state(iid, pid)
    assert s["invoice"] == "deposit_paid"
    assert s["project"] == "retainer_paid"  # exactly what a Stripe deposit does
    assert _offline_audit(iid) == [
        (
            "admin",
            "offline_payment",
            {
                "invoice_id": iid,
                "kind": "deposit",
                "amount_cents": 30000,
                "method": "cash",
                "received_on": "2026-11-07",
                "reference": "paid at the consult",
                # ties the audit row to this exact payment row, not just to its reusable id
                "payment_created_at": pay_row["created_at"],
            },
        )
    ]
    job = db.one(
        "SELECT status FROM jobs WHERE kind='notion_sync_invoice' AND payload LIKE ? ORDER BY id DESC",
        (f"%: {iid}}}",),
    )
    assert (
        job is not None and job["status"] == "queued"
    )  # same Notion sync as Stripe, staged in the tx
    assert db.one("SELECT COUNT(*) AS n FROM emails_log")["n"] == emails_before  # no client email


def test_a_check_balance_settles_and_paid_at_is_the_day_received(admin_client, chain):
    pid, iid = chain()
    assert _record(admin_client, iid, amount="300.00", received_on="2026-11-01").status_code == 303
    r = _record(
        admin_client,
        iid,
        method="check",
        amount="600.00",
        received_on="2026-11-05",
        reference="check 1042",
    )
    assert r.status_code == 303
    s = _state(iid, pid)
    assert s["invoice"] == "paid" and s["payments"] == 2
    assert (
        db.one("SELECT date(paid_at, 'localtime') AS d FROM invoices WHERE id=?", (iid,))["d"]
        == "2026-11-05"
    )
    kinds = [a[2]["kind"] for a in _offline_audit(iid)]
    assert kinds == ["deposit", "balance"]


def test_an_invoice_without_a_deposit_takes_the_full_amount(admin_client, chain):
    pid, iid = chain(deposit=0)
    assert _record(admin_client, iid, method="bank_transfer", amount="900.00").status_code == 303
    assert (
        db.one("SELECT kind, amount_cents FROM payments WHERE invoice_id=?", (iid,))["kind"]
        == "full"
    )
    assert _state(iid, pid)["invoice"] == "paid"


def test_the_admin_action_reuses_the_webhooks_payment_function(admin_client, chain, monkeypatch):
    pid, iid = chain()
    calls = []
    real = pay.record_invoice_payment

    def spy(con, d, kind, amount_cents, **kw):
        calls.append((kind, amount_cents, kw.get("stripe_event_id")))
        return real(con, d, kind, amount_cents, **kw)

    monkeypatch.setattr(pay, "record_invoice_payment", spy)
    assert _record(admin_client, iid).status_code == 303
    assert calls == [("deposit", 30000, None)]


# ── the #144 gate ───────────────────────────────────────────────────────────


def test_a_cash_deposit_opens_the_retainer_paid_gate_and_nothing_else_does(admin_client, chain):
    unpaid_pid, _ = chain()
    paid_pid, paid_iid = chain()

    # Without a recorded payment, a hand move into Retainer Paid is still refused.
    r = admin_client.post(
        f"/admin/studio/projects/{unpaid_pid}/status",
        data={"status": "retainer_paid"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert (
        db.one("SELECT status FROM projects WHERE id=?", (unpaid_pid,))["status"]
        == "contract_signed"
    )

    # Record the cash deposit: the project advances as a Stripe deposit would.
    assert _record(admin_client, paid_iid).status_code == 303
    assert (
        db.one("SELECT status FROM projects WHERE id=?", (paid_pid,))["status"] == "retainer_paid"
    )

    # And the hand move itself now passes the gate: out, then back in by hand.
    out = admin_client.post(
        f"/admin/studio/projects/{paid_pid}/status",
        data={"status": "contract_signed"},
        follow_redirects=False,
    )
    back = admin_client.post(
        f"/admin/studio/projects/{paid_pid}/status",
        data={"status": "retainer_paid"},
        follow_redirects=False,
    )
    assert (out.status_code, back.status_code) == (303, 303)
    assert (
        db.one("SELECT status FROM projects WHERE id=?", (paid_pid,))["status"] == "retainer_paid"
    )
    # The unpaid project is still refused after all that.
    again = admin_client.post(
        f"/admin/studio/projects/{unpaid_pid}/status",
        data={"status": "retainer_paid"},
        follow_redirects=False,
    )
    assert again.status_code == 409


# ── refusals: each writes nothing, each beside a positive control ───────────


def _assert_nothing_written(before, iid, pid):
    assert _state(iid, pid) == before


def test_refuses_a_draft_invoice(admin_client, chain):
    pid, iid = chain(inv_status="draft")
    before = _state(iid, pid)
    assert _record(admin_client, iid).status_code == 400
    _assert_nothing_written(before, iid, pid)
    db.run("UPDATE invoices SET status='sent' WHERE id=?", (iid,))  # control: once sent, accepted
    assert _record(admin_client, iid).status_code == 303


def test_refuses_a_fully_paid_invoice(admin_client, chain):
    pid, iid = chain(deposit=0)
    assert _record(admin_client, iid, amount="900.00").status_code == 303  # control
    before = _state(iid, pid)
    assert before["invoice"] == "paid"
    assert _record(admin_client, iid, amount="900.00").status_code == 400
    _assert_nothing_written(before, iid, pid)


@pytest.mark.parametrize("amount", ["900.00", "299.99", "600.00", "0", "-300", "abc"])
def test_refuses_an_amount_that_is_not_the_installment_due(admin_client, chain, amount):
    pid, iid = chain()
    before = _state(iid, pid)
    assert _record(admin_client, iid, amount=amount).status_code == 400
    _assert_nothing_written(before, iid, pid)
    assert _record(admin_client, iid, amount="300.00").status_code == 303  # control: the deposit


@pytest.mark.parametrize("amount", ["299.999", "300.001", "300.0000", "300.005"])
def test_refuses_more_than_two_decimal_places(admin_client, chain, amount):
    """Exact parse: 299.999 used to round to the $300.00 deposit and be accepted."""
    pid, iid = chain()
    before = _state(iid, pid)
    assert _record(admin_client, iid, amount=amount).status_code == 400
    _assert_nothing_written(before, iid, pid)
    assert _record(admin_client, iid, amount="300.0").status_code == 303  # control: exactly $300


@pytest.mark.parametrize("amount", ["1e309", "inf", "-inf", "nan", "Infinity", "1e999999999"])
def test_refuses_an_amount_that_is_not_finite_or_overflows(admin_client, chain, amount):
    """These used to reach round(float(...)) and raise OverflowError: a 500 on a money route."""
    pid, iid = chain()
    before = _state(iid, pid)
    assert _record(admin_client, iid, amount=amount).status_code == 400
    _assert_nothing_written(before, iid, pid)
    assert _record(admin_client, iid, amount="300").status_code == 303  # control


def test_refuses_a_second_deposit_on_a_double_submit(admin_client, chain):
    pid, iid = chain()
    assert _record(admin_client, iid).status_code == 303  # control: the first one lands
    before = _state(iid, pid)
    assert (
        _record(admin_client, iid).status_code == 400
    )  # the same form again: the balance is due now
    _assert_nothing_written(before, iid, pid)


def test_a_duplicate_installment_at_the_database_rolls_everything_back(admin_client, chain):
    """A race the form check cannot see: a deposit row exists while the invoice still
    reads 'sent'. UNIQUE(invoice_id, kind) refuses the insert and the whole
    transaction, audit row included, unwinds."""
    pid, iid = chain()
    db.run(
        "INSERT INTO payments (invoice_id, amount_cents, kind) VALUES (?,?,?)",
        (iid, 30000, "deposit"),
    )
    before = _state(iid, pid)
    assert _record(admin_client, iid).status_code == 409
    _assert_nothing_written(before, iid, pid)
    db.run(
        "DELETE FROM payments WHERE invoice_id=?", (iid,)
    )  # control: without the stray row, accepted
    assert _record(admin_client, iid).status_code == 303


def test_refuses_a_date_in_the_future(admin_client, chain):
    pid, iid = chain()
    before = _state(iid, pid)
    assert _record(admin_client, iid, received_on="2026-11-09").status_code == 400
    _assert_nothing_written(before, iid, pid)
    assert _record(admin_client, iid, received_on="2026-11-08").status_code == 303  # control: today


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "stripe"),
        ("method", ""),
        ("received_on", ""),
        ("received_on", "11/07/2026"),
        ("reference", "x" * 201),
    ],
)
def test_refuses_a_bad_method_date_or_reference(admin_client, chain, field, value):
    pid, iid = chain()
    before = _state(iid, pid)
    assert _record(admin_client, iid, **{field: value}).status_code == 400
    _assert_nothing_written(before, iid, pid)
    assert _record(admin_client, iid).status_code == 303  # control


def test_refuses_when_not_signed_in(chain):
    pid, iid = chain()
    before = _state(iid, pid)
    with TestClient(app) as anon:
        r = _record(anon, iid)
    assert r.status_code in (302, 303, 401, 403)
    _assert_nothing_written(before, iid, pid)


# ── what the client sees ────────────────────────────────────────────────────


def test_the_client_receipt_lists_a_cash_payment_without_claiming_stripe(admin_client, chain):
    pid, iid = chain()
    slug = db.one("SELECT slug FROM invoices WHERE id=?", (iid,))["slug"]
    assert _record(admin_client, iid).status_code == 303
    with TestClient(app) as anon:
        r = anon.get(f"/i/{slug}/receipt")
    assert r.status_code == 200 and "300.00" in r.text
    assert "processed securely by Stripe" not in r.text
    # Control: a Stripe-recorded payment still carries the line.
    db.run(
        "INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id, amount_cents, kind) "
        "VALUES (?,?,?,?,?)",
        (iid, f"evt_receipt_{iid}", f"cs_receipt_{iid}", 60000, "balance"),
    )
    with TestClient(app) as anon:
        r = anon.get(f"/i/{slug}/receipt")
    assert r.status_code == 200 and "processed securely by Stripe" in r.text


# ── shown on the invoice page ───────────────────────────────────────────────


def test_the_invoice_page_shows_how_it_was_paid(admin_client, chain):
    pid, iid = chain()
    page = admin_client.get(f"/admin/studio/invoices/{iid}")
    assert (
        page.status_code == 200
        and f'action="/admin/studio/invoices/{iid}/offline-payment"' in page.text
    )
    assert _record(admin_client, iid, method="check", reference="check 1042").status_code == 303
    page = admin_client.get(f"/admin/studio/invoices/{iid}")
    assert page.status_code == 200
    assert "data-offline-payment" in page.text
    assert "Check" in page.text and "received 2026-11-07" in page.text and "check 1042" in page.text


# ── labels survive a correction (payments.id has no AUTOINCREMENT, so ids are reused) ──


def _correct_by_hand(iid, payment_id, *, void=True):
    """What the PR's correction does: delete the offline payment row and put the invoice back.
    With void=True it also retires the payment's audit row by appending an
    offline_payment_voided row that names it (audit_log stays append-only)."""
    if void:
        from app import audit

        a = db.one(
            "SELECT id FROM audit_log WHERE entity_type='payment' AND action='offline_payment' "
            "AND entity_id=? ORDER BY id DESC LIMIT 1",
            (payment_id,),
        )
        with db.tx() as con:
            audit.log(con, "payment", payment_id, "offline_payment_voided", diff={"voids": a["id"]})
    db.run("DELETE FROM payments WHERE id=?", (payment_id,))
    db.run("UPDATE invoices SET status='sent', paid_at=NULL WHERE id=?", (iid,))


def _payment_line(page_text, amount="300.00"):
    """The <li> of the payment list that shows this amount."""
    return [li for li in page_text.split("<li") if f"${amount}</strong>" in li]


def test_a_stripe_payment_reusing_a_corrected_offline_payments_id_is_labelled_stripe(
    admin_client, chain
):
    """Kimi's reproduction (probe E): the corrected check's audit row used to re-attach,
    by id alone, to the Stripe payment that reused that id."""
    pid, iid = chain()
    assert _record(admin_client, iid, method="check", reference="chk-1").status_code == 303
    old_id = db.one("SELECT id FROM payments WHERE invoice_id=?", (iid,))["id"]
    _correct_by_hand(iid, old_id, void=False)  # even without the void row
    db.run(
        "INSERT INTO payments (id, invoice_id, stripe_event_id, amount_cents, kind) VALUES (?,?,?,?,?)",
        (old_id, iid, f"evt_reuse_{iid}", 30000, "deposit"),
    )
    page = admin_client.get(f"/admin/studio/invoices/{iid}").text
    assert "chk-1" not in page and "received 2026-11-07" not in page
    lines = _payment_line(page)
    assert len(lines) == 1 and "Stripe" in lines[0] and "data-offline-payment" not in lines[0]


def test_a_second_offline_payment_reusing_the_id_shows_only_its_own_details(admin_client, chain):
    pid, iid = chain()
    assert _record(admin_client, iid, method="check", reference="chk-1").status_code == 303
    old_id = db.one("SELECT id FROM payments WHERE invoice_id=?", (iid,))["id"]
    _correct_by_hand(iid, old_id, void=False)
    # same day, same kind, same amount: everything but the method and reference matches
    assert _record(admin_client, iid, method="cash", reference="cash-2").status_code == 303
    new_id = db.one("SELECT id FROM payments WHERE invoice_id=?", (iid,))["id"]
    assert new_id == old_id, "precondition: SQLite reused the freed id"
    page = admin_client.get(f"/admin/studio/invoices/{iid}").text
    lines = _payment_line(page)
    assert len(lines) == 1 and "cash-2" in lines[0] and "Cash" in lines[0]
    assert "chk-1" not in page


def test_a_retired_audit_row_never_labels_a_payment(admin_client, chain):
    """A row restored by hand with the same id, kind, amount and created_at would match the old
    audit row on every field; only the void row tells them apart."""
    pid, iid = chain()
    assert _record(admin_client, iid, method="check", reference="chk-1").status_code == 303
    old = dict(db.one("SELECT * FROM payments WHERE invoice_id=?", (iid,)))

    def restore():
        db.run(
            "INSERT INTO payments (id, invoice_id, amount_cents, kind, created_at) VALUES (?,?,?,?,?)",
            (old["id"], iid, old["amount_cents"], old["kind"], old["created_at"]),
        )

    # Control: deleted and restored byte for byte WITHOUT retiring the audit row: it is the same
    # payment, so the label comes back.
    _correct_by_hand(iid, old["id"], void=False)
    restore()
    assert "chk-1" in admin_client.get(f"/admin/studio/invoices/{iid}").text
    # Retired, then restored: no label.
    _correct_by_hand(iid, old["id"], void=True)
    restore()
    page = admin_client.get(f"/admin/studio/invoices/{iid}").text
    assert "chk-1" not in page
    assert "Recorded payment" in _payment_line(page)[0]


def test_a_payment_without_stripe_or_an_audit_row_is_not_called_stripe(admin_client, chain):
    pid, iid = chain()
    db.run(
        "INSERT INTO payments (invoice_id, amount_cents, kind) VALUES (?,?,?)",
        (iid, 30000, "deposit"),
    )
    db.run("UPDATE invoices SET status='deposit_paid' WHERE id=?", (iid,))
    lines = _payment_line(admin_client.get(f"/admin/studio/invoices/{iid}").text)
    assert len(lines) == 1 and "Recorded payment" in lines[0] and "Stripe" not in lines[0]
