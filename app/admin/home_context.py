"""Home dashboard context — the glanceable landing page's read-only rollups."""

import calendar as cal
import datetime as dt

from .. import config, db
from . import common

# Active sales-funnel stages for the Home pipeline strip (archived is terminal,
# shown only in the deep Studio view). Mirrors the projects.status CHECK
# constraint (migration 031) minus 'archived'.
PIPELINE_STAGES = [
    ("inquiry_received", "Inquiry"),
    ("consultation_call", "Consult"),
    ("proposal_sent", "Proposal"),
    ("contract_signed", "Contract"),
    ("retainer_paid", "Retainer"),
    ("session_planning", "Planning"),
    ("project_closed", "Closed"),
    ("archived", "Archived"),
]


def _ctx_greeting(today: dt.date) -> dict:
    """Time-of-day salutation and the long date beneath it. The salutation reads
    the clock's hour rather than `today`, so it moves through the day while the
    date line stays fixed to the studio's calendar day."""
    hour = dt.datetime.now().hour
    return {
        "greeting": "Good morning"
        if hour < 12
        else "Good afternoon"
        if hour < 18
        else "Good evening",
        "today_str": today.strftime("%A, %B %-d"),
    }


def _ctx_orphans() -> dict:
    """Published galleries with no studio client — orphans, usually from a client
    force-delete or a manual unlink. A live link means Kevin's lost the
    inquiry/proposal/invoice context, so surface them with a one-click re-link
    picker. Re-homed here from the galleries dashboard in the strict-1:1 rebuild
    (the grid card has no warn glyph). Drafts (unpublished) are fine — not nagged.
    The client list is only worth loading when there's something to re-link."""
    orphans = db.all_("""SELECT id, slug, title FROM galleries
                         WHERE type='gallery' AND published=1 AND client_id IS NULL
                         ORDER BY created_at DESC""")
    return {"orphans": orphans, "link_clients": db.clients_for_select() if orphans else []}


def _ctx_headline_counts() -> dict:
    """The headline stat tiles, plus the action-items backlog they roll up into.

    Every date boundary compares against date('now','localtime') — the operator's
    wall clock — so a shoot or a due date never flips a day early in the evening
    once UTC has rolled past midnight. `outstanding` is the shared AR figure and
    is read once here; the revenue snapshot reuses it rather than re-querying."""
    new_inquiries = db.one(
        "SELECT COUNT(*) AS n FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL"
    )["n"]
    outstanding = common.open_invoice_balance()
    upcoming_n = db.one(
        """SELECT COUNT(*) AS n FROM projects
           WHERE status != 'archived' AND shoot_date IS NOT NULL
             AND shoot_date >= date('now', 'localtime')
             AND shoot_date <= date('now', 'localtime', '+14 days')"""
    )["n"]
    overdue_inv = db.one(
        """SELECT COUNT(*) AS n FROM invoices
           WHERE status IN ('sent','viewed','deposit_paid')
             AND due_date IS NOT NULL AND due_date < date('now', 'localtime')"""
    )["n"]
    retainer_drafts = db.one(
        """SELECT COUNT(*) AS n FROM invoices
           WHERE recurring_plan_id IS NOT NULL AND status='draft'"""
    )["n"]
    tasks_due = db.one(
        """SELECT COUNT(*) AS n FROM tasks
           WHERE done=0 AND due_date IS NOT NULL
             AND due_date <= date('now', 'localtime')"""
    )["n"]
    return {
        "new_inquiries": new_inquiries,
        "outstanding": outstanding,
        "upcoming_n": upcoming_n,
        "overdue_inv": overdue_inv,
        "retainer_drafts": retainer_drafts,
        "action_items": overdue_inv + retainer_drafts + tasks_due,
    }


def _ctx_kpi() -> dict:
    """KPI secondary lines. Flow tiles (inquiries, bookings) get an honest
    7d-vs-prior-7d delta from a real timestamp. Stock tiles (action-items
    backlog, AR balance) have no stored history, so we show a point-in-time
    context figure instead of fabricating a week-over-week delta.

    Collected money — every figure on this page sums the `payments` table
    (Stripe webhook events), the same ground truth Financials and Reports
    read. Summing invoices.total_cents by paid_at would count a
    deposit-paid-but-still-open invoice as zero and attribute cash to the day
    the invoice closed rather than the day it arrived."""
    inq_7d = db.one(
        "SELECT COUNT(*) AS n FROM inquiries WHERE created_at >= datetime('now', '-7 days')"
    )["n"]
    inq_prev = db.one(
        "SELECT COUNT(*) AS n FROM inquiries "
        "WHERE created_at >= datetime('now', '-14 days') "
        "AND created_at < datetime('now', '-7 days')"
    )["n"]
    book_7d = db.one(
        "SELECT COUNT(*) AS n FROM projects WHERE shoot_date IS NOT NULL "
        "AND created_at >= datetime('now', '-7 days')"
    )["n"]
    book_prev = db.one(
        "SELECT COUNT(*) AS n FROM projects WHERE shoot_date IS NOT NULL "
        "AND created_at >= datetime('now', '-14 days') "
        "AND created_at < datetime('now', '-7 days')"
    )["n"]
    collected_7d = db.one(
        "SELECT COALESCE(SUM(amount_cents), 0) AS cents FROM payments "
        "WHERE created_at >= datetime('now', '-7 days')"
    )["cents"]
    return {
        "inquiries_delta": inq_7d - inq_prev,
        "bookings_delta": book_7d - book_prev,
        "collected_7d_cents": collected_7d,
    }


def _ctx_open_tasks() -> list:
    """Undone tasks, soonest due first, undated last. Overdue is judged on the
    wall clock so a task doesn't turn red an evening early."""
    return db.all_(
        """SELECT t.id, t.title, t.due_date, t.project_id, p.title AS project_title,
                  (t.due_date IS NOT NULL AND t.due_date < date('now', 'localtime'))
                    AS overdue
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=0
           ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC LIMIT 6"""
    )


def _ctx_reply_queue() -> dict:
    """Reply queue (v4 dashboard) — open inquiries as actionable rows with an
    honest waiting age and a channel-appropriate second action. Oldest first
    in SQL, not in Python: the ones that have waited longest are the point of
    the queue, so they must survive the LIMIT. The headline count is
    new_inquiries (all of them), never this six-row slice. No writes."""
    queue = []
    for r in db.all_(
        """SELECT * FROM inquiries
           WHERE converted_at IS NULL AND dismissed_at IS NULL
           ORDER BY created_at ASC LIMIT 6"""
    ):
        try:
            age_days = (dt.datetime.now() - dt.datetime.fromisoformat(r["created_at"][:19])).days
        except (ValueError, TypeError):
            age_days = 0
        is_sms = bool(r["phone"]) and not r["email"]
        queue.append(
            {
                "id": r["id"],
                "name": r["name"] or r["phone"] or "Unknown",
                "initials": ("".join(w[0] for w in (r["name"] or "").split()[:2]).upper()) or "+1",
                "age_days": age_days,
                "context": " · ".join(
                    x
                    for x in (
                        ("Booking inquiry via /book" if r["kind"] == "booking" else None),
                        ("SMS inquiry — replies by text" if is_sms else None),
                        r["business"],
                        r["email"] or r["phone"],
                        (r["service"] or None),
                    )
                    if x
                ),
                "second": "Text back"
                if is_sms
                else ("Draft proposal" if r["kind"] == "booking" else "Book intro call"),
            }
        )
    return {"queue": queue, "oldest_wait_days": queue[0]["age_days"] if queue else 0}


def _ctx_recent_galleries() -> list:
    """Recent galleries strip (v4) — Pixieset-style covers. Display-only."""
    return db.all_(
        """SELECT g.id, g.title, g.published, g.client_id,
                  c.company AS client_company, c.name AS client_name,
                  COALESCE(g.cover_asset_id,
                    (SELECT a.id FROM assets a WHERE a.gallery_id=g.id
                       AND a.status='ready' AND a.kind='photo'
                     ORDER BY a.position, a.id LIMIT 1)) AS cover_id,
                  (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id) AS n_assets
           FROM galleries g LEFT JOIN clients c ON c.id=g.client_id
           WHERE g.type='gallery'
           ORDER BY g.created_at DESC LIMIT 5"""
    )


def _ctx_revenue_months(today: dt.date) -> list:
    """Trailing six months of collected revenue for the money-card bars —
    current month last, heights proportional to the best month (or the goal
    if it's higher, so the dashed goal silhouette stays the tallest thing).

    Home is the only one of the three trailing-month reels that scales against
    the goal; Financials and Reports scale to their own best month."""
    rev_months = []
    first_of_month = today.replace(day=1)
    cursor = first_of_month
    for _ in range(6):
        rev_months.append(cursor)
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
    rev_months.reverse()
    # Bucket in local time (like _spark_series): payments are stored UTC, so an
    # evening payment on the last of the month is already tomorrow — and next
    # month — by UTC. Kevin's month ends when his day does.
    month_cents = {
        r["ym"]: r["cents"]
        for r in db.all_(
            """SELECT strftime('%Y-%m', created_at, 'localtime') AS ym,
                      COALESCE(SUM(amount_cents), 0) AS cents
               FROM payments GROUP BY ym"""
        )
    }
    scale = max(
        [month_cents.get(m.strftime("%Y-%m"), 0) for m in rev_months]
        + [config.MONTHLY_GOAL_CENTS or 0, 1]
    )
    return [
        {
            "label": m.strftime("%b").upper(),
            "cents": month_cents.get(m.strftime("%Y-%m"), 0),
            "pct": max(4, round(month_cents.get(m.strftime("%Y-%m"), 0) * 100 / scale)),
            "current": m == first_of_month,
        }
        for m in rev_months
    ]


def _ctx_horizon_shoots() -> list:
    """The next seven days of shoots as day-strip cards. The aerial flag rides
    bookings.notes (zero-schema, see public/scheduling.py) so a shoot card can
    carry its preflight cue without a column for it."""
    return db.all_(
        """SELECT p.id, p.title, c.name AS client_name, c.company,
                  CAST(julianday(p.shoot_date) -
                       julianday(date('now', 'localtime')) AS INTEGER) AS days_out,
                  CAST(strftime('%d', p.shoot_date) AS INTEGER) AS day,
                  CASE strftime('%m', p.shoot_date)
                    WHEN '01' THEN 'Jan' WHEN '02' THEN 'Feb' WHEN '03' THEN 'Mar'
                    WHEN '04' THEN 'Apr' WHEN '05' THEN 'May' WHEN '06' THEN 'Jun'
                    WHEN '07' THEN 'Jul' WHEN '08' THEN 'Aug' WHEN '09' THEN 'Sep'
                    WHEN '10' THEN 'Oct' WHEN '11' THEN 'Nov' WHEN '12' THEN 'Dec'
                  END AS mon,
                  EXISTS(SELECT 1 FROM bookings b WHERE b.project_id=p.id
                         AND b.notes LIKE '%AERIAL PASS%') AS aerial
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime')
             AND p.shoot_date <= date('now', 'localtime', '+7 days')
           ORDER BY p.shoot_date ASC"""
    )


def _ctx_invoices() -> dict:
    """The two invoice strips: what's still owed (soonest due first, undated
    last) and what recently settled. Overdue is judged on the wall clock — an
    invoice must not read LATE hours before the client's day is over."""
    open_invoices = db.all_(
        """SELECT i.id, i.title, i.total_cents, i.deposit_cents, i.status,
                  i.due_date, c.name AS client_name, c.company,
                  (i.due_date IS NOT NULL AND i.due_date < date('now', 'localtime'))
                    AS overdue
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
           ORDER BY (i.due_date IS NULL), i.due_date ASC LIMIT 6"""
    )
    recent_paid = db.all_(
        """SELECT i.id, i.title, i.total_cents, i.paid_at,
                  c.name AS client_name, c.company
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status='paid'
           ORDER BY i.paid_at DESC LIMIT 6"""
    )
    return {"open_invoices": open_invoices, "recent_paid": recent_paid}


def _ctx_activity_24h() -> list:
    """The wire: the last 24 hours across inquiries, downloads and sent email,
    interleaved by timestamp. The deep view is /admin/today."""
    return db.all_(
        """SELECT 'inquiry' AS kind, i.name AS who, i.business AS detail, i.created_at AS ts
           FROM inquiries i WHERE i.created_at >= datetime('now', '-24 hours')
         UNION ALL
           SELECT 'download', g.title, v.email, d.created_at
           FROM downloads d JOIN galleries g ON g.id=d.gallery_id
           LEFT JOIN visitors v ON v.id=d.visitor_id
           WHERE d.created_at >= datetime('now', '-24 hours')
         UNION ALL
           SELECT 'email', e.subject, c.name, e.created_at
           FROM emails_log e
           LEFT JOIN projects p ON p.id=e.project_id
           LEFT JOIN clients c ON c.id=p.client_id
           WHERE e.created_at >= datetime('now', '-24 hours')
         ORDER BY ts DESC LIMIT 8"""
    )


def _ctx_pipeline() -> list:
    """Pipeline board (read-only; all stages incl. archived). Every stage gets a
    column even when empty, so the board's shape doesn't change under Kevin as
    projects move; each column shows its full count and its first four cards."""
    proj_rows = db.all_(
        """SELECT p.id, p.title, p.status, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           ORDER BY p.stage_changed_at DESC, p.id DESC"""
    )
    by_stage: dict[str, list] = {k: [] for k, _ in PIPELINE_STAGES}
    for r in proj_rows:
        by_stage.setdefault(r["status"], []).append(r)
    return [
        {"key": k, "label": lbl, "n": len(by_stage[k]), "projects": by_stage[k][:4]}
        for k, lbl in PIPELINE_STAGES
    ]


def _ctx_dismissed_today() -> set:
    """Nudge keys the operator has already cleared today. A dismissal only
    suppresses for the current local day, so the nudge returns tomorrow if the
    underlying condition still holds."""
    return {
        row["nudge_key"]
        for row in db.all_(
            """SELECT nudge_key FROM dismissed_nudges
           WHERE date(dismissed_at, 'localtime') = date('now', 'localtime')"""
        )
    }


def _ctx_next_steps(dismissed_today: set) -> list:
    """Derived nudges — display-only, NEVER auto-send. Cleared nudges are
    dropped, then the list is sliced: filtering BEFORE the slice is what keeps
    up to 8 LIVE nudges on screen instead of letting dismissed ones eat the
    budget."""
    next_steps: list[dict] = []
    for r in db.all_(
        """SELECT i.id, i.title, i.due_date, c.name AS client_name, c.company
           FROM invoices i JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.due_date IS NOT NULL AND i.due_date < date('now', 'localtime')
           ORDER BY i.due_date ASC LIMIT 5"""
    ):
        who = r["company"] or r["client_name"]
        next_steps.append(
            {
                "tone": "warn",
                "key": f"inv_overdue:{r['id']}",
                "text": f"Invoice overdue — {r['title']} · {who} (due {r['due_date']})",
                "url": f"/admin/studio/invoices/{r['id']}",
            }
        )
    for r in db.all_(
        """SELECT id, name, business,
                  CAST(julianday('now') - julianday(created_at) AS INTEGER) AS age_d
           FROM inquiries
           WHERE converted_at IS NULL AND dismissed_at IS NULL
             AND created_at < datetime('now', '-2 days')
           ORDER BY created_at ASC LIMIT 5"""
    ):
        who = r["business"] or r["name"]
        next_steps.append(
            {
                "tone": "warn",
                "key": f"inq_reply:{r['id']}",
                "text": f"Reply to {who} — inquiry {r['age_d']}d old",
                "url": "/admin/inbox",
            }
        )
    for r in db.all_(
        """SELECT p.id, p.title, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status = 'contract_signed'
           ORDER BY p.stage_changed_at ASC LIMIT 5"""
    ):
        who = r["company"] or r["client_name"]
        next_steps.append(
            {
                "tone": "info",
                "key": f"retainer_send:{r['id']}",
                "text": f"Send retainer invoice — {r['title']} · {who}",
                "url": f"/admin/studio/projects/{r['id']}",
            }
        )
    for r in db.all_(
        """SELECT pr.id AS proposal_id, pr.status, p.id AS project_id, p.title,
                  c.name AS client_name, c.company
           FROM proposals pr JOIN projects p ON p.id=pr.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE pr.status IN ('sent','viewed')
           ORDER BY pr.sent_at ASC LIMIT 5"""
    ):
        who = r["company"] or r["client_name"]
        seen = "viewed, not accepted" if r["status"] == "viewed" else "sent, not viewed"
        next_steps.append(
            {
                "tone": "info",
                "key": f"prop_followup:{r['proposal_id']}",
                "text": f"Follow up proposal — {r['title']} · {who} ({seen})",
                "url": f"/admin/studio/projects/{r['project_id']}",
            }
        )
    # Client-submitted testimonials sit unpublished until moderated — surface them
    # so a self-submission never goes unnoticed (it has no other inbox).
    pending_t = db.one(
        """SELECT COUNT(*) AS n FROM testimonials t
           JOIN testimonial_requests tr ON tr.testimonial_id = t.id
           WHERE t.published = 0"""
    )["n"]
    if pending_t:
        next_steps.append(
            {
                "tone": "info",
                "key": "testimonials_review",
                "text": f"Review {pending_t} client testimonial"
                f"{'s' if pending_t != 1 else ''} awaiting publish",
                "url": "/admin/studio/testimonials",
            }
        )
    return [n for n in next_steps if n["key"] not in dismissed_today][:8]


def _ctx_on_deck(
    next_steps: list, dismissed_today: set, open_invoices: list, queue: list, recent_galleries: list
) -> list:
    """ON DECK (Screening Room 3h): the ONE ranked queue — money → replies →
    shipping → booking — merged from the read-only strips. Cards that
    correspond to a next_steps nudge carry its key so the existing dismiss
    endpoint doubles as "clear for today"; nothing here writes anything.

    Composed from lists already fetched rather than re-querying, so a card and
    the strip it came from can never disagree about the same row."""
    on_deck: list[dict] = []
    nudge_by_key = {n["key"]: n for n in next_steps}

    def _deck(lane, stock, title, meta, url, action, badge="", key=None, second=None):
        # Honor the snooze: ◯ (and the mobile swipe) say "until tomorrow", so a
        # card whose nudge was dismissed today leaves the deck for the rest of
        # the local day — it re-ranks tomorrow if the condition still holds.
        if key is not None and key in dismissed_today:
            return
        if key is not None and key not in nudge_by_key:
            key = None
        else:
            nudge_by_key.pop(key, None)
        on_deck.append(
            {
                "lane": lane,
                "stock": stock,
                "title": title,
                "meta": meta,
                "url": url,
                "action": action,
                "badge": badge,
                "key": key,
                "second": second,
            }
        )

    for r in open_invoices:
        if not r["overdue"]:
            continue
        who = r["company"] or r["client_name"]
        _deck(
            "money",
            "amber",
            f"Nudge — {r['title']} · ${(r['total_cents'] or 0) // 100:,}",
            f"{who} · due {r['due_date']}",
            f"/admin/studio/invoices/{r['id']}",
            "Nudge",
            badge="LATE",
            key=f"inv_overdue:{r['id']}",
        )
    for q in queue:
        _deck(
            "reply",
            "pl",
            f"Reply to {q['name']}",
            q["context"],
            f"/admin/inbox?sel={q['id']}",
            "Reply",
            badge=f"{q['age_days']}d",
            key=f"inq_reply:{q['id']}",
            second=q["second"],
        )
    for g in recent_galleries:
        if g["published"] or not g["n_assets"]:
            continue
        _deck(
            "shipping",
            "fb",
            f"Finish & publish — {g['title']}",
            f"{g['client_company'] or g['client_name'] or 'no client linked'}"
            f" · {g['n_assets']} asset{'s' if g['n_assets'] != 1 else ''} · draft",
            f"/admin/galleries/{g['id']}",
            "Open the bench",
        )
    # remaining nudges (retainers, proposal follow-ups, testimonials…) file in
    # as booking-lane cards so no next-step ever falls off the deck
    lane_for = {"retainer_send": "booking", "prop_followup": "booking"}
    for n in nudge_by_key.values():
        prefix = n["key"].split(":", 1)[0]
        _deck(
            lane_for.get(prefix, "booking"),
            "ok" if n["tone"] == "info" else "amber",
            n["text"],
            "",
            n["url"],
            "Open",
        )
        on_deck[-1]["key"] = n["key"]
    lane_rank = {"money": 0, "reply": 1, "shipping": 2, "booking": 3}
    on_deck.sort(key=lambda c: lane_rank.get(c["lane"], 9))
    return on_deck[:8]


def _ctx_docs_in_flight() -> list:
    """Documents in flight (lifecycle: sent -> viewed -> signed/paid), the three
    kinds interleaved by whichever timestamp is latest for each."""
    return db.all_(
        """SELECT 'Proposal' AS kind, pr.status,
                  p.title, c.name AS client_name, c.company,
                  COALESCE(pr.viewed_at, pr.sent_at) AS ts,
                  '/admin/studio/projects/' || p.id AS url
           FROM proposals pr JOIN projects p ON p.id=pr.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE pr.status IN ('sent','viewed')
         UNION ALL
           SELECT 'Contract', ct.status, p.title, c.name, c.company,
                  COALESCE(ct.viewed_at, ct.sent_at),
                  '/admin/studio/projects/' || p.id
           FROM contracts ct JOIN projects p ON p.id=ct.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE ct.status IN ('sent','viewed')
         UNION ALL
           SELECT 'Invoice', i.status, i.title, c.name, c.company,
                  COALESCE(i.viewed_at, i.sent_at),
                  '/admin/studio/invoices/' || i.id
           FROM invoices i JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
         ORDER BY ts DESC LIMIT 8"""
    )


def _ctx_revenue(outstanding, today: dt.date) -> dict:
    """Revenue snapshot: collected this month vs goal (display-only). The
    month-to-date window is the operator's calendar month — both sides of the
    comparison convert to localtime — so an evening payment on the last of the
    month lands in the month Kevin collected it, not the next one by UTC."""
    paid_mtd = db.one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS cents, COUNT(*) AS n
           FROM payments
           WHERE strftime('%Y-%m', created_at, 'localtime')
                 = strftime('%Y-%m', 'now', 'localtime')"""
    )
    goal_cents = config.MONTHLY_GOAL_CENTS
    return {
        "paid_cents": paid_mtd["cents"],
        "paid_n": paid_mtd["n"],
        "outstanding_cents": outstanding["cents"],
        "goal_cents": goal_cents,
        "goal_pct": min(100, round(paid_mtd["cents"] * 100 / goal_cents)) if goal_cents else 0,
        "month_label": today.strftime("%B"),
    }


def _ctx_mini_cal(today: dt.date) -> dict:
    """Mini month calendar with a dot on every day that hosts a live shoot.
    Weeks start Sunday to match the paper calendar Kevin reads beside it."""
    shoot_days = set()
    for r in db.all_(
        """SELECT shoot_date FROM projects
           WHERE status != 'archived' AND shoot_date IS NOT NULL
             AND strftime('%Y-%m', shoot_date) = strftime('%Y-%m', 'now', 'localtime')"""
    ):
        try:
            shoot_days.add(dt.date.fromisoformat(r["shoot_date"][:10]).day)
        except (ValueError, TypeError):
            pass
    return {
        "weeks": cal.Calendar(firstweekday=6).monthdayscalendar(today.year, today.month),
        "shoot_days": shoot_days,
        "today_day": today.day,
        "month_label": today.strftime("%B %Y"),
    }


def _home_context() -> dict:
    """Everything the Home dashboard renders, as one composition of read-only
    rollups — each panel is one _ctx_* helper. Kept as one assembler so the
    headline tiles and the strips they link to can never drift out of sync.

    One clock: the greeting line, the revenue reel, the month-to-date snapshot
    and the mini calendar all read a single `today`, so a render that straddles
    midnight cannot date one panel to yesterday and the next to today.

    Order matters in one place only: On Deck is merged from the already-fetched
    invoice, reply-queue and gallery strips plus the surviving nudges, so those
    four are computed first and handed in rather than re-queried."""
    today = dt.date.today()
    counts = _ctx_headline_counts()
    queue = _ctx_reply_queue()
    invoices = _ctx_invoices()
    recent_galleries = _ctx_recent_galleries()
    dismissed_today = _ctx_dismissed_today()
    next_steps = _ctx_next_steps(dismissed_today)
    return {
        **_ctx_greeting(today),
        **_ctx_orphans(),
        **counts,
        "kpi": _ctx_kpi(),
        "open_tasks": _ctx_open_tasks(),
        **queue,
        "recent_galleries": recent_galleries,
        "revenue_months": _ctx_revenue_months(today),
        "horizon_shoots": _ctx_horizon_shoots(),
        **invoices,
        "activity_24h": _ctx_activity_24h(),
        "pipeline": _ctx_pipeline(),
        "next_steps": next_steps,
        "on_deck": _ctx_on_deck(
            next_steps,
            dismissed_today,
            invoices["open_invoices"],
            queue["queue"],
            recent_galleries,
        ),
        "docs_in_flight": _ctx_docs_in_flight(),
        "revenue": _ctx_revenue(counts["outstanding"], today),
        "mini_cal": _ctx_mini_cal(today),
        "base_url": config.BASE_URL,
    }
