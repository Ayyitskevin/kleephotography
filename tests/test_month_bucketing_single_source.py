"""Home's month-to-date figure and the revenue chart must agree.

They are the same question — "cash collected in month X" — and they used to be
asked by two queries, each carrying its own copy of the localtime bucketing
expression. month_money_series' docstring records that this exact rule had
already drifted once when three copies of it existed.
"""

import datetime as dt

import pytest

from app import db
from app.admin import common, home_context

pytestmark = pytest.mark.integration


@pytest.fixture
def payments():
    db.run("INSERT INTO clients (name) VALUES ('MB')")
    cid = db.one("SELECT id FROM clients ORDER BY id DESC LIMIT 1")["id"]
    db.run("INSERT INTO projects (client_id,title) VALUES (?,'MB')", (cid,))
    pid = db.one("SELECT id FROM projects ORDER BY id DESC LIMIT 1")["id"]
    made = []
    # Late-evening edges are the whole reason the rule says 'localtime'.
    for n, (ts, amt) in enumerate(
        [
            ("2026-08-01 09:00:00", 12345),
            ("2026-08-31 21:00:00", 7500),
            ("2026-07-31 23:59:00", 999),
        ],
        1,
    ):
        db.run(
            "INSERT INTO invoices (project_id,slug,title,total_cents) VALUES (?,?,?,?)",
            (pid, f"mb{n}-{ts}", "MB", amt),
        )
        iid = db.one("SELECT id FROM invoices ORDER BY id DESC LIMIT 1")["id"]
        db.run(
            "INSERT INTO payments (invoice_id,stripe_event_id,amount_cents,kind,created_at) "
            "VALUES (?,?,?,'full',?)",
            (iid, f"mb-evt-{n}-{ts}", amt, ts),
        )
        made.append(iid)
    yield
    for iid in made:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def test_home_figure_matches_the_chart_bar_for_the_same_month(payments):
    today = dt.date(2026, 8, 20)
    revenue = home_context._ctx_revenue({"cents": 0}, today)
    series, _ = common.month_money_series(today, 3, lambda d: d.strftime("%Y-%m"))
    august = next(r for r in series if r["label"] == "2026-08")
    assert revenue["paid_cents"] == august["cents"], (
        "the figure at the top of Home disagrees with the bar beneath it"
    )


def test_the_count_comes_from_the_same_place_as_the_money(payments):
    by_ym = common.collected_by_month()["2026-08"]
    revenue = home_context._ctx_revenue({"cents": 0}, dt.date(2026, 8, 20))
    assert (revenue["paid_cents"], revenue["paid_n"]) == (by_ym["cents"], by_ym["n"])


def test_a_month_with_no_cash_reads_as_zero_not_a_crash(payments):
    revenue = home_context._ctx_revenue({"cents": 0}, dt.date(2026, 1, 20))
    assert revenue["paid_cents"] == 0 and revenue["paid_n"] == 0


def test_only_one_copy_of_the_bucketing_expression_remains():
    """The drift guard. A second copy is how the figure and the chart diverge."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    hits = [
        f"{p.relative_to(root.parent)}:{n}"
        for p in root.rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "strftime('%Y-%m', created_at" in line
    ]
    assert len(hits) == 1, f"month bucketing duplicated across: {hits}"
    assert hits[0].startswith("app/admin/common.py"), hits
