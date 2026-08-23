"""Home dashboard rollups (app/admin/home_context, rendered by activity.home).

The dashboard is read-only, but it is the page Kevin acts from, so the numbers
have to be honest: the reply queue must surface the longest-waiting leads, and
every "collected" figure must agree with Financials/Reports.
"""

import calendar as cal
import datetime as dt
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import home_context, studio
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


# --- context surface ---------------------------------------------------------
#
# Jinja renders an undefined name as the empty string instead of raising, so a
# context key that stops being produced is an invisible hole in the live
# dashboard, not a red test. These pins name every key the handler returns —
# and the keys inside the nested dicts and row lists — so dropping one fails
# here loudly rather than blanking a panel in production.

# Supplied by the framework, not by the handler: `request` from
# TemplateResponse, the other two from render.templates' context processor.
_FRAMEWORK_KEYS = {"request", "csp_nonce", "static_rev"}

_HOME_KEYS = {
    "action_items",
    "activity_24h",
    "base_url",
    "docs_in_flight",
    "greeting",
    "horizon_shoots",
    "kpi",
    "link_clients",
    "mini_cal",
    "new_inquiries",
    "next_steps",
    "oldest_wait_days",
    "on_deck",
    "open_invoices",
    "open_tasks",
    "orphans",
    "outstanding",
    "overdue_inv",
    "pipeline",
    "queue",
    "recent_galleries",
    "recent_paid",
    "retainer_drafts",
    "revenue",
    "revenue_months",
    "today_str",
    "upcoming_n",
}

# Column sets of the raw-row lists, spelled out so a dropped SELECT column is a
# failure here rather than a blank cell on the dashboard.
_ROW_KEYS = {
    "orphans": {"id", "slug", "title"},
    "link_clients": {"id", "name", "company"},
    "open_tasks": {"id", "title", "due_date", "project_id", "project_title", "overdue"},
    "horizon_shoots": {
        "id",
        "title",
        "client_name",
        "company",
        "days_out",
        "day",
        "mon",
        "aerial",
    },
    "open_invoices": {
        "id",
        "title",
        "total_cents",
        "deposit_cents",
        "status",
        "due_date",
        "client_name",
        "company",
        "overdue",
    },
    "recent_paid": {"id", "title", "total_cents", "paid_at", "client_name", "company"},
    "activity_24h": {"kind", "who", "detail", "ts"},
    "docs_in_flight": {"kind", "status", "title", "client_name", "company", "ts", "url"},
    "recent_galleries": {
        "id",
        "title",
        "published",
        "client_id",
        "client_company",
        "client_name",
        "cover_id",
        "n_assets",
    },
    "queue": {"id", "name", "initials", "age_days", "context", "second"},
    "revenue_months": {"label", "cents", "pct", "current"},
    "pipeline": {"key", "label", "n", "projects"},
    "next_steps": {"tone", "key", "text", "url"},
    "on_deck": {"lane", "stock", "title", "meta", "url", "action", "badge", "key", "second"},
}

_NESTED_KEYS = {
    "kpi": {"inquiries_delta", "bookings_delta", "collected_7d_cents"},
    "revenue": {
        "paid_cents",
        "paid_n",
        "outstanding_cents",
        "goal_cents",
        "goal_pct",
        "month_label",
    },
    "mini_cal": {"weeks", "shoot_days", "today_day", "month_label"},
    "outstanding": {"n", "cents"},
}


@pytest.fixture
def seeded_dashboard():
    """Enough studio to light up every panel at once.

    The row-shape pins are only real if no list is empty, so this seeds one of
    each: a stale lead, an overdue invoice, a paid invoice, a draft gallery with
    an asset, an orphaned published gallery, a task, a shoot inside the horizon,
    a payment, a proposal in flight and a project awaiting its retainer. The
    ages are deliberately extreme so the seeded rows survive each panel's
    ORDER BY … LIMIT against whatever else the shared suite database holds.
    """
    client_id = db.run(
        "INSERT INTO clients (name, company) VALUES (?,?)", ("Context Pin", "Context Pin LLC")
    )
    project_id = db.run(
        """INSERT INTO projects (client_id, title, status, shoot_date, stage_changed_at)
           VALUES (?,?, 'contract_signed', date('now', 'localtime', '+2 days'),
                   datetime('now', '-30 days'))""",
        (client_id, "Context Pin Shoot"),
    )
    overdue_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status, due_date, sent_at)
           VALUES (?,?,?,?, 'sent', date('now', 'localtime', '-500 days'),
                   datetime('now', '-500 days'))""",
        (project_id, "ctx-pin-overdue", "Context Pin overdue", 250_000),
    )
    paid_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status, paid_at)
           VALUES (?,?,?,?, 'paid', datetime('now'))""",
        (project_id, "ctx-pin-paid", "Context Pin paid", 120_000),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, amount_cents, kind)
           VALUES (?,?,?, 'full')""",
        (paid_id, "evt_ctx_pin", 120_000),
    )
    db.run(
        """INSERT INTO proposals (project_id, slug, title, total_cents, status, sent_at)
           VALUES (?,?,?,?, 'sent', datetime('now'))""",
        (project_id, "ctx-pin-proposal", "Context Pin proposal", 300_000),
    )
    task_id = db.run(
        "INSERT INTO tasks (title, due_date, project_id) VALUES (?, date('now','-400 days'), ?)",
        ("Context Pin task", project_id),
    )
    draft_gallery = db.run(
        """INSERT INTO galleries (slug, title, pin, published, type, client_id)
           VALUES (?,?, '0000', 0, 'gallery', ?)""",
        ("ctx-pin-draft", "Context Pin draft", client_id),
    )
    db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'ctx-pin.jpg', 'ctx-pin.jpg', 'ready')""",
        (draft_gallery,),
    )
    orphan_gallery = db.run(
        """INSERT INTO galleries (slug, title, pin, published, type, client_id)
           VALUES (?,?, '0000', 1, 'gallery', NULL)""",
        ("ctx-pin-orphan", "Context Pin orphan"),
    )
    stale_lead = db.run(
        """INSERT INTO inquiries (name, email, business, message, service, created_at)
           VALUES (?,?,?,?,?, datetime('now', '-2000 days'))""",
        ("Context Pin Lead", "ctx-pin@example.com", "Pin Diner", "hello?", "Menu shoot"),
    )
    fresh_lead = db.run(
        """INSERT INTO inquiries (name, email, business, message)
           VALUES (?,?,?,?)""",
        ("Context Pin Fresh", "ctx-pin-fresh@example.com", "Pin Cafe", "just now"),
    )
    try:
        yield {"client_id": client_id, "project_id": project_id}
    finally:
        db.run("DELETE FROM inquiries WHERE id IN (?,?)", (stale_lead, fresh_lead))
        db.run("DELETE FROM assets WHERE gallery_id=?", (draft_gallery,))
        db.run("DELETE FROM galleries WHERE id IN (?,?)", (draft_gallery, orphan_gallery))
        db.run("DELETE FROM tasks WHERE id=?", (task_id,))
        db.run("DELETE FROM proposals WHERE project_id=?", (project_id,))
        db.run("DELETE FROM payments WHERE invoice_id IN (?,?)", (overdue_id, paid_id))
        db.run("DELETE FROM invoices WHERE id IN (?,?)", (overdue_id, paid_id))
        db.run("DELETE FROM projects WHERE id=?", (project_id,))
        db.run("DELETE FROM clients WHERE id=?", (client_id,))


@pytest.mark.integration
def test_home_publishes_exactly_this_set_of_context_keys(admin_client, seeded_dashboard):
    """The whole context surface, named. Anything added or dropped fails here."""
    ctx = admin_client.get("/admin/home").context
    assert set(ctx) - _FRAMEWORK_KEYS == _HOME_KEYS


@pytest.mark.integration
def test_home_row_lists_are_populated_and_keep_their_columns(admin_client, seeded_dashboard):
    """Every list panel has rows, and every row carries exactly its columns."""
    ctx = admin_client.get("/admin/home").context
    for name, expected in _ROW_KEYS.items():
        rows = ctx[name]
        assert rows, f"{name} is empty — the shape pin below would not bite"
        for row in rows:
            assert set(row.keys()) == expected, name


@pytest.mark.integration
def test_home_nested_dicts_keep_their_keys(admin_client, seeded_dashboard):
    """The four composed dicts, whose keys templates read as `revenue.goal_pct`
    and friends — a dropped one renders blank rather than raising."""
    ctx = admin_client.get("/admin/home").context
    for name, expected in _NESTED_KEYS.items():
        assert set(ctx[name].keys()) == expected, name


@pytest.mark.integration
def test_home_scalar_values_keep_their_types(admin_client, seeded_dashboard):
    """Cheap value pins for the keys with no rows of their own — enough that a
    key surviving as the wrong thing (None, a stray string) still fails."""
    ctx = admin_client.get("/admin/home").context

    assert ctx["greeting"] in ("Good morning", "Good afternoon", "Good evening")
    assert ctx["today_str"] == dt.date.today().strftime("%A, %B %-d")
    assert ctx["base_url"] == config.BASE_URL
    for name in ("new_inquiries", "upcoming_n", "overdue_inv", "retainer_drafts", "action_items"):
        assert isinstance(ctx[name], int), name
    assert ctx["action_items"] == ctx["overdue_inv"] + ctx["retainer_drafts"] + _tasks_due()
    assert ctx["oldest_wait_days"] == ctx["queue"][0]["age_days"]
    assert len(ctx["revenue_months"]) == 6
    assert [s["key"] for s in ctx["pipeline"]] == [k for k, _ in home_context.PIPELINE_STAGES]
    assert ctx["mini_cal"]["today_day"] == dt.date.today().day


def _tasks_due() -> int:
    return db.one(
        "SELECT COUNT(*) AS n FROM tasks WHERE done=0 AND due_date IS NOT NULL "
        "AND due_date <= date('now', 'localtime')"
    )["n"]


# --- one financial clock ------------------------------------------------------
#
# Home used to read dt.date.today() and SQLite's own date('now','localtime'),
# neither of which a test can pin, so nothing below could be asserted at all.
# Both are now the one monkeypatchable studio wall-clock, studio._today().

_PINNED = dt.date(2027, 4, 15)


@pytest.fixture
def pinned_clock(monkeypatch):
    """The studio clock frozen far from any real 'now', so a panel that quietly
    kept its own clock reads a different month, not merely a different second."""
    assert _PINNED != dt.date.today(), "the pin must not coincide with the real day"
    monkeypatch.setattr(studio, "_today", lambda: _PINNED)
    return _PINNED


@pytest.fixture
def clock_boundary_rows():
    """One row on each side of every date boundary Home judges, dated relative
    to the pinned day — far enough from the real day that the real clock puts
    all of them on the same (wrong) side."""
    client_id = db.run("INSERT INTO clients (name) VALUES (?)", ("Clock Pin Co",))
    project_id = db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?, 'session_planning', ?)",
        (client_id, "Clock Pin Shoot", (_PINNED + dt.timedelta(days=3)).isoformat()),
    )
    late_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status, due_date)
           VALUES (?,?,?,?, 'sent', ?)""",
        (
            project_id,
            "clock-pin-late",
            "Clock Pin late",
            90_000,
            (_PINNED - dt.timedelta(days=1)).isoformat(),
        ),
    )
    early_id = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status, due_date)
           VALUES (?,?,?,?, 'sent', ?)""",
        (
            project_id,
            "clock-pin-early",
            "Clock Pin early",
            80_000,
            (_PINNED + dt.timedelta(days=1)).isoformat(),
        ),
    )
    overdue_task = db.run(
        "INSERT INTO tasks (title, due_date, project_id) VALUES (?,?,?)",
        ("Clock Pin overdue task", (_PINNED - dt.timedelta(days=1)).isoformat(), project_id),
    )
    future_task = db.run(
        "INSERT INTO tasks (title, due_date, project_id) VALUES (?,?,?)",
        ("Clock Pin future task", (_PINNED + dt.timedelta(days=1)).isoformat(), project_id),
    )
    try:
        yield {"project_id": project_id, "late": late_id, "early": early_id}
    finally:
        db.run("DELETE FROM tasks WHERE id IN (?,?)", (overdue_task, future_task))
        db.run("DELETE FROM invoices WHERE id IN (?,?)", (late_id, early_id))
        db.run("DELETE FROM projects WHERE id=?", (project_id,))
        db.run("DELETE FROM clients WHERE id=?", (client_id,))


@pytest.mark.integration
def test_every_dated_home_panel_reads_the_pinned_studio_clock(admin_client, pinned_clock):
    """The greeting, the revenue reel, the month-to-date snapshot and the mini
    calendar all print the pinned day. Any one of them still reading its own
    clock would print the real month here."""
    ctx = admin_client.get("/admin/home").context

    assert ctx["today_str"] == "Thursday, April 15"
    assert [m["label"] for m in ctx["revenue_months"]] == ["NOV", "DEC", "JAN", "FEB", "MAR", "APR"]
    assert ctx["revenue"]["month_label"] == "April"
    assert ctx["mini_cal"]["month_label"] == "April 2027"
    assert ctx["mini_cal"]["today_day"] == 15
    assert ctx["mini_cal"]["weeks"] == cal.Calendar(firstweekday=6).monthdayscalendar(2027, 4)


@pytest.mark.integration
def test_home_row_boundaries_are_judged_on_the_pinned_studio_clock(
    admin_client, pinned_clock, clock_boundary_rows
):
    """The boundaries that decide what a row SAYS, not just what a label reads:
    days-out on the horizon, LATE on an invoice, red on a task. Each is asserted
    on both sides so a boundary stuck on the real clock fails rather than
    happening to agree."""
    ctx = admin_client.get("/admin/home").context

    shoot = next(s for s in ctx["horizon_shoots"] if s["title"] == "Clock Pin Shoot")
    assert shoot["days_out"] == 3
    assert ctx["upcoming_n"] >= 1

    by_title = {i["title"]: i for i in ctx["open_invoices"]}
    assert by_title["Clock Pin late"]["overdue"]
    assert not by_title["Clock Pin early"]["overdue"]

    tasks = {t["title"]: t for t in ctx["open_tasks"]}
    assert tasks["Clock Pin overdue task"]["overdue"]
    assert not tasks["Clock Pin future task"]["overdue"]


@pytest.mark.integration
def test_mini_calendar_dots_come_from_the_month_it_draws(
    admin_client, pinned_clock, clock_boundary_rows
):
    """The grid and the dots on it are one panel: a shoot in the pinned month
    dots the pinned grid. Selecting dots by SQLite's own month would leave the
    April 2027 grid wearing whatever the real month's shoots were."""
    ctx = admin_client.get("/admin/home").context

    assert (_PINNED + dt.timedelta(days=3)).day in ctx["mini_cal"]["shoot_days"]


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
def test_reply_queue_ages_a_lead_against_the_same_clock_that_stamped_it(admin_client):
    """inquiries.created_at is SQLite datetime('now'), i.e. UTC. Ageing it
    against a naive local now subtracts the zone offset from every lead, so
    west of Greenwich a lead that has waited three full days reports "2d" for
    part of every day — the queue under-reports exactly the thing it exists to
    escalate. Pins a real offset because CI runs UTC, where the bug is invisible.
    """
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    inquiry_id = None
    try:
        inquiry_id = db.run(
            """INSERT INTO inquiries (name, email, message, created_at)
               VALUES (?,?,?, datetime('now', '-3 days'))""",
            ("Offset Lead", "offset-lead@example.com", "waiting on a reply"),
        )
        queue = admin_client.get("/admin/home").context["queue"]
        row = next(q for q in queue if q["name"] == "Offset Lead")
        assert row["age_days"] == 3
    finally:
        if inquiry_id is not None:
            db.run("DELETE FROM inquiries WHERE id=?", (inquiry_id,))
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


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


@pytest.mark.integration
def test_month_money_buckets_an_evening_payment_in_the_local_month(admin_client):
    """A 9:30 PM payment on the last of the month is that month's money. It is
    stored UTC, where the same instant is already the first of the next month,
    so both the reel and the month-to-date headline must bucket in local time."""
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        first_of_month = dt.date.today().replace(day=1)
        last_month = first_of_month - dt.timedelta(days=1)
        # 21:30 local on the last of last month, as SQLite stores it
        stored_utc = f"{first_of_month.isoformat()} 01:30:00"

        def month_cents(ctx, day):
            label = day.strftime("%b").upper()
            return next(m["cents"] for m in ctx["revenue_months"] if m["label"] == label)

        before = admin_client.get("/admin/home").context
        client_id = _deposit_paid_invoice("home-month-edge", 50_000, 12_500, paid_at=stored_utc)
        try:
            ctx = admin_client.get("/admin/home").context
            assert month_cents(ctx, last_month) == month_cents(before, last_month) + 12_500
            # …and it stays out of this month, on the bar and in the headline
            assert month_cents(ctx, first_of_month) == month_cents(before, first_of_month)
            assert ctx["revenue"]["paid_cents"] == before["revenue"]["paid_cents"]
        finally:
            _drop_client(client_id)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


@pytest.mark.integration
def test_the_wire_carries_favourites_and_client_notes(admin_client):
    """The two signals a photographer most wants used to happen in silence.

    Favouriting is the strongest buying signal in the business, and a client
    note is a change request; neither reached the owner anywhere.
    """
    gid = db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        ("wire-signals", "Copper Crane — Dinner", "1234"),
    )
    vid = db.run(
        "INSERT INTO visitors (gallery_id,token) VALUES (?,?)", (gid, "wire-visitor-token")
    )
    assets = [
        db.run(
            "INSERT INTO assets (gallery_id,kind,filename,stored,status) "
            "VALUES (?,'photo',?,?,'ready')",
            (gid, f"w{n}.jpg", f"w{n}.jpg"),
        )
        for n in range(3)
    ]
    try:
        for asset_id in assets:
            db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, asset_id))
        db.run(
            """INSERT INTO video_comments
               (asset_id, gallery_id, visitor_id, author_role, timecode, body)
               VALUES (?,?,?,'client',0,?)""",
            (assets[0], gid, vid, "Can we get a tighter crop on this one?"),
        )
        # The studio's own reply is not news to the studio.
        db.run(
            """INSERT INTO video_comments
               (asset_id, gallery_id, visitor_id, author_role, timecode, body)
               VALUES (?,?,NULL,'admin',0,?)""",
            (assets[0], gid, "On it"),
        )

        wire = admin_client.get("/admin/home").context["activity_24h"]
        by_kind = {row["kind"]: row for row in wire}

        assert "favorite" in by_kind, "a client circling their picks never reached the deck"
        # Three favourites are ONE event, not three rows — otherwise a 40-frame
        # selection pushes everything else off an eight-row wire.
        assert len([r for r in wire if r["kind"] == "favorite"]) == 1
        assert by_kind["favorite"]["who"] == "Copper Crane — Dinner"
        assert by_kind["favorite"]["detail"] == "circled 3 takes"

        assert "note" in by_kind, "a client change request never reached the deck"
        assert "tighter crop" in by_kind["note"]["detail"]
        assert not any("On it" in (r["detail"] or "") for r in wire), "studio reply echoed back"
    finally:
        db.run("DELETE FROM video_comments WHERE gallery_id=?", (gid,))
        db.run(
            "DELETE FROM favorites WHERE asset_id IN (SELECT id FROM assets WHERE gallery_id=?)",
            (gid,),
        )
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


@pytest.mark.integration
def test_a_single_favourite_reads_as_one_take(admin_client):
    """'circled 1 takes' is the kind of thing that makes a dashboard look cheap."""
    gid = db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        ("wire-singular", "One Frame", "1234"),
    )
    vid = db.run("INSERT INTO visitors (gallery_id,token) VALUES (?,?)", (gid, "wire-singular-tok"))
    aid = db.run(
        "INSERT INTO assets (gallery_id,kind,filename,stored,status) "
        "VALUES (?,'photo','s.jpg','s.jpg','ready')",
        (gid,),
    )
    try:
        db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aid))
        wire = admin_client.get("/admin/home").context["activity_24h"]
        row = next(r for r in wire if r["kind"] == "favorite" and r["who"] == "One Frame")
        assert row["detail"] == "circled 1 take"
    finally:
        db.run(
            "DELETE FROM favorites WHERE asset_id IN (SELECT id FROM assets WHERE gallery_id=?)",
            (gid,),
        )
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
