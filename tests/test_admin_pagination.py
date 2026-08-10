"""Admin lists that grow forever: one page at a time, with a pager that reaches
the rest.

Every surface here used to render its whole table (or truncate at a fixed LIMIT
with no way past it). Each test seeds one row more than a page, then proves the
three things a pager owes the operator: the page-1 window stops, the count is the
true total, and the older rows are actually reachable. Filters ride the page
links — a pager that drops the active filter is a new bug.

Full CSV exports are the accountant's path and stay whole; the CSV assertions
here exist to keep pagination from leaking into them.
"""

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as client:
        r = client.post(
            "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
        )
        assert r.status_code == 303
        yield client


# ── expenses ledger ──────────────────────────────────────────────────────────

_EXP_PAGE = 50


@pytest.fixture
def expense_ledger():
    """52 expenses dated far enough out that they own the top of the DESC ledger
    whatever else the shared test DB holds: 51 Travel + 1 Meals, oldest first."""
    made = []
    for i in range(52):
        cat = "Meals" if i == 51 else "Travel"
        made.append(
            db.run(
                """INSERT INTO expenses (spent_on, vendor, category, amount_cents,
                                         deductible_pct)
                   VALUES (?,?,?,?,100)""",
                (f"2099-{1 + i // 28:02d}-{1 + i % 28:02d}", f"PGV-{i:03d}", cat, 1000 + i),
            )
        )
    try:
        yield {"oldest": "PGV-000", "newest": "PGV-051"}
    finally:
        db.run(f"DELETE FROM expenses WHERE id IN ({','.join('?' * len(made))})", tuple(made))


def test_expense_ledger_pages_instead_of_rendering_every_row(admin, expense_ledger):
    page1 = admin.get("/admin/financials/expenses").text
    assert expense_ledger["newest"] in page1
    assert expense_ledger["oldest"] not in page1
    assert f"/admin/financials/expenses?cat=all&amp;offset={_EXP_PAGE}" in page1

    page2 = admin.get(f"/admin/financials/expenses?offset={_EXP_PAGE}").text
    assert expense_ledger["oldest"] in page2
    assert expense_ledger["newest"] not in page2


def test_expense_page_link_keeps_the_category_filter(admin, expense_ledger):
    """PGV-000 is Travel; paging out of the filtered view must not drop back to
    the whole ledger."""
    page1 = admin.get("/admin/financials/expenses?cat=Travel").text
    assert f"/admin/financials/expenses?cat=Travel&amp;offset={_EXP_PAGE}" in page1
    assert expense_ledger["oldest"] not in page1

    page2 = admin.get(f"/admin/financials/expenses?cat=Travel&offset={_EXP_PAGE}").text
    assert expense_ledger["oldest"] in page2
    assert "Meals" not in page2.split('<table class="fin-tbl">')[1].split("</table>")[0]


def test_expense_summary_and_csv_stay_whole_ledger(admin, expense_ledger):
    """The cards and the accountant's export read every row — only the table is
    windowed."""
    page1 = admin.get("/admin/financials/expenses").text
    total = db.one("SELECT COUNT(*) AS n FROM expenses")["n"]
    assert f"{total} logged" in page1

    csv_rows = admin.get("/admin/financials/expenses.csv").text.strip().splitlines()
    assert len(csv_rows) == total + 1
    assert any(expense_ledger["oldest"] in row for row in csv_rows)


# ── receipt inbox ────────────────────────────────────────────────────────────

_RCPT_PAGE = 50


@pytest.fixture
def receipt_shoebox():
    """52 scans, newest last: RCPT-051 is linked to an expense, the rest are not,
    so the unlinked filter still has one more than a page."""
    exp_id = db.run(
        """INSERT INTO expenses (spent_on, vendor, category, amount_cents, deductible_pct)
           VALUES ('2099-06-01','Shoebox Co','Other',4200,100)"""
    )
    made = []
    for i in range(52):
        made.append(
            db.run(
                """INSERT INTO receipts (filename, stored, content_type, size_bytes, expense_id)
                   VALUES (?,?,?,?,?)""",
                (f"RCPT-{i:03d}.pdf", f"rcpt-{i:03d}.pdf", "application/pdf", 10, exp_id)
                if i == 51
                else (f"RCPT-{i:03d}.pdf", f"rcpt-{i:03d}.pdf", "application/pdf", 10, None),
            )
        )
    try:
        yield {"oldest": "RCPT-000.pdf", "newest": "RCPT-051.pdf"}
    finally:
        db.run(f"DELETE FROM receipts WHERE id IN ({','.join('?' * len(made))})", tuple(made))
        db.run("DELETE FROM expenses WHERE id=?", (exp_id,))


def test_receipt_grid_pages_instead_of_rendering_every_scan(admin, receipt_shoebox):
    page1 = admin.get("/admin/financials/receipts").text
    assert receipt_shoebox["newest"] in page1
    assert receipt_shoebox["oldest"] not in page1
    assert f"/admin/financials/receipts?filter=all&amp;offset={_RCPT_PAGE}" in page1

    page2 = admin.get(f"/admin/financials/receipts?offset={_RCPT_PAGE}").text
    assert receipt_shoebox["oldest"] in page2
    assert receipt_shoebox["newest"] not in page2


def test_receipt_page_link_keeps_the_linked_filter(admin, receipt_shoebox):
    page1 = admin.get("/admin/financials/receipts?filter=unlinked").text
    assert f"/admin/financials/receipts?filter=unlinked&amp;offset={_RCPT_PAGE}" in page1
    assert receipt_shoebox["oldest"] not in page1

    page2 = admin.get(f"/admin/financials/receipts?filter=unlinked&offset={_RCPT_PAGE}").text
    assert receipt_shoebox["oldest"] in page2
    assert receipt_shoebox["newest"] not in page2
