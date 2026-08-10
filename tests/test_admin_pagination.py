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


# ── mileage log ──────────────────────────────────────────────────────────────

_MILE_PAGE = 50


@pytest.fixture
def trip_log():
    """51 trips dated out past anything else in the shared DB, oldest first."""
    made = []
    for i in range(51):
        made.append(
            db.run(
                """INSERT INTO mileage (drove_on, from_place, to_place, purpose, miles, rate_cents)
                   VALUES (?,?,?,?,?,70)""",
                (
                    f"2099-{1 + i // 28:02d}-{1 + i % 28:02d}",
                    "Studio",
                    f"TRIP-{i:03d}",
                    "Shoot",
                    10,
                ),
            )
        )
    try:
        yield {"oldest": "TRIP-000", "newest": "TRIP-050"}
    finally:
        db.run(f"DELETE FROM mileage WHERE id IN ({','.join('?' * len(made))})", tuple(made))


def test_trip_log_pages_instead_of_rendering_every_trip(admin, trip_log):
    page1 = admin.get("/admin/financials/mileage").text
    assert trip_log["newest"] in page1
    assert trip_log["oldest"] not in page1
    assert f"/admin/financials/mileage?offset={_MILE_PAGE}" in page1

    page2 = admin.get(f"/admin/financials/mileage?offset={_MILE_PAGE}").text
    assert trip_log["oldest"] in page2
    assert trip_log["newest"] not in page2


def test_mileage_cards_and_csv_stay_whole_log(admin, trip_log):
    total = db.one("SELECT COUNT(*) AS n FROM mileage")["n"]
    assert f"{total} trips" in admin.get("/admin/financials/mileage").text

    csv_rows = admin.get("/admin/financials/mileage.csv").text.strip().splitlines()
    assert len(csv_rows) == total + 1
    assert any(trip_log["oldest"] in row for row in csv_rows)


# ── form submissions ─────────────────────────────────────────────────────────

_SUB_PAGE = 50


@pytest.fixture
def busy_form():
    """A lead form with 51 submissions, each stamped a minute apart so the
    newest-first order is not left to a same-second tie."""
    form_id = db.run(
        "INSERT INTO forms (slug, title, kind) VALUES (?,?,'lead')",
        ("pager-form", "Pager Form"),
    )
    for i in range(51):
        db.run(
            "INSERT INTO form_submissions (form_id, name, data, created_at) VALUES (?,?,'{}',?)",
            (form_id, f"SUB-{i:03d}", f"2099-01-01 {i // 60:02d}:{i % 60:02d}:00"),
        )
    try:
        yield {"id": form_id, "oldest": "SUB-000", "newest": "SUB-050"}
    finally:
        db.run("DELETE FROM form_submissions WHERE form_id=?", (form_id,))
        db.run("DELETE FROM forms WHERE id=?", (form_id,))


def test_form_submissions_page_and_count_the_whole_inbox(admin, busy_form):
    page1 = admin.get(f"/admin/forms/{busy_form['id']}/submissions").text
    assert "<b>51</b> total" in page1
    assert busy_form["newest"] in page1
    assert busy_form["oldest"] not in page1
    assert f"/admin/forms/{busy_form['id']}/submissions?offset={_SUB_PAGE}" in page1

    page2 = admin.get(f"/admin/forms/{busy_form['id']}/submissions?offset={_SUB_PAGE}").text
    assert busy_form["oldest"] in page2
    assert busy_form["newest"] not in page2


# ── press log ────────────────────────────────────────────────────────────────

_PRESS_PAGE = 50


@pytest.fixture
def press_log():
    """51 pending hits — pending floats to the top of the press order, so these
    own page 1 whatever else the shared DB holds."""
    made = []
    for i in range(51):
        made.append(db.run("INSERT INTO press (outlet) VALUES (?)", (f"PRESS-{i:03d}",)))
    try:
        yield {"oldest": "PRESS-000", "newest": "PRESS-050"}
    finally:
        db.run(f"DELETE FROM press WHERE id IN ({','.join('?' * len(made))})", tuple(made))


def test_press_log_pages_and_keeps_an_all_time_header(admin, press_log):
    page1 = admin.get("/admin/studio/press").text
    total = db.one("SELECT COUNT(*) AS n FROM press WHERE deleted_at IS NULL")["n"]
    assert page1.count('class="press-row"') == _PRESS_PAGE
    assert f"<b>{total}</b> hit" in page1 and f"Logged ({total})" in page1
    assert press_log["newest"] in page1
    assert press_log["oldest"] not in page1
    assert f"/admin/studio/press?offset={_PRESS_PAGE}" in page1

    page2 = admin.get(f"/admin/studio/press?offset={_PRESS_PAGE}").text
    assert press_log["oldest"] in page2
    assert press_log["newest"] not in page2


# ── past & cancelled bookings ────────────────────────────────────────────────

_BOOK_PAGE = 100


@pytest.fixture
def past_bookings():
    """101 cancelled bookings — one more than the window that used to be the end
    of the road."""
    event_id = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,30)",
        ("pager-shoot", "Pager Shoot"),
    )
    made = []
    for i in range(101):
        made.append(
            db.run(
                """INSERT INTO bookings (token, event_type_id, name, email, start_utc,
                                         end_utc, status)
                   VALUES (?,?,?,?,?,?,'cancelled')""",
                (
                    f"pager-token-{i:03d}",
                    event_id,
                    f"BOOK-{i:03d}",
                    "past@example.com",
                    f"2020-01-01 {i // 60:02d}:{i % 60:02d}:00",
                    f"2020-01-01 {i // 60:02d}:{i % 60:02d}:30",
                ),
            )
        )
    try:
        yield {"event_id": event_id, "names": {f"BOOK-{i:03d}" for i in range(101)}}
    finally:
        db.run(f"DELETE FROM bookings WHERE id IN ({','.join('?' * len(made))})", tuple(made))
        db.run("DELETE FROM event_types WHERE id=?", (event_id,))


def test_past_bookings_reach_beyond_the_first_window(admin, past_bookings):
    """The old query stopped at 100 rows with no pager — booking 101 existed and
    could not be seen from the admin at all."""
    total = db.one(
        """SELECT COUNT(*) AS n FROM bookings b JOIN event_types e ON e.id=b.event_type_id
           WHERE b.status!='confirmed' OR b.start_utc < datetime('now')"""
    )["n"]
    assert total <= 2 * _BOOK_PAGE  # both pages together must cover the seeded set

    page1 = admin.get("/admin/scheduling/bookings").text
    assert f"<b>{total}</b> past" in page1
    assert f"/admin/scheduling/bookings?past_offset={_BOOK_PAGE}" in page1

    page2 = admin.get(f"/admin/scheduling/bookings?past_offset={_BOOK_PAGE}").text
    seen1 = {n for n in past_bookings["names"] if n in page1}
    seen2 = {n for n in past_bookings["names"] if n in page2}
    assert seen1 != past_bookings["names"]  # page 1 really is a window
    assert seen1 | seen2 == past_bookings["names"]


def test_past_booking_page_opens_on_the_past_pane(admin, past_bookings):
    """The pager lives in a pane the toggle hides by default — following it must
    not dump the operator back on Upcoming."""
    page2 = admin.get(f"/admin/scheduling/bookings?past_offset={_BOOK_PAGE}").text
    assert '<section class="sched-block" data-bview-pane="past">' in page2
    assert '<section class="sched-block" data-bview-pane="upcoming" hidden>' in page2
    assert 'data-bview="past" class="is-active"' in page2
