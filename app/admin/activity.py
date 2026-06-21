import calendar as cal
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .. import config, db, jobs, security
from ..render import templates

log = logging.getLogger("mise.admin.activity")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

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


@router.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    """The studio landing — HoneyBook-style 'Home': a glanceable greeting page
    with headline stat tiles, quick-create shortcuts, and panels that each link
    out to the deep view (Studio pipeline, Today feed, Galleries). Read-only
    rollups; every number is a link to where you'd act on it."""
    hour = dt.datetime.now().hour
    greeting = ("Good morning" if hour < 12
                else "Good afternoon" if hour < 18 else "Good evening")
    today_str = dt.date.today().strftime("%A, %B %-d")

    # Published galleries with no studio client — orphans, usually from a client
    # force-delete or a manual unlink. A live link means Kevin's lost the
    # inquiry/proposal/invoice context, so surface them with a one-click re-link
    # picker. Re-homed here from the galleries dashboard in the strict-1:1 rebuild
    # (the grid card has no warn glyph). Drafts (unpublished) are fine — not nagged.
    orphans = db.all_("""SELECT id, slug, title FROM galleries
                         WHERE type='gallery' AND published=1 AND client_id IS NULL
                         ORDER BY created_at DESC""")
    link_clients = (db.all_("SELECT id, name, company FROM clients ORDER BY name")
                    if orphans else [])

    new_inquiries = db.one(
        "SELECT COUNT(*) AS n FROM inquiries "
        "WHERE converted_at IS NULL AND dismissed_at IS NULL")["n"]
    outstanding = db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(CASE
             WHEN status='deposit_paid' THEN total_cents - deposit_cents
             ELSE total_cents END), 0) AS cents
           FROM invoices WHERE status IN ('sent','viewed','deposit_paid')""")
    upcoming_n = db.one(
        """SELECT COUNT(*) AS n FROM projects
           WHERE status != 'archived' AND shoot_date IS NOT NULL
             AND shoot_date >= date('now', 'localtime')
             AND shoot_date <= date('now', 'localtime', '+14 days')""")["n"]
    overdue_inv = db.one(
        """SELECT COUNT(*) AS n FROM invoices
           WHERE status IN ('sent','viewed','deposit_paid')
             AND due_date IS NOT NULL AND due_date < date('now', 'localtime')""")["n"]
    retainer_drafts = db.one(
        """SELECT COUNT(*) AS n FROM invoices
           WHERE recurring_plan_id IS NOT NULL AND status='draft'""")["n"]
    tasks_due = db.one(
        """SELECT COUNT(*) AS n FROM tasks
           WHERE done=0 AND due_date IS NOT NULL
             AND due_date <= date('now', 'localtime')""")["n"]
    action_items = overdue_inv + retainer_drafts + tasks_due

    # KPI secondary lines. Flow tiles (inquiries, bookings) get an honest
    # 7d-vs-prior-7d delta from a real timestamp. Stock tiles (action-items
    # backlog, AR balance) have no stored history, so we show a point-in-time
    # context figure instead of fabricating a week-over-week delta.
    inq_7d = db.one("SELECT COUNT(*) AS n FROM inquiries "
                    "WHERE created_at >= datetime('now', '-7 days')")["n"]
    inq_prev = db.one("SELECT COUNT(*) AS n FROM inquiries "
                      "WHERE created_at >= datetime('now', '-14 days') "
                      "AND created_at < datetime('now', '-7 days')")["n"]
    book_7d = db.one("SELECT COUNT(*) AS n FROM projects WHERE shoot_date IS NOT NULL "
                     "AND created_at >= datetime('now', '-7 days')")["n"]
    book_prev = db.one("SELECT COUNT(*) AS n FROM projects WHERE shoot_date IS NOT NULL "
                       "AND created_at >= datetime('now', '-14 days') "
                       "AND created_at < datetime('now', '-7 days')")["n"]
    collected_7d = db.one("SELECT COALESCE(SUM(total_cents), 0) AS cents FROM invoices "
                          "WHERE paid_at >= datetime('now', '-7 days')")["cents"]
    kpi = {"inquiries_delta": inq_7d - inq_prev,
           "bookings_delta": book_7d - book_prev,
           "collected_7d_cents": collected_7d}

    open_tasks = db.all_(
        """SELECT t.id, t.title, t.due_date, t.project_id, p.title AS project_title,
                  (t.due_date IS NOT NULL AND t.due_date < date('now', 'localtime'))
                    AS overdue
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=0
           ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC LIMIT 6""")

    leads = db.all_(
        """SELECT * FROM inquiries
           WHERE converted_at IS NULL AND dismissed_at IS NULL
           ORDER BY created_at DESC LIMIT 6""")
    horizon_shoots = db.all_(
        """SELECT p.id, p.title, c.name AS client_name, c.company,
                  CAST(julianday(p.shoot_date) -
                       julianday(date('now', 'localtime')) AS INTEGER) AS days_out,
                  CAST(strftime('%d', p.shoot_date) AS INTEGER) AS day,
                  CASE strftime('%m', p.shoot_date)
                    WHEN '01' THEN 'Jan' WHEN '02' THEN 'Feb' WHEN '03' THEN 'Mar'
                    WHEN '04' THEN 'Apr' WHEN '05' THEN 'May' WHEN '06' THEN 'Jun'
                    WHEN '07' THEN 'Jul' WHEN '08' THEN 'Aug' WHEN '09' THEN 'Sep'
                    WHEN '10' THEN 'Oct' WHEN '11' THEN 'Nov' WHEN '12' THEN 'Dec'
                  END AS mon
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime')
             AND p.shoot_date <= date('now', 'localtime', '+7 days')
           ORDER BY p.shoot_date ASC""")
    open_invoices = db.all_(
        """SELECT i.id, i.title, i.total_cents, i.deposit_cents, i.status,
                  i.due_date, c.name AS client_name, c.company,
                  (i.due_date IS NOT NULL AND i.due_date < date('now', 'localtime'))
                    AS overdue
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
           ORDER BY (i.due_date IS NULL), i.due_date ASC LIMIT 6""")
    recent_paid = db.all_(
        """SELECT i.id, i.title, i.total_cents, i.paid_at,
                  c.name AS client_name, c.company
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status='paid'
           ORDER BY i.paid_at DESC LIMIT 6""")
    activity_24h = db.all_(
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
         ORDER BY ts DESC LIMIT 8""")

    # --- Pipeline board (read-only; all stages incl. archived) ---
    proj_rows = db.all_(
        """SELECT p.id, p.title, p.status, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           ORDER BY p.stage_changed_at DESC, p.id DESC""")
    by_stage: dict[str, list] = {k: [] for k, _ in PIPELINE_STAGES}
    for r in proj_rows:
        by_stage.setdefault(r["status"], []).append(r)
    pipeline = [{"key": k, "label": lbl, "n": len(by_stage[k]),
                 "projects": by_stage[k][:4]} for k, lbl in PIPELINE_STAGES]

    # --- Next steps (derived nudges; display-only, NEVER auto-send) ---
    next_steps: list[dict] = []
    for r in db.all_(
        """SELECT i.id, i.title, i.due_date, c.name AS client_name, c.company
           FROM invoices i JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.due_date IS NOT NULL AND i.due_date < date('now', 'localtime')
           ORDER BY i.due_date ASC LIMIT 5"""):
        who = r["company"] or r["client_name"]
        next_steps.append({"tone": "warn",
                           "key": f"inv_overdue:{r['id']}",
                           "text": f"Invoice overdue — {r['title']} · {who} (due {r['due_date']})",
                           "url": f"/admin/studio/invoices/{r['id']}"})
    for r in db.all_(
        """SELECT id, name, business,
                  CAST(julianday('now') - julianday(created_at) AS INTEGER) AS age_d
           FROM inquiries
           WHERE converted_at IS NULL AND dismissed_at IS NULL
             AND created_at < datetime('now', '-2 days')
           ORDER BY created_at ASC LIMIT 5"""):
        who = r["business"] or r["name"]
        next_steps.append({"tone": "warn",
                           "key": f"inq_reply:{r['id']}",
                           "text": f"Reply to {who} — inquiry {r['age_d']}d old",
                           "url": "/admin/inbox"})
    for r in db.all_(
        """SELECT p.id, p.title, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status = 'contract_signed'
           ORDER BY p.stage_changed_at ASC LIMIT 5"""):
        who = r["company"] or r["client_name"]
        next_steps.append({"tone": "info",
                           "key": f"retainer_send:{r['id']}",
                           "text": f"Send retainer invoice — {r['title']} · {who}",
                           "url": f"/admin/studio/projects/{r['id']}"})
    for r in db.all_(
        """SELECT pr.id AS proposal_id, pr.status, p.id AS project_id, p.title,
                  c.name AS client_name, c.company
           FROM proposals pr JOIN projects p ON p.id=pr.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE pr.status IN ('sent','viewed')
           ORDER BY pr.sent_at ASC LIMIT 5"""):
        who = r["company"] or r["client_name"]
        seen = "viewed, not accepted" if r["status"] == "viewed" else "sent, not viewed"
        next_steps.append({"tone": "info",
                           "key": f"prop_followup:{r['proposal_id']}",
                           "text": f"Follow up proposal — {r['title']} · {who} ({seen})",
                           "url": f"/admin/studio/projects/{r['project_id']}"})
    # Client-submitted testimonials sit unpublished until moderated — surface them
    # so a self-submission never goes unnoticed (it has no other inbox).
    pending_t = db.one(
        """SELECT COUNT(*) AS n FROM testimonials t
           JOIN testimonial_requests tr ON tr.testimonial_id = t.id
           WHERE t.published = 0""")["n"]
    if pending_t:
        next_steps.append({"tone": "info",
                           "key": "testimonials_review",
                           "text": f"Review {pending_t} client testimonial"
                                   f"{'s' if pending_t != 1 else ''} awaiting publish",
                           "url": "/admin/studio/testimonials"})
    # Drop nudges the operator has already cleared today — a dismissal only
    # suppresses for the current local day, so the list returns tomorrow if the
    # underlying condition still holds. Slice AFTER filtering so up to 8 LIVE
    # nudges still surface.
    dismissed_today = {row["nudge_key"] for row in db.all_(
        """SELECT nudge_key FROM dismissed_nudges
           WHERE date(dismissed_at, 'localtime') = date('now', 'localtime')""")}
    next_steps = [n for n in next_steps if n["key"] not in dismissed_today][:8]

    # --- Documents in flight (lifecycle: sent -> viewed -> signed/paid) ---
    docs_in_flight = db.all_(
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
         ORDER BY ts DESC LIMIT 8""")

    # --- Revenue snapshot: collected this month vs goal (display-only) ---
    paid_mtd = db.one(
        """SELECT COALESCE(SUM(total_cents), 0) AS cents, COUNT(*) AS n
           FROM invoices WHERE status='paid'
             AND strftime('%Y-%m', paid_at) = strftime('%Y-%m', 'now', 'localtime')""")
    goal_cents = config.MONTHLY_GOAL_CENTS
    revenue = {"paid_cents": paid_mtd["cents"], "paid_n": paid_mtd["n"],
               "outstanding_cents": outstanding["cents"], "goal_cents": goal_cents,
               "goal_pct": min(100, round(paid_mtd["cents"] * 100 / goal_cents))
                           if goal_cents else 0,
               "month_label": dt.date.today().strftime("%B")}

    # --- Mini month calendar with shoot dots ---
    today = dt.date.today()
    shoot_days = set()
    for r in db.all_(
        """SELECT shoot_date FROM projects
           WHERE status != 'archived' AND shoot_date IS NOT NULL
             AND strftime('%Y-%m', shoot_date) = strftime('%Y-%m', 'now', 'localtime')"""):
        try:
            shoot_days.add(dt.date.fromisoformat(r["shoot_date"][:10]).day)
        except (ValueError, TypeError):
            pass
    mini_cal = {"weeks": cal.Calendar(firstweekday=6).monthdayscalendar(
                    today.year, today.month),
                "shoot_days": shoot_days, "today_day": today.day,
                "month_label": today.strftime("%B %Y")}

    return templates.TemplateResponse(request, "admin/home.html",
                                      {"greeting": greeting, "today_str": today_str,
                                       "new_inquiries": new_inquiries,
                                       "outstanding": outstanding,
                                       "upcoming_n": upcoming_n,
                                       "action_items": action_items,
                                       "kpi": kpi,
                                       "overdue_inv": overdue_inv,
                                       "retainer_drafts": retainer_drafts,
                                       "open_tasks": open_tasks,
                                       "leads": leads,
                                       "horizon_shoots": horizon_shoots,
                                       "open_invoices": open_invoices,
                                       "recent_paid": recent_paid,
                                       "activity_24h": activity_24h,
                                       "pipeline": pipeline,
                                       "next_steps": next_steps,
                                       "docs_in_flight": docs_in_flight,
                                       "revenue": revenue,
                                       "mini_cal": mini_cal,
                                       "orphans": orphans,
                                       "link_clients": link_clients,
                                       "base_url": config.BASE_URL})


# Stable prefixes for the derived "Needs you today" nudges (activity.home).
# A dismissal is keyed "<prefix>:<id>" (or the bare prefix for the aggregate
# testimonials nudge); we allowlist the prefix so a stray POST can't pollute
# the table with junk keys that would never match a real nudge anyway.
_NUDGE_PREFIXES = frozenset(
    {"inv_overdue", "inq_reply", "retainer_send", "prop_followup",
     "testimonials_review"})


@router.post("/home/nudge/dismiss")
async def nudge_dismiss(key: str = Form(...)):
    """Clear a Home 'Needs you today' nudge for the rest of the local day.
    The nudges are recomputed from live data, so we can't mark a stored row
    done — instead we record the dismissal and home() filters it out until the
    date rolls over (it returns tomorrow if the condition still holds)."""
    if key.split(":", 1)[0] not in _NUDGE_PREFIXES:
        raise HTTPException(status_code=400, detail="unknown nudge")
    db.run("INSERT OR REPLACE INTO dismissed_nudges (nudge_key, dismissed_at) "
           "VALUES (?, datetime('now'))", (key,))
    log.info("dashboard nudge cleared for today: %s", key)
    return RedirectResponse("/admin/home", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_view(request: Request):
    failed = db.all_("SELECT * FROM jobs WHERE status='failed' "
                     "ORDER BY updated_at DESC LIMIT 50")
    recent = db.all_("SELECT * FROM jobs WHERE status!='failed' ORDER BY id DESC LIMIT 30")
    return templates.TemplateResponse(request, "admin/jobs.html",
                                      {"failed": failed, "recent": recent,
                                       "pending": jobs.pending_count()})


@router.post("/jobs/{job_id}/retry")
async def job_retry(job_id: int):
    if not jobs.retry(job_id):
        raise HTTPException(status_code=404, detail="no failed job with that id")
    return RedirectResponse("/admin/jobs", status_code=303)


@router.get("/emails", response_class=HTMLResponse)
async def emails_view(request: Request):
    rows = db.all_("""SELECT v.email, v.first_seen, g.id AS gallery_id, g.title
                      FROM visitors v JOIN galleries g ON g.id=v.gallery_id
                      WHERE v.email IS NOT NULL
                      ORDER BY v.first_seen DESC""")
    distinct = db.one("""SELECT COUNT(DISTINCT email) AS n FROM visitors
                         WHERE email IS NOT NULL""")["n"]
    return templates.TemplateResponse(request, "admin/emails.html",
                                      {"rows": rows, "distinct": distinct})


@router.get("/emails.txt", response_class=PlainTextResponse)
async def emails_export():
    rows = db.all_("""SELECT DISTINCT email FROM visitors
                      WHERE email IS NOT NULL ORDER BY email""")
    return "\n".join(r["email"] for r in rows) + ("\n" if rows else "")


@router.get("/today", response_class=HTMLResponse)
async def today_view(request: Request):
    """Single-page 'what happened in the last 24h?' across inquiries,
    downloads, favorites, sent emails, and portal visits. Threads with the
    sparklines (ship #57/#58) — sparkline says 'something happened'; this
    view says 'this is what.'"""
    inquiries_24h = db.all_(
        """SELECT * FROM inquiries
           WHERE created_at >= datetime('now', '-24 hours')
           ORDER BY created_at DESC""")
    downloads_24h = db.all_(
        """SELECT d.created_at, d.gallery_id, d.asset_id,
                  g.title AS gallery_title, g.slug AS gallery_slug,
                  v.email AS visitor_email, a.filename
           FROM downloads d
           JOIN galleries g ON g.id=d.gallery_id
           LEFT JOIN visitors v ON v.id=d.visitor_id
           LEFT JOIN assets a ON a.id=d.asset_id
           WHERE d.created_at >= datetime('now', '-24 hours')
           ORDER BY d.created_at DESC""")
    favorites_24h = db.all_(
        """SELECT g.id AS gallery_id, g.title AS gallery_title, g.slug,
                  COUNT(DISTINCT f.asset_id) AS n_assets,
                  MAX(f.created_at) AS most_recent
           FROM favorites f
           JOIN assets a ON a.id=f.asset_id
           JOIN galleries g ON g.id=a.gallery_id
           WHERE f.created_at >= datetime('now', '-24 hours')
           GROUP BY g.id ORDER BY most_recent DESC""")
    sent_24h = db.all_(
        """SELECT e.*, p.title AS project_title, c.name AS client_name
           FROM emails_log e
           LEFT JOIN projects p ON p.id=e.project_id
           LEFT JOIN clients c ON c.id=p.client_id
           WHERE e.created_at >= datetime('now', '-24 hours')
           ORDER BY e.created_at DESC""")
    portal_visits_24h = db.all_(
        """SELECT p.*, c.name AS client_name, c.company
           FROM portals p JOIN clients c ON c.id=p.client_id
           WHERE p.last_visit IS NOT NULL
             AND p.last_visit >= datetime('now', '-24 hours')
           ORDER BY p.last_visit DESC""")
    return templates.TemplateResponse(request, "admin/today.html",
                                      {"inquiries": inquiries_24h,
                                       "downloads": downloads_24h,
                                       "favorites": favorites_24h,
                                       "sent": sent_24h,
                                       "portal_visits": portal_visits_24h})


@router.get("/sent", response_class=HTMLResponse)
async def sent_emails_view(request: Request, offset: int = 0):
    """Manual send audit log — proposal/contract/invoice/delivery emails Kevin
    has fired from the studio. Paginated 50/page, newest first."""
    offset = max(0, offset)
    page_size = 50
    rows = db.all_("""SELECT e.*, p.title AS project_title,
                             c.name AS client_name, c.company
                      FROM emails_log e
                      LEFT JOIN projects p ON p.id=e.project_id
                      LEFT JOIN clients c ON c.id=p.client_id
                      ORDER BY e.created_at DESC, e.id DESC
                      LIMIT ? OFFSET ?""", (page_size, offset))
    total = db.one("SELECT COUNT(*) AS n FROM emails_log")["n"]
    kinds = {r["doc_kind"]: r["n"] for r in db.all_(
        "SELECT doc_kind, COUNT(*) AS n FROM emails_log GROUP BY doc_kind")}
    return templates.TemplateResponse(request, "admin/sent.html",
                                      {"rows": rows, "total": total,
                                       "kinds": kinds, "offset": offset,
                                       "page_size": page_size})


@router.get("/galleries/{gallery_id}/activity", response_class=HTMLResponse)
async def activity(request: Request, gallery_id: int):
    g = db.one("SELECT * FROM galleries WHERE id=?", (gallery_id,))
    visitors = db.all_("""SELECT v.*,
                          (SELECT COUNT(*) FROM downloads d WHERE d.visitor_id=v.id) AS n_dl,
                          (SELECT COUNT(*) FROM favorites f WHERE f.visitor_id=v.id) AS n_fav
                          FROM visitors v WHERE v.gallery_id=?
                          ORDER BY v.first_seen DESC""", (gallery_id,))
    downloads = db.all_("""SELECT d.created_at, d.asset_id, a.filename, v.email
                           FROM downloads d
                           LEFT JOIN assets a ON a.id=d.asset_id
                           LEFT JOIN visitors v ON v.id=d.visitor_id
                           WHERE d.gallery_id=? ORDER BY d.created_at DESC LIMIT 200""",
                        (gallery_id,))
    favorites = db.all_("""SELECT a.id, a.filename, COUNT(*) AS n
                           FROM favorites f JOIN assets a ON a.id=f.asset_id
                           WHERE a.gallery_id=? GROUP BY a.id ORDER BY n DESC, a.filename""",
                        (gallery_id,))
    return templates.TemplateResponse(request, "admin/activity.html",
                                      {"g": g, "visitors": visitors,
                                       "downloads": downloads, "favorites": favorites})


@router.get("/galleries/{gallery_id}/favorites.txt", response_class=PlainTextResponse)
async def favorites_export(gallery_id: int):
    rows = db.all_("""SELECT DISTINCT a.filename
                      FROM favorites f JOIN assets a ON a.id=f.asset_id
                      WHERE a.gallery_id=? ORDER BY a.filename""", (gallery_id,))
    return "\n".join(r["filename"] for r in rows) + ("\n" if rows else "")


# ---- Tasks (HoneyBook "Tasks" parity, Phase 3) -----------------------------

def _task_due_label(due: str | None, today: dt.date) -> tuple[str, bool]:
    """Return (label, urgent) for a task's due date relative to today.
    Urgent (overdue or due today) drives the clay due-text color in the board."""
    if not due:
        return "", False
    try:
        dd = dt.date.fromisoformat(due[:10])
    except (ValueError, TypeError):
        return "", False
    delta = (dd - today).days
    if delta < 0:
        n = -delta
        return (f"Overdue {n}d" if n <= 9 else "Overdue"), True
    if delta == 0:
        return "Today", True
    if delta == 1:
        return "Tomorrow", False
    if delta <= 6:
        return dd.strftime("%a"), False
    return dd.strftime("%b %-d"), False


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_view(request: Request):
    """Studio to-do board (strict-1:1 prototype): three columns — Today (due
    today or overdue), This week (every other open task), Done (recently
    completed). Each card toggles done via a POST form; due_date feeds the
    calendar."""
    today = dt.date.today()

    def card(r) -> dict:
        label, urgent = _task_due_label(r["due_date"], today)
        return {"id": r["id"], "title": r["title"],
                "project": r["project_title"] or "General",
                "project_id": r["project_id"], "due": label, "urgent": urgent}

    open_rows = db.all_(
        """SELECT t.id, t.title, t.due_date, t.project_id, p.title AS project_title
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=0
           ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC""")
    today_iso = today.isoformat()
    today_col, week_col = [], []
    for r in open_rows:
        due = r["due_date"]
        if due and due[:10] <= today_iso:        # overdue or due today
            today_col.append(card(r))
        else:
            week_col.append(card(r))

    done_rows = db.all_(
        """SELECT t.id, t.title, t.due_date, t.done_at, t.project_id,
                  p.title AS project_title
           FROM tasks t LEFT JOIN projects p ON p.id=t.project_id
           WHERE t.done=1 ORDER BY t.done_at DESC LIMIT 12""")
    done_col = []
    for r in done_rows:
        c = card(r)
        c["due"] = ("done " + r["done_at"][:10]) if r["done_at"] else "done"
        c["urgent"] = False
        done_col.append(c)

    week_ago = (today - dt.timedelta(days=7)).isoformat()
    done_week = db.one(
        "SELECT COUNT(*) n FROM tasks WHERE done=1 AND done_at >= ?", (week_ago,))["n"]

    columns = [
        {"key": "today", "label": "Today", "dot": "#7C2F38", "tasks": today_col},
        {"key": "week", "label": "This week", "dot": "#EDB23C", "tasks": week_col},
        {"key": "done", "label": "Done", "dot": "#2f7d57", "tasks": done_col},
    ]
    projects = db.all_(
        """SELECT id, title FROM projects WHERE status != 'archived'
           ORDER BY title""")
    return templates.TemplateResponse(request, "admin/tasks.html",
                                      {"columns": columns,
                                       "open_count": len(today_col) + len(week_col),
                                       "done_week": done_week,
                                       "projects": projects})


@router.post("/tasks")
async def task_create(title: str = Form(...), due_date: str = Form(""),
                      project_id: str = Form("")):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    due = due_date.strip() or None
    pid = int(project_id) if project_id.strip() else None
    if pid is not None and not db.one("SELECT 1 FROM projects WHERE id=?", (pid,)):
        raise HTTPException(status_code=400, detail="bad project")
    db.run("INSERT INTO tasks (title, due_date, project_id) VALUES (?, ?, ?)",
           (title, due, pid))
    log.info("task created: %s (due %s, project %s)", title, due, pid)
    return RedirectResponse("/admin/tasks", status_code=303)


@router.post("/tasks/{task_id}/toggle")
async def task_toggle(task_id: int):
    t = db.one("SELECT done FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise HTTPException(status_code=404, detail="no such task")
    if t["done"]:
        db.run("UPDATE tasks SET done=0, done_at=NULL WHERE id=?", (task_id,))
    else:
        db.run("UPDATE tasks SET done=1, done_at=datetime('now') WHERE id=?",
               (task_id,))
    log.info("task %s toggled -> done=%s", task_id, 0 if t["done"] else 1)
    return RedirectResponse("/admin/tasks", status_code=303)


@router.post("/tasks/{task_id}/delete")
async def task_delete(task_id: int):
    if not db.one("SELECT 1 FROM tasks WHERE id=?", (task_id,)):
        raise HTTPException(status_code=404, detail="no such task")
    db.run("DELETE FROM tasks WHERE id=?", (task_id,))
    log.info("task %s deleted", task_id)
    return RedirectResponse("/admin/tasks", status_code=303)


# ---- Calendar (month grid: shoots + task due dates + invoice due dates) -----

# Three-bucket palette matching the prototype legend (Shoot / Call·delivery / Money).
_CAL_BUCKET = {
    "shoot":   ("#7C2F38", "#f3e3e5"),
    "call":    ("#2f7d57", "#e1f2e9"),
    "money":   ("#9a7a2c", "#f7ecd2"),
}


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_view(request: Request, year: int = 0, month: int = 0):
    """Month grid overlaying three real date sources, bucketed to the prototype's
    legend: shoots (clay), confirmed consults (green), invoices due (gold).
    Read-only — each cell entry links to the project/booking/invoice it represents."""
    today = dt.date.today()
    if not (1 <= month <= 12) or year < 1970:
        year, month = today.year, today.month
    first = dt.date(year, month, 1)
    last = dt.date(year, month, cal.monthrange(year, month)[1])
    lo, hi = first.isoformat(), last.isoformat()

    events: dict[int, list[dict]] = {}

    def add(day_iso: str, bucket: str, label: str, url: str):
        try:
            day_d = dt.date.fromisoformat(day_iso[:10])
        except (ValueError, TypeError):
            return
        color, bg = _CAL_BUCKET[bucket]
        events.setdefault(day_d.day, []).append(
            {"label": label, "url": url, "color": color, "bg": bg})

    for r in db.all_(
        """SELECT p.id, p.title, p.shoot_date, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date BETWEEN ? AND ?""", (lo, hi)):
        who = r["company"] or r["client_name"]
        add(r["shoot_date"], "shoot",
            f"{r['title']} · {who}" if who else r["title"],
            f"/admin/studio/projects/{r['id']}")
    for r in db.all_(
        """SELECT i.id, i.title, i.due_date, c.name AS client_name, c.company
           FROM invoices i JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.due_date IS NOT NULL AND i.due_date BETWEEN ? AND ?""", (lo, hi)):
        who = r["company"] or r["client_name"]
        add(r["due_date"], "money",
            f"{r['title']} · {who}" if who else r["title"],
            f"/admin/studio/invoices/{r['id']}")
    # Confirmed consultations/bookings — start_utc is UTC; show on Kevin's local
    # day. zoneinfo handles DST. Bookings link to the bookings console.
    tz = ZoneInfo(config.TIMEZONE)
    for r in db.all_(
        """SELECT b.id, b.name, b.start_utc, e.name AS event_name FROM bookings b
           JOIN event_types e ON e.id=b.event_type_id
           WHERE b.status='confirmed' AND b.start_utc IS NOT NULL"""):
        try:
            local = (dt.datetime.fromisoformat(r["start_utc"]).replace(tzinfo=dt.timezone.utc)
                     .astimezone(tz))
        except (ValueError, TypeError):
            continue
        if not (lo <= local.date().isoformat() <= hi):
            continue
        add(local.date().isoformat(), "call",
            f"{local:%H:%M} {r['event_name']} · {r['name']}",
            "/admin/scheduling/bookings")

    # Sunday-first month grid of cells (prototype layout): leading/trailing blanks
    # so the grid is a whole number of weeks.
    weeks = cal.Calendar(firstweekday=6).monthdayscalendar(year, month)
    cells: list[dict] = []
    for week in weeks:
        for day in week:
            if day == 0:
                cells.append({"empty": True, "day": "", "today": False, "events": []})
            else:
                cells.append({
                    "empty": False, "day": day,
                    "today": (day == today.day and month == today.month
                              and year == today.year),
                    "events": events.get(day, []),
                })

    prev_m = (first - dt.timedelta(days=1))
    next_m = (last + dt.timedelta(days=1))
    return templates.TemplateResponse(request, "admin/calendar.html",
                                      {"year": year, "month": month,
                                       "month_name": first.strftime("%B"),
                                       "cells": cells, "today": today,
                                       "dow": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
                                       "prev_year": prev_m.year, "prev_month": prev_m.month,
                                       "next_year": next_m.year, "next_month": next_m.month})
