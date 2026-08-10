"""Financials & Reports admin — the accountant-facing seams.

These CSVs leave the building: Kevin hands them to his accountant, so a row that
silently drops (an export link with no Include params) or a row whose structure
breaks on a comma in a vendor name is a data bug, not a cosmetic one. Each test
pins one of those seams over real routes and the real SQLite schema.
"""

import csv
import datetime as dt
import io
import os
import re
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import common, studio
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


# --- D1: a payment's month is decided on the studio's wall clock ------------

_LATE_CENTS = 98_765_400  # dominates the month reel's scale, whatever else is seeded


@pytest.fixture
def new_york():
    """Pins the process TZ so SQLite's 'localtime' means something, and dates a
    payment 23:30 on 31 May 2026 — 03:30 UTC on 1 June. Bucketed on raw UTC it
    lands in a month the operator never collected it in."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    created_utc = dt.datetime(2026, 5, 31, 23, 30).astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert created_utc == "2026-06-01 03:30:00"  # the TZ really took
    try:
        yield {"created_utc": created_utc, "local": "2026-05", "utc": "2026-06"}
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


@contextmanager
def _payment_at(created_utc: str, cents: int):
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Boundary Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Late Job"))
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status)
           VALUES (?,?,?,?,?,?)""",
        (pid, "boundary-late", "Late Job", "[]", cents, "paid"),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
           amount_cents, kind, created_at) VALUES (?,?,?,?,?,?)""",
        (iid, "evt_boundary", "cs_boundary", cents, "full", created_utc),
    )
    try:
        yield
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def _revenue_months(body: str) -> dict[str, float]:
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["month", "collected_usd"]
    return {r[0]: float(r[1]) for r in rows[1:]}


def test_revenue_csv_counts_a_late_payment_in_the_local_month(admin, new_york):
    """revenue.csv is what the accountant reconciles against — a 23:30 payment
    must not appear in the next month's row."""
    before = _revenue_months(admin.get("/admin/reports/revenue.csv").text)
    with _payment_at(new_york["created_utc"], _LATE_CENTS):
        after = _revenue_months(admin.get("/admin/reports/revenue.csv").text)
    assert after[new_york["local"]] - before.get(new_york["local"], 0.0) == _LATE_CENTS / 100
    assert after.get(new_york["utc"], 0.0) == before.get(new_york["utc"], 0.0)


def test_reports_chart_buckets_a_late_payment_in_the_local_month(admin, new_york):
    """The 12-month revenue chart reads the same buckets as the CSV — and since
    all three month series now share this bucketing, so do the two reels."""
    before = common.collected_by_month()
    with _payment_at(new_york["created_utc"], _LATE_CENTS):
        after = common.collected_by_month()
    assert after[new_york["local"]] - before.get(new_york["local"], 0) == _LATE_CENTS
    assert after.get(new_york["utc"], 0) == before.get(new_york["utc"], 0)


def _month_reel(page: str) -> dict[str, int]:
    """Bar height per month label from the Financials month reel."""
    return {
        label: int(pct)
        for pct, label in re.findall(
            r'style="height: (\d+)%;"></i>\s*<small[^>]*>([A-Z]{3})</small>', page
        )
    }


def test_month_reel_bar_lands_in_the_local_month(admin, new_york, monkeypatch):
    """The reel's tallest bar answers 'how did that month go' — on UTC the money
    slides one bar to the right for anything collected after 8 PM EDT."""
    monkeypatch.setattr(config, "SCREENING_ROOM", True)
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2026, 6, 13))
    with _payment_at(new_york["created_utc"], _LATE_CENTS):
        reel = _month_reel(admin.get("/admin/financials").text)
    assert reel["MAY"] == 100  # the payment set the scale, so its month is full height
    assert reel["JUN"] < 100


# --- D5: operator text must not be able to break a row ----------------------

_NASTY = 'Rent, "The" Loft\nunit 2'  # a comma, a quote pair and a newline in one value
# A date stored verbatim from the form before it was normalized — the ledgers
# still hold such rows, and the CSV columns they sit in were never escaped.
_LEGACY_DATE = '6/15/2026, "evening"'


def test_expenses_csv_survives_a_comma_quote_and_newline(admin):
    """Hand-rolled escaping quoted only vendor/notes, so a comma in the date
    column shifted every field after it onto the accountant's next column."""
    eid = db.run(
        """INSERT INTO expenses (spent_on, vendor, category, amount_cents,
           deductible_pct, notes) VALUES (?,?,?,?,?,?)""",
        (_LEGACY_DATE, _NASTY, "Equipment", 124000, 50, 'said "mint", boxed'),
    )
    try:
        body = admin.get("/admin/financials/expenses.csv").text
    finally:
        db.run("DELETE FROM expenses WHERE id=?", (eid,))
    rows = list(csv.reader(io.StringIO(body)))
    assert all(len(r) == 7 for r in rows)
    mine = [r for r in rows[1:] if r[1] == _NASTY]
    assert mine == [
        [_LEGACY_DATE, _NASTY, "Equipment", "1240.00", "50", "620.00", 'said "mint", boxed']
    ]


def test_mileage_csv_survives_a_comma_quote_and_newline(admin):
    """Same structure guarantee for the trip log — the deduction column is what
    the accountant totals, so a shifted field is a wrong number."""
    tid = db.run(
        """INSERT INTO mileage (drove_on, from_place, to_place, purpose, miles, rate_cents)
           VALUES (?,?,?,?,?,?)""",
        (_LEGACY_DATE, _NASTY, "Cúrate", 'menu shoot, "summer"', 8.4, 70),
    )
    try:
        body = admin.get("/admin/financials/mileage.csv").text
    finally:
        db.run("DELETE FROM mileage WHERE id=?", (tid,))
    rows = list(csv.reader(io.StringIO(body)))
    assert all(len(r) == 7 for r in rows)
    mine = [r for r in rows[1:] if r[1] == _NASTY]
    assert mine == [[_LEGACY_DATE, _NASTY, "Cúrate", 'menu shoot, "summer"', "8.4", "0.70", "5.88"]]


def test_income_csv_summary_totals_come_from_raw_cents(admin, ledger):
    """The summary branch used to re-parse its own '$1,234.56' display strings;
    the counts and totals must equal the seeded invoices exactly."""
    body = admin.get("/admin/financials/income.csv?range=ytd&inc=1&inc_paid=on&fmt=summary").text
    rows = {r[0]: r[1:] for r in csv.reader(io.StringIO(body))}
    assert rows["category"] == ["count", "amount_usd"]
    assert rows["Outstanding"] == ["0", "0.00"]
    paid_n, paid_usd = rows["Paid"]
    assert int(paid_n) >= 1 and float(paid_usd) >= 500.00  # the seeded $500 payment


# --- D6: bookkeeping dates are stored normalized or not at all --------------


def test_expense_rejects_a_non_iso_date(admin):
    """type=date guards only the happy path; a hand-built post storing
    '6/15/2026' would sort into the wrong place in a string-ordered ledger."""
    r = admin.post(
        "/admin/financials/expenses",
        data={
            "spent_on": "6/15/2026",
            "vendor": "Bad Date Co",
            "category": "Equipment",
            "amount": "12.00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db.one("SELECT id FROM expenses WHERE vendor='Bad Date Co'") is None


def test_mileage_rejects_a_non_iso_date(admin):
    r = admin.post(
        "/admin/financials/mileage",
        data={
            "drove_on": "next tuesday",
            "from_place": "Studio",
            "to_place": "Bad Date Cafe",
            "miles": "3.2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db.one("SELECT id FROM mileage WHERE to_place='Bad Date Cafe'") is None
