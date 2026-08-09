"""Home dashboard rollups (app/admin/activity.home).

The dashboard is read-only, but it is the page Kevin acts from, so the numbers
have to be honest: the reply queue must surface the longest-waiting leads, and
every "collected" figure must agree with Financials/Reports.
"""

import pytest
from fastapi.testclient import TestClient

from app import db
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


def _open_inquiries() -> int:
    return db.one(
        "SELECT COUNT(*) AS n FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL"
    )["n"]


def _deposit_paid_invoice(slug: str, total: int, deposit: int, paid_at: str | None = None) -> int:
    """A half-collected invoice: the deposit landed as a real payment event but
    the invoice is still open, so `invoices.paid_at` is null. `paid_at` is the
    stored (UTC) payment timestamp. Returns the client id, for cleanup."""
    client_id = db.run("INSERT INTO clients (name) VALUES (?)", (f"{slug} Co",))
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?,?)", (client_id, f"{slug} project")
    )
    invoice_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, deposit_cents, status)
           VALUES (?,?,?,?,?, 'deposit_paid')""",
        (project_id, slug, f"{slug} invoice", total, deposit),
    )
    db.run(
        "INSERT INTO payments (invoice_id, amount_cents, kind, created_at) VALUES (?,?, 'deposit',?)",
        (invoice_id, deposit, paid_at or db.one("SELECT datetime('now') AS t")["t"]),
    )
    return client_id


def _drop_client(client_id: int) -> None:
    db.run(
        """DELETE FROM payments WHERE invoice_id IN
           (SELECT i.id FROM invoices i JOIN projects p ON p.id=i.project_id
            WHERE p.client_id=?)""",
        (client_id,),
    )
    db.run(
        "DELETE FROM invoices WHERE project_id IN (SELECT id FROM projects WHERE client_id=?)",
        (client_id,),
    )
    db.run("DELETE FROM projects WHERE client_id=?", (client_id,))
    db.run("DELETE FROM clients WHERE id=?", (client_id,))


@pytest.mark.integration
def test_reply_queue_shows_the_oldest_leads_and_counts_all_of_them(admin_client):
    """Eight open inquiries: the six oldest fill the queue (the oldest first),
    and the headline reports all eight — not the length of the slice."""
    baseline = _open_inquiries()
    ids = []
    try:
        for i in range(8):
            ids.append(
                db.run(
                    """INSERT INTO inquiries (name, email, message, created_at)
                       VALUES (?,?,?, datetime('now', ?))""",
                    (
                        f"Queue Lead {i}",
                        f"queue-lead-{i}@example.com",
                        "waiting on a reply",
                        f"-{400 - i} days",
                    ),
                )
            )

        response = admin_client.get("/admin/home")
        assert response.status_code == 200
        ctx = response.context

        assert ctx["new_inquiries"] == baseline + 8
        queue = ctx["queue"]
        assert len(queue) == 6
        assert queue[0]["name"] == "Queue Lead 0"
        assert [q["name"] for q in queue] == [f"Queue Lead {i}" for i in range(6)]
        assert ctx["oldest_wait_days"] == queue[0]["age_days"] >= 399

        # the On Deck reply lane deals the same corrected rows
        replies = [c for c in ctx["on_deck"] if c["lane"] == "reply"]
        assert replies and replies[0]["title"] == "Reply to Queue Lead 0"

        page = response.text
        assert f"{baseline + 8} inquir" in page
        assert "Convert Queue Lead 0" in page
    finally:
        for inquiry_id in ids:
            db.run("DELETE FROM inquiries WHERE id=?", (inquiry_id,))


@pytest.mark.integration
def test_home_collected_counts_a_deposit_on_a_still_open_invoice(admin_client):
    """A deposit paid against an invoice that is still open is collected cash.
    Home used to read invoices.total_cents stamped by paid_at, so it showed $0
    for that invoice while Financials — reading the payments table — showed the
    deposit. Both pages now answer the same question the same way."""
    before = admin_client.get("/admin/home").context
    client_id = _deposit_paid_invoice("home-collected-deposit", 100_000, 30_000)
    try:
        ctx = admin_client.get("/admin/home").context
        assert ctx["revenue"]["paid_cents"] == before["revenue"]["paid_cents"] + 30_000
        assert ctx["kpi"]["collected_7d_cents"] == before["kpi"]["collected_7d_cents"] + 30_000
        # the invoice is still open — nothing here depends on invoices.paid_at
        inv = db.one("SELECT status, paid_at FROM invoices WHERE slug='home-collected-deposit'")
        assert inv["status"] == "deposit_paid" and inv["paid_at"] is None

        # same window, same number as the money page
        fin = admin_client.get("/admin/financials?range=month").context
        collected = next(c for c in fin["cards"] if c["label"] == "Collected")
        assert collected["value"] == f"${ctx['revenue']['paid_cents'] / 100:,.2f}"
    finally:
        _drop_client(client_id)
