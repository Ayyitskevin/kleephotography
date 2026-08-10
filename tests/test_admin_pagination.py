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

import datetime as dt
import html
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import studio
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


@pytest.fixture
def ampersand_category_ledger():
    """51 expenses in "Props & supplies" — a real category whose name carries an
    ampersand — plus one Equipment row dated last, so it is exactly what an
    unfiltered page 2 would pick up."""
    made = []
    for i in range(51):
        made.append(
            db.run(
                """INSERT INTO expenses (spent_on, vendor, category, amount_cents,
                                         deductible_pct)
                   VALUES (?,?,'Props & supplies',?,100)""",
                (f"2098-{1 + i // 28:02d}-{2 + i % 28:02d}", f"PAMP-{i:03d}", 1000 + i),
            )
        )
    made.append(
        db.run(
            """INSERT INTO expenses (spent_on, vendor, category, amount_cents, deductible_pct)
               VALUES ('2098-01-01',?,'Equipment',999,100)""",
            ("PCTL-000",),
        )
    )
    try:
        yield {"oldest": "PAMP-000", "other_category": "PCTL-000"}
    finally:
        db.run(f"DELETE FROM expenses WHERE id IN ({','.join('?' * len(made))})", tuple(made))


def test_expense_page_link_survives_a_category_name_with_an_ampersand(
    admin, ampersand_category_ledger
):
    """ "Props & supplies" spelled into the href raw ends the `cat` pair at its
    own ampersand: the browser sends `cat=Props `, no category matches, and the
    operator lands back in the whole ledger one page down. Following the link
    the page actually renders is the only way to catch that."""
    page1 = admin.get("/admin/financials/expenses?cat=Props+%26+supplies").text
    href = re.search(r'href="([^"]*expenses\?cat=[^"]*offset=50)"', page1).group(1)

    # what a browser sends when it follows that link
    page2 = admin.get(html.unescape(href)).text
    assert ampersand_category_ledger["oldest"] in page2
    assert ampersand_category_ledger["other_category"] not in page2


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


# ── inbox threads ────────────────────────────────────────────────────────────

_INBOX_PAGE = 100


@pytest.fixture
def archived_threads():
    """101 archived inquiries, dismissed on distinct far-future stamps so they
    own the top of the archived tab's ordering."""
    made = []
    for i in range(101):
        made.append(
            db.run(
                """INSERT INTO inquiries (name, email, message, dismissed_at)
                   VALUES (?,?,?,?)""",
                (
                    f"LEAD-{i:03d}",
                    f"lead{i:03d}@example.com",
                    "Archived thread",
                    f"2099-01-01 {i // 60:02d}:{i % 60:02d}:00",
                ),
            )
        )
    try:
        yield {"ids": made, "oldest": "LEAD-000", "newest": "LEAD-100"}
    finally:
        db.run(f"DELETE FROM inquiries WHERE id IN ({','.join('?' * len(made))})", tuple(made))


def test_archived_tab_badge_and_list_stay_reachable(admin, archived_threads):
    """The badge always counted every archived thread; the list stopped at 100,
    so past that the two disagreed permanently with no way to page down."""
    archived = db.one(
        "SELECT COUNT(*) AS n FROM inquiries "
        "WHERE converted_at IS NOT NULL OR dismissed_at IS NOT NULL"
    )["n"]
    page1 = admin.get("/admin/inbox?tab=archived").text
    assert f'<span class="ib-tab-n">{archived}</span>' in page1
    assert archived_threads["newest"] in page1
    assert archived_threads["oldest"] not in page1
    assert f"/admin/inbox?tab=archived&amp;offset={_INBOX_PAGE}" in page1

    page2 = admin.get(f"/admin/inbox?tab=archived&offset={_INBOX_PAGE}").text
    assert archived_threads["oldest"] in page2
    assert archived_threads["newest"] not in page2


def test_inbox_page_link_keeps_the_tab(admin, archived_threads):
    """A pager that dropped ?tab would land the operator back in All."""
    page2 = admin.get(f"/admin/inbox?tab=archived&offset={_INBOX_PAGE}").text
    assert '/admin/inbox?tab=archived&amp;offset=0"' in page2
    assert 'class="ib-tab is-active">Archived' in page2


def test_sel_deep_link_still_prepends_from_another_page(admin, archived_threads):
    """The ?sel fix — a selected thread outside the window is fetched separately
    and prepended — has to keep working now that the window can be page 2."""
    sel = archived_threads["ids"][50]  # LEAD-050 lives on page 1
    page2 = admin.get(f"/admin/inbox?tab=archived&offset={_INBOX_PAGE}&sel={sel}").text
    assert f'class="ib-row is-active" id="ib-row-{sel}"' in page2
    assert archived_threads["oldest"] in page2


def test_thread_link_carries_the_page_it_was_clicked_from(admin, archived_threads):
    """`sel` alone always selected the right thread — inbox.py fetches an
    out-of-window selection and prepends it — but it dropped the operator back
    on page 1 to read it, losing their place in a list they had paged into.

    Page 1 keeps the short link: the default offset does not belong in it."""
    newest, oldest = archived_threads["ids"][100], archived_threads["ids"][0]

    page1 = admin.get("/admin/inbox?tab=archived").text
    assert f'href="/admin/inbox?tab=archived&sel={newest}"' in page1

    page2 = admin.get(f"/admin/inbox?tab=archived&offset={_INBOX_PAGE}").text
    assert f'href="/admin/inbox?tab=archived&sel={oldest}&offset={_INBOX_PAGE}"' in page2

    # Following it lands back on page 2 with that thread open — and open once:
    # it is inside this window, so the out-of-window prepend must not fire.
    opened = admin.get(f"/admin/inbox?tab=archived&sel={oldest}&offset={_INBOX_PAGE}").text
    assert opened.count(f'id="ib-row-{oldest}"') == 1
    assert f'class="ib-row is-active" id="ib-row-{oldest}"' in opened


# ── one pager across every surface ───────────────────────────────────────────


def _pager_shape(page: str, surface: str) -> str:
    """The rendered pager with hrefs and numbers blanked — what is left is the
    markup and wording every paginated surface is supposed to share."""
    match = re.search(r"<p [^>]*>(?:(?!</p>).)*?offset=(?:(?!</p>).)*</p>", page, re.S)
    assert match, f"no pager rendered on {surface}"
    shape = " ".join(match.group(0).split())
    return re.sub(r"\d+", "#", re.sub(r'href="[^"]*"', 'href="?"', shape))


def test_every_paginated_ledger_renders_the_same_pager(
    admin, expense_ledger, receipt_shoebox, trip_log, busy_form, press_log, past_bookings
):
    """Six surfaces grew their own copy of the Newer/Older block and drifted —
    different classes, one with an inline style. They now share one macro, so
    page 1 of every one of them is the same markup with a different href."""
    shapes = {
        surface: _pager_shape(admin.get(url).text, surface)
        for surface, url in (
            ("expenses", "/admin/financials/expenses"),
            ("receipts", "/admin/financials/receipts"),
            ("mileage", "/admin/financials/mileage"),
            ("press", "/admin/studio/press"),
            ("bookings", "/admin/scheduling/bookings"),
            ("submissions", f"/admin/forms/{busy_form['id']}/submissions"),
        )
    }
    # Submissions is the one surface that qualifies its own count, because its
    # search box filters the page rather than the ledger.
    note = "— search filters this page"
    shapes["submissions"] = shapes["submissions"].replace(f" {note}", "")

    assert len(set(shapes.values())) == 1, shapes
    assert 'class="sr-pager muted"' in next(iter(shapes.values()))
    assert "style=" not in next(iter(shapes.values()))


# ── financial range windows read the studio clock, not UTC ───────────────────


@pytest.fixture
def new_york():
    """Pins the process TZ so SQLite's 'localtime' means something, and dates a
    payment 23:30 on 31 May 2026 — already 1 June in UTC."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    created_utc = dt.datetime(2026, 5, 31, 23, 30).astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert created_utc == "2026-06-01 03:30:00"  # the TZ really took
    try:
        yield created_utc
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


@pytest.fixture
def late_payment(new_york):
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Boundary Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Late Job"))
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status)
           VALUES (?,?,?,?,?,'paid')""",
        (pid, "range-late", "Late Job", "[]", 41_500),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
           amount_cents, kind, created_at) VALUES (?,?,?,?,'full',?)""",
        (iid, "evt_range", "cs_range", 41_500, new_york),
    )
    try:
        yield {"client": "Boundary Co", "amount": "$415.00"}
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def test_range_window_keeps_a_late_payment_in_its_local_month(admin, late_payment, monkeypatch):
    """23:30 on the last of May is 1 June in UTC. Compared as raw strings it fell
    out of May's window entirely — off the page and out of the export the
    accountant reconciles May against."""
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2026, 5, 15))

    page = admin.get("/admin/financials?range=month").text
    assert late_payment["client"] in page
    assert "1 payment · this month" in page

    csv_body = admin.get("/admin/financials/income.csv?range=month&inc_paid=on").text
    row = next(r for r in csv_body.splitlines() if late_payment["client"] in r)
    assert row.startswith("2026-05-31")  # the row prints the date that selected it
    assert row.endswith(",415.00,0.00,Paid")


def test_range_window_does_not_lend_a_late_payment_to_the_next_month(
    admin, late_payment, monkeypatch
):
    """The other half of the same boundary: June must not claim May's cash."""
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2026, 6, 15))

    assert late_payment["client"] not in admin.get("/admin/financials?range=month").text
    csv_body = admin.get("/admin/financials/income.csv?range=month&inc_paid=on").text
    assert late_payment["client"] not in csv_body
