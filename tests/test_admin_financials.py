"""Financials & Reports admin — the accountant-facing seams.

These CSVs leave the building: Kevin hands them to his accountant, so a row that
silently drops (an export link with no Include params) or a row whose structure
breaks on a comma in a vendor name is a data bug, not a cosmetic one. Each test
pins one of those seams over real routes and the real SQLite schema.
"""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def admin():
    with TestClient(app) as c:
        r = c.post(
            "/admin/login",
            data={"password": config.ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303
        yield c


@pytest.fixture
def ledger():
    """One paid invoice (with its Stripe payment) + one open invoice, both dated
    now so any range that contains today holds them."""
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Ledger Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Ledger Job"))
    paid_iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status,
           created_at) VALUES (?,?,?,?,?,?,datetime('now'))""",
        (pid, "ledger-paid", "Paid Job", "[]", 50000, "paid"),
    )
    out_iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status,
           created_at) VALUES (?,?,?,?,?,?,datetime('now'))""",
        (pid, "ledger-open", "Open Job", "[]", 75000, "sent"),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
           amount_cents, kind, created_at) VALUES (?,?,?,?,?,datetime('now'))""",
        (paid_iid, "evt_ledger", "cs_ledger", 50000, "full"),
    )
    try:
        yield {"client": "Ledger Co", "paid_iid": paid_iid, "out_iid": out_iid}
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (paid_iid,))
        db.run("DELETE FROM invoices WHERE id IN (?,?)", (paid_iid, out_iid))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def _statuses(body: str, client: str) -> set[str]:
    """Status column of the seeded client's rows, parsed as real CSV."""
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == [
        "date",
        "client",
        "invoice",
        "service",
        "amount_usd",
        "sales_tax_usd",
        "status",
    ]
    return {r[-1] for r in rows[1:] if r[1] == client}


# --- A5: the bare export link must not download an empty file ---------------


def test_bare_export_link_includes_paid_and_outstanding(admin, ledger):
    """The topbar link passes only `range`. With neither Include param the old
    filter kept nothing, so the accountant got a header-only file."""
    body = admin.get("/admin/financials/income.csv?range=ytd").text
    assert _statuses(body, ledger["client"]) == {"Paid", "Outstanding"}


def test_one_checked_include_box_still_excludes_its_sibling(admin, ledger):
    """An explicit choice keeps its meaning — the default only fills in when the
    request carries no Include information at all."""
    body = admin.get("/admin/financials/income.csv?range=ytd&inc_paid=on").text
    assert _statuses(body, ledger["client"]) == {"Paid"}


def test_export_panel_with_both_boxes_unchecked_excludes_everything(admin, ledger):
    """Unchecked checkboxes are simply absent from the submission, so the panel
    sends a marker: with it, "neither" stays neither instead of becoming both."""
    body = admin.get("/admin/financials/income.csv?range=ytd&inc=1").text
    assert _statuses(body, ledger["client"]) == set()


def test_export_panel_form_sends_the_include_marker(admin):
    """The marker lives in the panel form; if it moves, the backend default
    silently starts overriding an operator's explicit 'neither'."""
    page = admin.get("/admin/financials").text
    assert '<input type="hidden" name="inc" value="1">' in page
