"""The three trailing-month money series, pinned before anyone unifies them.

Home's revenue reel (`activity.home`), the Financials month reel
(`financials.income`) and the Reports 12-month chart (`reports.reports` /
`revenue.csv`) each bucket collected cash by month and scale it for display.
They have drifted apart before, so the shape of each — window length, label
format, ordering, the bar-height rule — is pinned here independently of the
money definition, and independently of which module ends up owning the code.

Every assertion is a delta or a shape, never an absolute total: the suite shares
one database, so a figure that happens to be right today is not a pin.
"""

import csv
import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import studio
from app.main import app

pytestmark = pytest.mark.integration

# Big enough to be the tallest bar whatever else the suite has seeded.
_DOMINANT_CENTS = 98_765_400


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
def collected():
    """One payment and the closed invoice it settled, both stamped `now`.

    Seeding both sides keeps the pins readable to either definition of
    "collected" — the payments table (Financials, Reports) and invoices closed
    by paid_at (what Home read historically) — so these tests pin the series
    shape without also taking a position on that question.
    """
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Month Series Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Series Job"))
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents,
           status, created_at, paid_at)
           VALUES (?,?,?,?,?, 'paid', datetime('now'), datetime('now'))""",
        (pid, "month-series", "Series Job", "[]", _DOMINANT_CENTS),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
           amount_cents, kind, created_at)
           VALUES (?,?,?,?, 'full', datetime('now'))""",
        (iid, "evt_month_series", "cs_month_series", _DOMINANT_CENTS),
    )
    try:
        yield {"cents": _DOMINANT_CENTS, "invoice_id": iid}
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def _trailing_months(last: dt.date, n: int) -> list[dt.date]:
    """The n first-of-month dates ending at `last`, oldest first."""
    out, cursor = [], last.replace(day=1)
    for _ in range(n):
        out.append(cursor)
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
    out.reverse()
    return out


# --- Home revenue reel -------------------------------------------------------


def test_home_reel_is_six_trailing_months_ending_with_this_one(admin):
    reel = admin.get("/admin/home").context["revenue_months"]
    expected = _trailing_months(dt.date.today(), 6)

    assert [m["label"] for m in reel] == [d.strftime("%b").upper() for d in expected]
    assert [m["current"] for m in reel] == [False] * 5 + [True]
    # An empty month still draws a stub bar rather than vanishing.
    assert all(4 <= m["pct"] <= 100 for m in reel)


def test_home_reel_scales_to_the_best_month_when_no_goal_is_set(admin, collected, monkeypatch):
    monkeypatch.setattr(config, "MONTHLY_GOAL_CENTS", 0)
    reel = admin.get("/admin/home").context["revenue_months"]
    assert reel[-1]["pct"] == 100


def test_home_reel_leaves_headroom_for_a_higher_goal(admin, collected, monkeypatch):
    """Home alone scales against the monthly goal, so the dashed goal silhouette
    stays the tallest thing on the card. The Financials reel has no goal."""
    monkeypatch.setattr(config, "MONTHLY_GOAL_CENTS", _DOMINANT_CENTS * 4)
    reel = admin.get("/admin/home").context["revenue_months"]
    assert reel[-1]["pct"] < 100


# --- Financials month reel ---------------------------------------------------


def test_financials_reel_is_six_trailing_months_on_the_studio_clock(admin, monkeypatch):
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2026, 6, 13))
    reel = admin.get("/admin/financials").context["month_reel"]

    assert [m["label"] for m in reel] == ["JAN", "FEB", "MAR", "APR", "MAY", "JUN"]
    assert [m["current"] for m in reel] == [False] * 5 + [True]
    assert all(4 <= m["pct"] <= 100 for m in reel)


def test_financials_reel_ignores_the_monthly_goal(admin, collected, monkeypatch):
    """The counterpart to the Home headroom pin: a goal must not shrink these
    bars, or the two reels would disagree about the same six months."""
    monkeypatch.setattr(config, "MONTHLY_GOAL_CENTS", _DOMINANT_CENTS * 4)
    reel = admin.get("/admin/financials").context["month_reel"]
    assert reel[-1]["pct"] == 100


# --- Reports 12-month chart --------------------------------------------------


def test_reports_chart_is_twelve_trailing_months_labelled_mon_yy(admin, monkeypatch):
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2026, 6, 13))
    ctx = admin.get("/admin/reports").context

    assert [m["label"] for m in ctx["chart"]] == [
        d.strftime("%b %y") for d in _trailing_months(dt.date(2026, 6, 13), 12)
    ]
    assert ctx["chart_max"] >= max(m["cents"] for m in ctx["chart"])
    assert ctx["chart_max"] >= 1  # never divide by zero on an empty studio


# --- revenue.csv -------------------------------------------------------------


def _revenue_rows(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body)))


def test_revenue_csv_is_one_ascending_row_per_month(admin, collected):
    rows = _revenue_rows(admin.get("/admin/reports/revenue.csv").text)

    assert rows[0] == ["month", "collected_usd"]
    months = [r[0] for r in rows[1:]]
    assert months == sorted(months)
    assert len(months) == len(set(months))
    assert all(len(r) == 2 for r in rows)


def test_revenue_csv_amounts_are_dollars_with_two_decimals(admin, collected):
    before = {r[0]: r[1] for r in _revenue_rows(admin.get("/admin/reports/revenue.csv").text)}
    this_month = dt.date.today().strftime("%Y-%m")

    assert this_month in before
    assert before[this_month] == f"{float(before[this_month]):.2f}"
    # the seeded payment is in there, at cents/100
    assert float(before[this_month]) >= _DOMINANT_CENTS / 100


# --- one payment, three surfaces ---------------------------------------------


def _financials_collected(ctx) -> int:
    card = next(c for c in ctx["cards"] if c["label"] == "Collected")
    return round(float(card["value"].replace("$", "").replace(",", "")) * 100)


def _income_csv_paid_cents(body: str) -> int:
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0][0] == "date" and rows[0][-1] == "status"
    return round(sum(float(r[4]) for r in rows[1:] if r[-1] == "Paid") * 100)


def test_one_payment_moves_home_financials_and_the_csv_by_the_same_amount(admin, request):
    """Home, the Financials page and income.csv are three renderings of one
    fact. Whatever each counts as collected, a single payment has to move all
    three by exactly its own amount — otherwise two pages state different
    revenue for the same month and the accountant gets a third number."""
    home_before = admin.get("/admin/home").context["revenue"]["paid_cents"]
    fin_before = _financials_collected(admin.get("/admin/financials?range=month").context)
    csv_before = _income_csv_paid_cents(
        admin.get("/admin/financials/income.csv?range=month&inc=1&inc_paid=on").text
    )

    seeded = request.getfixturevalue("collected")

    home_after = admin.get("/admin/home").context["revenue"]["paid_cents"]
    fin_after = _financials_collected(admin.get("/admin/financials?range=month").context)
    csv_after = _income_csv_paid_cents(
        admin.get("/admin/financials/income.csv?range=month&inc=1&inc_paid=on").text
    )

    assert home_after - home_before == seeded["cents"]
    assert fin_after - fin_before == seeded["cents"]
    assert csv_after - csv_before == seeded["cents"]
