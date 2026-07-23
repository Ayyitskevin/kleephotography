"""Studio board context — pipeline + needs-attention strips for the CRM home."""

import calendar
import datetime as dt
import json

from fastapi import Request

from .. import db
from . import common
from .lookups import PROJECT_STATUSES

# A project sitting this many days in its current stage is flagged "stalled" on
# the kanban (terminal stages — project_closed/archived — are never flagged).
STALE_DAYS = 14


def _spark_series(table: str, today: dt.date, window: int) -> tuple[list[int], int]:
    """Daily counts for the `window` days ending `today`, on the studio clock
    (localtime). created_at is stored UTC; date(created_at,'localtime') maps each
    event onto Kevin's wall-clock calendar so the bucket clock matches the
    Python-local window clock. Without this the evening hours undercount — a
    9 PM EDT row is stored as next-day UTC, buckets onto tomorrow's UTC date, and
    falls outside a window built from local `today`. The window boundary is a
    Python-computed bound param, so SQLite never derives its own clock here.
    `table` is always a literal from this module, never user input."""
    start = (today - dt.timedelta(days=window - 1)).isoformat()
    day_strs = [(today - dt.timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]
    rows = db.all_(
        f"""SELECT date(created_at, 'localtime') AS d, COUNT(*) AS n
                       FROM {table}
                       WHERE date(created_at, 'localtime') >= ?
                       GROUP BY date(created_at, 'localtime')""",
        (start,),
    )
    b = {r["d"]: r["n"] for r in rows}
    series = [b.get(d, 0) for d in day_strs]
    return series, sum(series)


def _ctx_pipeline(today_iso: str) -> dict:
    """Pipeline board: projects (non-archived), per-stage value rollup, status
    counts, and per-stage overdue-invoice tally.

    Financial semantics: an invoice is overdue when its due_date is past on the
    OPERATOR'S WALL CLOCK (localtime, the canonical studio clock) — not UTC.
    Judging on UTC would declare an invoice overdue hours early in the evening
    EDT once UTC has rolled past midnight, which is a wrong statement about a
    client. due_date is a stored wall-clock date; compare it to local "today"
    passed as a bound param so SQLite never derives its own UTC 'now' here."""
    projects = db.all_(
        """SELECT p.*, c.name AS client_name, c.company,
                          CAST(julianday('now')
                               - julianday(COALESCE(p.stage_changed_at, p.created_at))
                               AS INTEGER) AS days_in_stage,
                          (SELECT COUNT(*) FROM invoices i WHERE i.project_id=p.id
                             AND i.status IN ('sent','viewed','deposit_paid')
                             AND i.due_date IS NOT NULL
                             AND i.due_date < ?) AS n_overdue,
                          COALESCE(
                            (SELECT SUM(total_cents) FROM invoices i WHERE i.project_id=p.id),
                            (SELECT pr.total_cents FROM proposals pr WHERE pr.project_id=p.id
                               ORDER BY (pr.status='accepted') DESC, pr.created_at DESC LIMIT 1),
                            0) AS value_cents
                          FROM projects p JOIN clients c ON c.id=p.client_id
                          WHERE p.status != 'archived'
                          ORDER BY p.created_at DESC""",
        (today_iso,),
    )
    # Pipeline value: a project's worth = its invoiced total (the contracted
    # number), falling back to its accepted/latest proposal when nothing's
    # invoiced yet. Rolled up per stage for the strip, plus a grand total and a
    # "booked" cut (retainer paid onward = money that's effectively committed).
    # Display-only — no money moves here.
    stage_value: dict = {}
    for p in projects:
        stage_value[p["status"]] = stage_value.get(p["status"], 0) + (p["value_cents"] or 0)
    pipeline_value_total = sum(stage_value.values())
    booked_value = sum(
        v
        for s, v in stage_value.items()
        if s in ("retainer_paid", "session_planning", "project_closed")
    )
    # Counts span archived too: the board's Archived column is a live drag
    # target, and its header tally was stuck at 0 when the query excluded it.
    counts = {
        r["status"]: r["n"]
        for r in db.all_("SELECT status, COUNT(*) AS n FROM projects GROUP BY status")
    }
    # Per-stage overdue-invoice rollup — projects whose current status puts
    # them in stage X AND have at least one overdue invoice. Lets the pipeline
    # strip flag "invoice 3 (2 overdue)" without an extra query.
    overdue_by_stage: dict = {}
    for p in projects:
        if p["n_overdue"]:
            overdue_by_stage[p["status"]] = overdue_by_stage.get(p["status"], 0) + 1
    return {
        "projects": projects,
        "stage_value": stage_value,
        "pipeline_value_total": pipeline_value_total,
        "booked_value": booked_value,
        "counts": counts,
        "overdue_by_stage": overdue_by_stage,
    }


def _ctx_inquiries() -> dict:
    """Open inquiries (live triage) + a recently-resolved archive tail."""
    inquiries = db.all_(
        "SELECT * FROM inquiries "
        "WHERE converted_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 25"
    )
    inquiries_archived = db.all_(
        "SELECT * FROM inquiries "
        "WHERE converted_at IS NOT NULL OR dismissed_at IS NOT NULL "
        "ORDER BY COALESCE(dismissed_at, converted_at) DESC LIMIT 50"
    )
    return {"inquiries": inquiries, "inquiries_archived": inquiries_archived}


def _ctx_licenses_expiring() -> list:
    """Licenses expiring within 45 days (or already lapsed) — active, dated,
    non-perpetual. Silent when empty. Mirrors the dedicated /licenses strip."""
    return db.all_(
        """SELECT l.id, l.title, l.usage_tier, l.ends_on,
                  c.name AS holder_name, c.company AS holder_company,
                  CAST(julianday(l.ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
           FROM licenses l JOIN clients c ON c.id=l.holder_client_id
           WHERE l.deleted_at IS NULL AND l.status='active' AND l.perpetual=0
             AND l.ends_on IS NOT NULL
             AND julianday(l.ends_on) - julianday(date('now', 'localtime')) <= 45
           ORDER BY l.ends_on"""
    )


def _ctx_retainer_drafts() -> list:
    """Retainer drafts waiting to send — invoices the recurring scheduler (or the
    manual Generate button) auto-created and that Kevin hasn't sent yet. Slice 2
    made these appear unattended, so without this strip an auto-generated draft
    can rot unsent — the manual-send doctrine's safety valve. Silent when empty;
    oldest first so the most-stale nags loudest."""
    return db.all_(
        """SELECT i.id, i.title, i.total_cents,
                  c.name AS client_name, c.company,
                  CAST(julianday(date('now')) - julianday(date(i.created_at)) AS INTEGER) AS age_days
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.recurring_plan_id IS NOT NULL AND i.status='draft'
           ORDER BY i.created_at ASC"""
    )


def _ctx_sparklines(request: Request, today: dt.date) -> dict:
    """Activity sparklines — Inquiries / Downloads / Favorites — over a 7 / 30 /
    90 day window picked via ?days=. Other values clamp to the closest
    allowed bucket so link tampering / typos always render something useful."""
    try:
        raw_days = int(request.query_params.get("days", 7))
    except ValueError:
        raw_days = 7
    spark_days_window = min((7, 30, 90), key=lambda d: abs(d - raw_days))
    spark_inq, spark_inq_total = _spark_series("inquiries", today, spark_days_window)
    spark_dl, spark_dl_total = _spark_series("downloads", today, spark_days_window)
    spark_fav, spark_fav_total = _spark_series("favorites", today, spark_days_window)
    day_strs = [
        (today - dt.timedelta(days=i)).isoformat() for i in range(spark_days_window - 1, -1, -1)
    ]
    sparklines = [
        {"label": "Inquiries", "series": spark_inq, "total": spark_inq_total},
        {"label": "Downloads", "series": spark_dl, "total": spark_dl_total},
        {"label": "Favorites", "series": spark_fav, "total": spark_fav_total},
    ]
    return {
        "sparklines": sparklines,
        "spark_days": day_strs,
        "spark_window": spark_days_window,
    }


def _ctx_upcoming() -> list:
    """Upcoming-shoots strip: next 14 days, non-archived. Also surfaces shoots
    already in the past but not yet shipped (status pre-'shooting') as overdue
    — those are the "the shoot was Tuesday and nothing's been edited" gotchas."""
    return db.all_(
        """SELECT p.id, p.title, p.status, p.shoot_date,
                  c.name AS client_name, c.company,
                  CAST(julianday(p.shoot_date) -
                       julianday(date('now', 'localtime')) AS INTEGER) AS days_out
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime', '-7 days')
             AND p.shoot_date <= date('now', 'localtime', '+14 days')
           ORDER BY p.shoot_date ASC"""
    )


def _ctx_proofing() -> list:
    """Proofing-waiting strip: galleries with proofing sections that haven't been
    filled yet — threads with ships #24 (proofing) + #28 (proofing-prompt
    email kind). Linked to projects so Kevin can find the inquiry context;
    each chip links to the gallery admin where the "Proofing prompt" email
    template is one click away. Rolled up to one row per project with N waiting
    chapters + M picks remaining."""
    proofing_waiting = db.all_(
        """SELECT p.id AS project_id, p.title AS project_title,
                  c.name AS client_name, c.company,
                  g.id AS gallery_id, g.title AS gallery_title, g.slug AS gallery_slug,
                  s.id AS section_id, s.name AS section_name, s.proof_target,
                  (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                   JOIN assets a ON a.id=f.asset_id
                   WHERE a.gallery_id=g.id AND a.section_id=s.id) AS picks
           FROM projects p JOIN clients c ON c.id=p.client_id
           JOIN galleries g ON g.project_id=p.id
           JOIN sections s ON s.gallery_id=g.id
           WHERE p.status != 'archived' AND g.published=1
             AND s.proof_target IS NOT NULL AND s.proof_target > 0
           ORDER BY p.id, s.position"""
    )
    waiting: dict = {}
    for r in proofing_waiting:
        if r["picks"] >= r["proof_target"]:
            continue  # section satisfied — skip
        proj = waiting.setdefault(
            r["project_id"],
            {
                "project_id": r["project_id"],
                "project_title": r["project_title"],
                "client_name": r["client_name"],
                "company": r["company"],
                "gallery_slug": r["gallery_slug"],
                "gallery_id": r["gallery_id"],
                "gallery_title": r["gallery_title"],
                "n_chapters": 0,
                "remaining": 0,
            },
        )
        proj["n_chapters"] += 1
        proj["remaining"] += r["proof_target"] - r["picks"]
    return list(waiting.values())


def _ctx_conflicts() -> list:
    """Booking-conflict guard: any date in the next 90 days that hosts 2+ items
    (active project shoot + active project shoot, or active shoot + pending
    booking inquiry). Silent if empty. -7 day floor catches "I just booked
    someone on a date I already had taken" near-misses."""
    conf_projects = db.all_(
        """SELECT p.id, p.title, p.status, p.shoot_date,
                  c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime', '-7 days')
             AND p.shoot_date <= date('now', 'localtime', '+90 days')"""
    )
    conf_inquiries = db.all_(
        """SELECT id, name, email, shoot_date, service
           FROM inquiries
           WHERE kind='booking' AND converted_at IS NULL AND shoot_date IS NOT NULL
             AND shoot_date >= date('now', 'localtime', '-7 days')
             AND shoot_date <= date('now', 'localtime', '+90 days')"""
    )
    by_date: dict = {}
    for r in conf_projects:
        by_date.setdefault(r["shoot_date"], []).append(
            {
                "kind": "project",
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "who": r["client_name"],
                "company": r["company"],
            }
        )
    for r in conf_inquiries:
        by_date.setdefault(r["shoot_date"], []).append(
            {
                "kind": "inquiry",
                "id": r["id"],
                "title": r["service"] or "Booking inquiry",
                "status": "pending",
                "who": r["name"],
                "company": None,
            }
        )
    return [
        {"shoot_date": d, "entries": items}
        for d, items in sorted(by_date.items())
        if len(items) >= 2
    ]


def _ctx_quota_behind(today: dt.date) -> list:
    """Retainers behind quota (Domain G) — active plans whose this-period delivery
    lags the month's run-rate. PACE-AWARE: a label is "behind" when its
    delivered count is below target × fraction-of-month-elapsed, so on-track
    retainers stay silent all month and one only surfaces once it slips behind
    pace (the gap widens toward month-end). quota is JSON, so the gap is summed
    in Python. Silent when empty; worst deficit first. 0-target lines are
    placeholders and never count as behind."""
    period = today.strftime("%Y-%m")
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    elapsed = today.day / days_in_month
    days_left = days_in_month - today.day
    quota_behind = []
    for rp in db.all_(
        """SELECT rp.id, rp.title, rp.quota,
                      c.name AS client_name, c.company
               FROM recurring_plans rp
               JOIN projects p ON p.id=rp.project_id
               JOIN clients c ON c.id=p.client_id
               WHERE rp.active=1 AND rp.deleted_at IS NULL AND rp.quota <> '[]'"""
    ):
        try:
            quota = json.loads(rp["quota"])
        except (ValueError, TypeError):
            continue
        delivered = {
            r["label"]: r["n"]
            for r in db.all_(
                "SELECT label, COALESCE(SUM(qty),0) AS n FROM retainer_deliveries "
                "WHERE plan_id=? AND period=? GROUP BY label",
                (rp["id"], period),
            )
        }
        behind = []
        for q in quota:
            target = q.get("target", 0)
            if target <= 0:
                continue
            done = delivered.get(q["label"], 0)
            if done < target * elapsed:  # behind the month's run-rate
                behind.append(
                    {"label": q["label"], "done": done, "target": target, "to_go": target - done}
                )
        if behind:
            behind.sort(key=lambda b: b["to_go"], reverse=True)
            quota_behind.append(
                {
                    "id": rp["id"],
                    "title": rp["title"],
                    "client_name": rp["client_name"],
                    "company": rp["company"],
                    "days_left": days_left,
                    "behind": behind,
                    "worst": behind[0],
                }
            )
    quota_behind.sort(key=lambda x: x["worst"]["to_go"], reverse=True)
    return quota_behind


def _ctx_content_due(today: dt.date) -> list:
    """Content due (Domain G) — calendar slots scheduled THIS period and not yet
    delivered: the "what's coming" companion to behind-quota's "what's at risk".
    A delivered slot drops off (composes with slice-4 assisted credit). Overdue
    (slot_date < today, not delivered) is INCLUDED and flagged urgent — it's the
    most actionable thing here, never hidden behind an upcoming-only filter. One
    chip PER PLAN (soonest/overdue item + "+N more"), plans sorted by their most
    urgent slot. A plan may ALSO show in behind-quota; that co-occurrence is
    accepted — the strips answer different questions (no cross-strip dedup).
    Carryover (overdue-rollover VISIBILITY fix): undelivered slots from PRIOR
    periods (slot_date before this period's first day, still planned/shot) stay
    visible instead of vanishing when the month rolls over — an owed shoot doesn't
    disappear just because the period turned. Delivering it still drops it off
    (status leaves planned/shot). Future-period look-ahead remains period-bounded
    (month-end blindness for UPCOMING slots is the accepted idiom). Read-only."""
    period = today.strftime("%Y-%m")
    today_iso = today.isoformat()
    soon_iso = (today + dt.timedelta(days=3)).isoformat()
    due_by_plan = {}
    for s in db.all_(
        """SELECT cc.plan_id, cc.slot_date, cc.label, cc.title,
                      rp.title AS plan_title, c.name AS client_name, c.company
               FROM content_calendar cc
               JOIN recurring_plans rp ON rp.id=cc.plan_id
               JOIN projects p ON p.id=rp.project_id
               JOIN clients c ON c.id=p.client_id
               WHERE rp.active=1 AND rp.deleted_at IS NULL
                 AND cc.status IN ('planned','shot')
                 AND (substr(cc.slot_date,1,7)=? OR cc.slot_date < ?)
               ORDER BY cc.slot_date, cc.id""",
        (period, f"{period}-01"),
    ):
        item = {
            "slot_date": s["slot_date"],
            "label": s["label"],
            "title": s["title"],
            "overdue": s["slot_date"] < today_iso,
            "urgent": s["slot_date"] <= soon_iso,
        }  # overdue ⊆ urgent
        g = due_by_plan.setdefault(
            s["plan_id"],
            {
                "id": s["plan_id"],
                "title": s["plan_title"],
                "client_name": s["client_name"],
                "company": s["company"],
                "slots": [],
            },
        )
        g["slots"].append(item)
    content_due = list(due_by_plan.values())
    for g in content_due:
        g["worst"] = g["slots"][0]  # SQL ordered slots soonest-first
    content_due.sort(key=lambda g: g["worst"]["slot_date"])
    return content_due


def _ctx_press_confirm() -> list:
    """Press → confirm published (Domain H, H2 rollup of H3's per-license cue). H3
    renders, on a license detail page, a "review & confirm published" cue when
    published press evidence matches a license whose `published` flag is still 0.
    This strip rolls that cue up to the dashboard so a matched-but-unconfirmed
    license doesn't stay hidden on its detail page. ACTIVE licenses only — the
    actionable case is a live grant; draft/expired/renewed/terminated stay quiet.
    Reuses press_for_license verbatim (deferred import breaks the studio<->press
    <->licenses cycle, as license_detail does). READ-ONLY: never flips
    `published` — the human does that on the license form (the control H3 sits
    beside). Silent when empty; most-evidence first; chip links to the detail
    where the evidence and the Published checkbox live."""
    from .press import press_for_license

    press_confirm = []
    for lic in db.all_(
        """SELECT l.*, c.name AS holder_name, c.company AS holder_company
               FROM licenses l JOIN clients c ON c.id=l.holder_client_id
               WHERE l.deleted_at IS NULL AND l.status='active' AND l.published=0"""
    ):
        hits = press_for_license(lic)
        if hits:
            press_confirm.append(
                {
                    "id": lic["id"],
                    "title": lic["title"],
                    "holder_name": lic["holder_name"],
                    "company": lic["holder_company"],
                    "usage_tier": lic["usage_tier"],
                    "n": len(hits),
                    "latest": hits[0],
                }
            )
    press_confirm.sort(key=lambda x: x["n"], reverse=True)
    return press_confirm


def _ctx_intel() -> dict:
    """Per-project intel line for the board cards — the latest thing a client
    did (or hasn't done) with this project's documents, composed from
    timestamps that already exist. Later lifecycle stages override earlier
    ones. Read-only; the card's one-tap next action navigates to the project
    page where the real (form-posted) action lives."""
    intel: dict = {}
    for r in db.all_(
        "SELECT project_id, status FROM proposals WHERE status IN ('sent','viewed','accepted')"
    ):
        intel[r["project_id"]] = {
            "sent": {"text": "Proposal out — not opened yet", "tone": "muted", "act": "Follow up"},
            "viewed": {
                "text": "Proposal opened — awaiting answer",
                "tone": "gold",
                "act": "Nudge proposal",
            },
            "accepted": {"text": "Proposal accepted", "tone": "go", "act": "Send contract"},
        }[r["status"]]
    for r in db.all_(
        "SELECT project_id, status FROM contracts WHERE status IN ('sent','viewed','signed')"
    ):
        intel[r["project_id"]] = (
            {"text": "Contract signed", "tone": "go", "act": "Send invoice"}
            if r["status"] == "signed"
            else {
                "text": "Contract out — awaiting signature",
                "tone": "gold",
                "act": "Nudge contract",
            }
        )
    for r in db.all_(
        """SELECT project_id, status FROM invoices
           WHERE status IN ('sent','viewed','deposit_paid') AND project_id IS NOT NULL"""
    ):
        intel[r["project_id"]] = (
            {"text": "Retainer in — plan the shoot", "tone": "go", "act": "Open plan"}
            if r["status"] == "deposit_paid"
            else {"text": "Invoice out — unpaid", "tone": "gold", "act": "Nudge invoice"}
        )
    return intel


def _studio_context(request: Request) -> dict:
    """Shared context for the Studio board and its Activity sub-view. Both render
    from the same pipeline + needs-attention computation: the board template reads
    the project/stage subset, the activity template the strips + sparklines. Kept
    as one assembler so the "X needs action" badge on the board and the strips it
    links to can never drift out of sync — each strip is one _ctx_* helper.

    One clock: every strip — the financial overdue boundary and the
    activity/calendar strips alike — reads studio._today(), the monkeypatchable
    canonical studio wall-clock, so a pinned 'today' moves the whole board
    coherently and nothing silently falls back to an unpinnable dt.date.today().
    Lazy import avoids a load-time cycle (studio imports this assembler)."""
    from . import studio

    today = studio._today()
    today_iso = today.isoformat()
    return {
        "statuses": PROJECT_STATUSES,
        "stale_days": STALE_DAYS,
        "intel": _ctx_intel(),
        **_ctx_pipeline(today_iso),
        "outstanding": common.open_invoice_balance(),
        **_ctx_inquiries(),
        "licenses_expiring": _ctx_licenses_expiring(),
        "retainer_drafts": _ctx_retainer_drafts(),
        **_ctx_sparklines(request, today),
        "upcoming": _ctx_upcoming(),
        "proofing_waiting": _ctx_proofing(),
        "conflicts": _ctx_conflicts(),
        "quota_behind": _ctx_quota_behind(today),
        "content_due": _ctx_content_due(today),
        "press_confirm": _ctx_press_confirm(),
    }
