"""Studio — clients & projects (the CRM spine; proposals/contracts/invoices hang off projects)."""

import calendar
import datetime as dt
import json
import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import clients, config, db, jobs, pricing, security, usage_vocab
from ..render import templates

log = logging.getLogger("mise.admin.studio")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

PROJECT_STATUSES = ["inquiry_received", "consultation_call", "proposal_sent",
                    "contract_signed", "retainer_paid", "session_planning",
                    "project_closed", "archived"]

# A project sitting this many days in its current stage is flagged "stalled" on
# the kanban (terminal stages — project_closed/archived — are never flagged).
STALE_DAYS = 14

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")
BRAND_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".eps", ".ai", ".zip"}
# Brand-KIT logos are composited server-side onto crops, so only raster formats
# Pillow can open + alpha-composite are allowed (PNG/WebP carry transparency;
# vector EPS/PDF/AI and archives can't be pasted onto a JPEG).
KIT_EXTS = {".png", ".webp", ".jpg", ".jpeg"}
KIT_POSITIONS = {"tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"}


def get_client(client_id: int) -> "db.sqlite3.Row":
    c = db.one("SELECT * FROM clients WHERE id=?", (client_id,))
    if not c:
        raise HTTPException(status_code=404)
    return c


def get_project(project_id: int) -> "db.sqlite3.Row":
    p = db.one("""SELECT p.*, c.name AS client_name, c.company, c.email AS client_email
                  FROM projects p JOIN clients c ON c.id=p.client_id WHERE p.id=?""",
               (project_id,))
    if not p:
        raise HTTPException(status_code=404)
    return p


def _today() -> dt.date:
    """Single source for the studio's wall-clock 'today' (localtime, the canonical
    studio clock). Financial date boundaries build their comparison from this and
    pass it as a bound param, so SQLite never derives its own UTC 'now' for a
    judgement that must follow the operator's wall clock. Monkeypatchable so the
    overdue financial boundary can be pinned deterministically in tests."""
    return dt.date.today()


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
    day_strs = [(today - dt.timedelta(days=i)).isoformat()
                for i in range(window - 1, -1, -1)]
    rows = db.all_(f"""SELECT date(created_at, 'localtime') AS d, COUNT(*) AS n
                       FROM {table}
                       WHERE date(created_at, 'localtime') >= ?
                       GROUP BY date(created_at, 'localtime')""", (start,))
    b = {r["d"]: r["n"] for r in rows}
    series = [b.get(d, 0) for d in day_strs]
    return series, sum(series)


@router.get("/playbook", response_class=HTMLResponse)
async def studio_playbook(request: Request):
    return templates.TemplateResponse(request, "admin/studio_playbook.html", {})


@router.get("", response_class=HTMLResponse)
async def studio_home(request: Request):
    # Financial semantics: an invoice is overdue when its due_date is past on the
    # OPERATOR'S WALL CLOCK (localtime, the canonical studio clock) — not UTC.
    # Judging on UTC would declare an invoice overdue hours early in the evening
    # EDT once UTC has rolled past midnight, which is a wrong statement about a
    # client. due_date is a stored wall-clock date; compare it to local "today"
    # passed as a bound param so SQLite never derives its own UTC 'now' here.
    today_iso = _today().isoformat()
    projects = db.all_("""SELECT p.*, c.name AS client_name, c.company,
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
                          ORDER BY p.created_at DESC""", (today_iso,))
    # Pipeline value: a project's worth = its invoiced total (the contracted
    # number), falling back to its accepted/latest proposal when nothing's
    # invoiced yet. Rolled up per stage for the strip, plus a grand total and a
    # "booked" cut (retainer paid onward = money that's effectively committed).
    # Display-only — no money moves here.
    stage_value: dict = {}
    for p in projects:
        stage_value[p["status"]] = stage_value.get(p["status"], 0) + (p["value_cents"] or 0)
    pipeline_value_total = sum(stage_value.values())
    booked_value = sum(v for s, v in stage_value.items()
                       if s in ("retainer_paid", "session_planning", "project_closed"))
    clients = db.all_("""SELECT c.*,
                         (SELECT COUNT(*) FROM projects p WHERE p.client_id=c.id) AS n_projects,
                         (SELECT po.published FROM portals po WHERE po.client_id=c.id) AS portal_published,
                         (SELECT po.visits FROM portals po WHERE po.client_id=c.id) AS portal_visits,
                         (SELECT po.last_visit FROM portals po WHERE po.client_id=c.id) AS portal_last_visit
                         FROM clients c ORDER BY c.name""")
    # Compute a friendly "visited Xh ago" / "never visited" hint per client so
    # the template stays declarative. Engagement on the client portal (Phase 2)
    # was invisible from the studio dashboard before this.
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    client_portal_hints = {}
    for c in clients:
        if c["portal_published"] is None:
            client_portal_hints[c["id"]] = ("muted", "no portal")
        elif not c["portal_last_visit"]:
            client_portal_hints[c["id"]] = ("muted", "never visited")
        else:
            try:
                last = dt.datetime.fromisoformat(c["portal_last_visit"])
            except ValueError:
                client_portal_hints[c["id"]] = ("muted", "visited (date unknown)")
                continue
            delta = now - last
            if delta.total_seconds() < 60:
                hint = "just now"
            elif delta.total_seconds() < 3600:
                hint = f"{int(delta.total_seconds() // 60)}m ago"
            elif delta.total_seconds() < 86400:
                hint = f"{int(delta.total_seconds() // 3600)}h ago"
            elif delta.days < 30:
                hint = f"{delta.days}d ago"
            else:
                hint = last.date().isoformat()
            client_portal_hints[c["id"]] = ("ok", f"👁 {hint}")
    counts = {r["status"]: r["n"] for r in db.all_(
        """SELECT status, COUNT(*) AS n FROM projects
           WHERE status != 'archived' GROUP BY status""")}
    # Per-stage overdue-invoice rollup — projects whose current status puts
    # them in stage X AND have at least one overdue invoice. Lets the pipeline
    # strip flag "invoice 3 (2 overdue)" without an extra query.
    overdue_by_stage: dict = {}
    for p in projects:
        if p["n_overdue"]:
            overdue_by_stage[p["status"]] = overdue_by_stage.get(p["status"], 0) + 1
    outstanding = db.one(
        """SELECT COUNT(*) AS n, COALESCE(SUM(CASE
             WHEN status='deposit_paid' THEN total_cents - deposit_cents
             ELSE total_cents END), 0) AS cents
           FROM invoices WHERE status IN ('sent','viewed','deposit_paid')""")
    inquiries = db.all_(
        "SELECT * FROM inquiries "
        "WHERE converted_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 25")
    inquiries_archived = db.all_(
        "SELECT * FROM inquiries "
        "WHERE converted_at IS NOT NULL OR dismissed_at IS NOT NULL "
        "ORDER BY COALESCE(dismissed_at, converted_at) DESC LIMIT 50")
    # Licenses expiring within 45 days (or already lapsed) — active, dated,
    # non-perpetual. Silent when empty. Mirrors the dedicated /licenses strip.
    licenses_expiring = db.all_(
        """SELECT l.id, l.title, l.usage_tier, l.ends_on,
                  c.name AS holder_name, c.company AS holder_company,
                  CAST(julianday(l.ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
           FROM licenses l JOIN clients c ON c.id=l.holder_client_id
           WHERE l.deleted_at IS NULL AND l.status='active' AND l.perpetual=0
             AND l.ends_on IS NOT NULL
             AND julianday(l.ends_on) - julianday(date('now', 'localtime')) <= 45
           ORDER BY l.ends_on""")
    # Retainer drafts waiting to send — invoices the recurring scheduler (or the
    # manual Generate button) auto-created and that Kevin hasn't sent yet. Slice 2
    # made these appear unattended, so without this strip an auto-generated draft
    # can rot unsent — the manual-send doctrine's safety valve. Silent when empty;
    # oldest first so the most-stale nags loudest.
    retainer_drafts = db.all_(
        """SELECT i.id, i.title, i.total_cents,
                  c.name AS client_name, c.company,
                  CAST(julianday(date('now')) - julianday(date(i.created_at)) AS INTEGER) AS age_days
           FROM invoices i
           JOIN projects p ON p.id=i.project_id
           JOIN clients c ON c.id=p.client_id
           WHERE i.recurring_plan_id IS NOT NULL AND i.status='draft'
           ORDER BY i.created_at ASC""")
    # Activity sparklines — Inquiries / Downloads / Favorites — over a 7 / 30 /
    # 90 day window picked via ?days=. Other values clamp to the closest
    # allowed bucket so link tampering / typos always render something useful.
    try:
        raw_days = int(request.query_params.get("days", 7))
    except ValueError:
        raw_days = 7
    spark_days_window = min((7, 30, 90), key=lambda d: abs(d - raw_days))
    today = dt.date.today()
    spark_inq, spark_inq_total = _spark_series("inquiries", today, spark_days_window)
    spark_dl,  spark_dl_total  = _spark_series("downloads", today, spark_days_window)
    spark_fav, spark_fav_total = _spark_series("favorites", today, spark_days_window)
    day_strs = [(today - dt.timedelta(days=i)).isoformat()
                for i in range(spark_days_window - 1, -1, -1)]
    sparklines = [
        {"label": "Inquiries", "series": spark_inq, "total": spark_inq_total},
        {"label": "Downloads", "series": spark_dl,  "total": spark_dl_total},
        {"label": "Favorites", "series": spark_fav, "total": spark_fav_total},
    ]
    # Upcoming-shoots strip: next 14 days, non-archived. Also surfaces shoots
    # already in the past but not yet shipped (status pre-'shooting') as overdue
    # — those are the "the shoot was Tuesday and nothing's been edited" gotchas.
    upcoming = db.all_(
        """SELECT p.id, p.title, p.status, p.shoot_date,
                  c.name AS client_name, c.company,
                  CAST(julianday(p.shoot_date) -
                       julianday(date('now', 'localtime')) AS INTEGER) AS days_out
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime', '-7 days')
             AND p.shoot_date <= date('now', 'localtime', '+14 days')
           ORDER BY p.shoot_date ASC""")
    # Proofing-waiting strip: galleries with proofing sections that haven't been
    # filled yet — threads with ships #24 (proofing) + #28 (proofing-prompt
    # email kind). Linked to projects so Kevin can find the inquiry context;
    # each chip links to the gallery admin where the "Proofing prompt" email
    # template is one click away.
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
           ORDER BY p.id, s.position""")
    # Roll up to one row per project with N waiting chapters + M picks remaining.
    waiting: dict = {}
    for r in proofing_waiting:
        if r["picks"] >= r["proof_target"]:
            continue  # section satisfied — skip
        proj = waiting.setdefault(r["project_id"], {
            "project_id": r["project_id"], "project_title": r["project_title"],
            "client_name": r["client_name"], "company": r["company"],
            "gallery_slug": r["gallery_slug"], "gallery_id": r["gallery_id"],
            "gallery_title": r["gallery_title"],
            "n_chapters": 0, "remaining": 0,
        })
        proj["n_chapters"] += 1
        proj["remaining"] += r["proof_target"] - r["picks"]
    # Booking-conflict guard: any date in the next 90 days that hosts 2+ items
    # (active project shoot + active project shoot, or active shoot + pending
    # booking inquiry). Silent if empty. -7 day floor catches "I just booked
    # someone on a date I already had taken" near-misses.
    conf_projects = db.all_(
        """SELECT p.id, p.title, p.status, p.shoot_date,
                  c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived' AND p.shoot_date IS NOT NULL
             AND p.shoot_date >= date('now', 'localtime', '-7 days')
             AND p.shoot_date <= date('now', 'localtime', '+90 days')""")
    conf_inquiries = db.all_(
        """SELECT id, name, email, shoot_date, service
           FROM inquiries
           WHERE kind='booking' AND converted_at IS NULL AND shoot_date IS NOT NULL
             AND shoot_date >= date('now', 'localtime', '-7 days')
             AND shoot_date <= date('now', 'localtime', '+90 days')""")
    by_date: dict = {}
    for r in conf_projects:
        by_date.setdefault(r["shoot_date"], []).append(
            {"kind": "project", "id": r["id"], "title": r["title"],
             "status": r["status"], "who": r["client_name"],
             "company": r["company"]})
    for r in conf_inquiries:
        by_date.setdefault(r["shoot_date"], []).append(
            {"kind": "inquiry", "id": r["id"], "title": r["service"] or "Booking inquiry",
             "status": "pending", "who": r["name"], "company": None})
    conflicts = [{"shoot_date": d, "entries": items}
                 for d, items in sorted(by_date.items()) if len(items) >= 2]
    # Retainers behind quota (Domain G) — active plans whose this-period delivery
    # lags the month's run-rate. PACE-AWARE: a label is "behind" when its
    # delivered count is below target × fraction-of-month-elapsed, so on-track
    # retainers stay silent all month and one only surfaces once it slips behind
    # pace (the gap widens toward month-end). quota is JSON, so the gap is summed
    # in Python. Silent when empty; worst deficit first. 0-target lines are
    # placeholders and never count as behind.
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
               WHERE rp.active=1 AND rp.deleted_at IS NULL AND rp.quota <> '[]'"""):
        try:
            quota = json.loads(rp["quota"])
        except (ValueError, TypeError):
            continue
        delivered = {r["label"]: r["n"] for r in db.all_(
            "SELECT label, COALESCE(SUM(qty),0) AS n FROM retainer_deliveries "
            "WHERE plan_id=? AND period=? GROUP BY label", (rp["id"], period))}
        behind = []
        for q in quota:
            target = q.get("target", 0)
            if target <= 0:
                continue
            done = delivered.get(q["label"], 0)
            if done < target * elapsed:        # behind the month's run-rate
                behind.append({"label": q["label"], "done": done,
                               "target": target, "to_go": target - done})
        if behind:
            behind.sort(key=lambda b: b["to_go"], reverse=True)
            quota_behind.append(
                {"id": rp["id"], "title": rp["title"],
                 "client_name": rp["client_name"], "company": rp["company"],
                 "days_left": days_left, "behind": behind, "worst": behind[0]})
    quota_behind.sort(key=lambda x: x["worst"]["to_go"], reverse=True)
    # Content due (Domain G) — calendar slots scheduled THIS period and not yet
    # delivered: the "what's coming" companion to behind-quota's "what's at risk".
    # A delivered slot drops off (composes with slice-4 assisted credit). Overdue
    # (slot_date < today, not delivered) is INCLUDED and flagged urgent — it's the
    # most actionable thing here, never hidden behind an upcoming-only filter. One
    # chip PER PLAN (soonest/overdue item + "+N more"), plans sorted by their most
    # urgent slot. A plan may ALSO show in behind-quota; that co-occurrence is
    # accepted — the strips answer different questions (no cross-strip dedup).
    # Carryover (overdue-rollover VISIBILITY fix): undelivered slots from PRIOR
    # periods (slot_date before this period's first day, still planned/shot) stay
    # visible instead of vanishing when the month rolls over — an owed shoot doesn't
    # disappear just because the period turned. Delivering it still drops it off
    # (status leaves planned/shot). Future-period look-ahead remains period-bounded
    # (month-end blindness for UPCOMING slots is the accepted idiom). Read-only.
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
               ORDER BY cc.slot_date, cc.id""", (period, f"{period}-01")):
        item = {"slot_date": s["slot_date"], "label": s["label"],
                "title": s["title"], "overdue": s["slot_date"] < today_iso,
                "urgent": s["slot_date"] <= soon_iso}  # overdue ⊆ urgent
        g = due_by_plan.setdefault(
            s["plan_id"],
            {"id": s["plan_id"], "title": s["plan_title"],
             "client_name": s["client_name"], "company": s["company"], "slots": []})
        g["slots"].append(item)
    content_due = list(due_by_plan.values())
    for g in content_due:
        g["worst"] = g["slots"][0]          # SQL ordered slots soonest-first
    content_due.sort(key=lambda g: g["worst"]["slot_date"])
    # Press → confirm published (Domain H, H2 rollup of H3's per-license cue). H3
    # renders, on a license detail page, a "review & confirm published" cue when
    # published press evidence matches a license whose `published` flag is still 0.
    # This strip rolls that cue up to the dashboard so a matched-but-unconfirmed
    # license doesn't stay hidden on its detail page. ACTIVE licenses only — the
    # actionable case is a live grant; draft/expired/renewed/terminated stay quiet.
    # Reuses press_for_license verbatim (deferred import breaks the studio<->press
    # <->licenses cycle, as license_detail does). READ-ONLY: never flips
    # `published` — the human does that on the license form (the control H3 sits
    # beside). Silent when empty; most-evidence first; chip links to the detail
    # where the evidence and the Published checkbox live.
    from .press import press_for_license
    press_confirm = []
    for lic in db.all_(
            """SELECT l.*, c.name AS holder_name, c.company AS holder_company
               FROM licenses l JOIN clients c ON c.id=l.holder_client_id
               WHERE l.deleted_at IS NULL AND l.status='active' AND l.published=0"""):
        hits = press_for_license(lic)
        if hits:
            press_confirm.append(
                {"id": lic["id"], "title": lic["title"],
                 "holder_name": lic["holder_name"], "company": lic["holder_company"],
                 "usage_tier": lic["usage_tier"], "n": len(hits), "latest": hits[0]})
    press_confirm.sort(key=lambda x: x["n"], reverse=True)
    return templates.TemplateResponse(request, "admin/studio.html",
                                      {"projects": projects, "clients": clients,
                                       "statuses": PROJECT_STATUSES, "counts": counts,
                                       "stale_days": STALE_DAYS,
                                       "outstanding": outstanding,
                                       "inquiries": inquiries,
                                       "inquiries_archived": inquiries_archived,
                                       "licenses_expiring": licenses_expiring,
                                       "retainer_drafts": retainer_drafts,
                                       "quota_behind": quota_behind,
                                       "content_due": content_due,
                                       "press_confirm": press_confirm,
                                       "upcoming": upcoming,
                                       "proofing_waiting": list(waiting.values()),
                                       "conflicts": conflicts,
                                       "client_portal_hints": client_portal_hints,
                                       "overdue_by_stage": overdue_by_stage,
                                       "sparklines": sparklines,
                                       "spark_days": day_strs,
                                       "spark_window": spark_days_window,
                                       "stage_value": stage_value,
                                       "pipeline_value_total": pipeline_value_total,
                                       "booked_value": booked_value})


@router.post("/clients")
async def create_client(name: str = Form(...), company: str = Form(""),
                        email: str = Form(""), phone: str = Form("")):
    cid = db.run("INSERT INTO clients (name, company, email, phone) VALUES (?,?,?,?)",
                 (name.strip(), company.strip() or None,
                  email.strip() or None, phone.strip() or None))
    log.info("client %s created", cid)
    return RedirectResponse(f"/admin/studio/clients/{cid}", status_code=303)


@router.post("/inquiries/{inquiry_id}/unconvert")
async def inquiry_unconvert(inquiry_id: int):
    """Clear the conversion stamps on an inquiry so it shows up as actionable
    again. INTENTIONALLY does NOT delete the spawned client/project — by the
    time Kevin clicks undo, those may already carry edits, brand assets, or
    proposals. This is a misclick fix, not a cascade delete."""
    inq = db.one("SELECT id FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    db.run("""UPDATE inquiries SET converted_at=NULL,
              converted_client_id=NULL, converted_project_id=NULL
              WHERE id=?""", (inquiry_id,))
    log.info("inquiry %s unconverted (spawned client/project untouched)",
             inquiry_id)
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/inquiries/{inquiry_id}/dismiss")
async def inquiry_dismiss(inquiry_id: int):
    """Archive an unconverted inquiry — spam, test, or dead leads. Reversible:
    the row is kept and stamped dismissed_at, so it drops out of the active
    leads list and the home 'new inquiries' count but stays in the Inquiries
    table with an undo. Refuses once converted (that anchors a real
    client/project history)."""
    inq = db.one("SELECT id, converted_at FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    if inq["converted_at"]:
        raise HTTPException(status_code=400,
                            detail="converted inquiries cannot be dismissed")
    db.run("UPDATE inquiries SET dismissed_at=datetime('now') WHERE id=?",
           (inquiry_id,))
    log.info("inquiry %s dismissed (archived)", inquiry_id)
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/inquiries/{inquiry_id}/undismiss")
async def inquiry_undismiss(inquiry_id: int):
    """Undo a dismiss — clears dismissed_at so the lead returns to the active
    pipeline."""
    inq = db.one("SELECT id FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    db.run("UPDATE inquiries SET dismissed_at=NULL WHERE id=?", (inquiry_id,))
    log.info("inquiry %s undismissed (restored)", inquiry_id)
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/inquiries/{inquiry_id}/client")
async def inquiry_to_client(inquiry_id: int):
    inq = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    existing = db.one("SELECT id FROM clients WHERE email=?", (inq["email"],))
    cid = existing["id"] if existing else db.run(
        "INSERT INTO clients (name, company, email, notes) VALUES (?,?,?,?)",
        (inq["name"], inq["business"], inq["email"],
         f"From inquiry {inq['created_at'][:10]}:\n{inq['message']}"))
    if not existing:
        log.info("client %s created from inquiry %s", cid, inquiry_id)
    pid = None
    # Bookings carry a date + service → lift straight into an 'inquiry_received' project so
    # Kevin can spawn a proposal without re-typing the date.
    if inq["kind"] == "booking" and inq["shoot_date"]:
        title = f"{inq['service'] or 'Shoot'} — {inq['shoot_date']}"
        pid = db.run("""INSERT INTO projects (client_id, title, shoot_date)
                        VALUES (?,?,?)""", (cid, title, inq["shoot_date"]))
        log.info("project %s spawned from booking %s", pid, inquiry_id)
    # Stamp the inquiry as converted so the studio list can fade it out.
    db.run("""UPDATE inquiries SET converted_at=datetime('now'),
              converted_client_id=?, converted_project_id=? WHERE id=?""",
           (cid, pid, inquiry_id))
    if pid:
        return RedirectResponse(f"/admin/studio/projects/{pid}", status_code=303)
    return RedirectResponse(f"/admin/studio/clients/{cid}", status_code=303)


@router.post("/inquiries/{inquiry_id}/quote")
async def inquiry_to_quote(inquiry_id: int):
    """One click from a lead to an editable draft quote: find/create the client,
    spawn an 'inquiry_received' project, and open a blank draft proposal seeded
    with the inquiry brief as the intro. Quoting-first flow — Kevin fills the
    line items (no auto-pricing; the catalog floor numbers live in proposals
    PRESETS and are applied by hand per client)."""
    inq = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    existing = db.one("SELECT id FROM clients WHERE email=?", (inq["email"],))
    cid = existing["id"] if existing else db.run(
        "INSERT INTO clients (name, company, email, notes) VALUES (?,?,?,?)",
        (inq["name"], inq["business"], inq["email"],
         f"From inquiry {inq['created_at'][:10]}:\n{inq['message']}"))
    if not existing:
        log.info("client %s created from inquiry %s (quote)", cid, inquiry_id)
    title = f"{inq['service'] or 'Shoot'}"
    if inq["shoot_date"]:
        title += f" — {inq['shoot_date']}"
    pid = db.run("INSERT INTO projects (client_id, title, shoot_date) VALUES (?,?,?)",
                 (cid, title, inq["shoot_date"]))
    intro = (f"Quote prepared from inquiry received {inq['created_at'][:10]}.\n\n"
             f"{inq['message']}")
    prop_id = db.run("""INSERT INTO proposals (project_id, slug, title, intro)
                        VALUES (?,?,?,?)""",
                     (pid, security.new_slug(), f"Quote — {title}", intro))
    db.run("""UPDATE inquiries SET converted_at=datetime('now'),
              converted_client_id=?, converted_project_id=? WHERE id=?""",
           (cid, pid, inquiry_id))
    log.info("inquiry %s → project %s + draft proposal %s", inquiry_id, pid, prop_id)
    return RedirectResponse(f"/admin/studio/proposals/{prop_id}", status_code=303)


@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: int):
    c = get_client(client_id)
    projects = db.all_("SELECT * FROM projects WHERE client_id=? ORDER BY created_at DESC",
                       (client_id,))
    portal = db.one("SELECT * FROM portals WHERE client_id=?", (client_id,))
    galleries = db.all_(
        """SELECT g.id, g.title, g.published, g.slug,
                  (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id
                     AND a.status='ready') AS n_assets,
                  (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                     JOIN assets a ON a.id=f.asset_id
                     WHERE a.gallery_id=g.id) AS n_favs
           FROM galleries g WHERE g.client_id=? ORDER BY g.created_at DESC""",
        (client_id,))
    brand = db.all_("SELECT * FROM brand_assets WHERE client_id=? ORDER BY created_at DESC",
                    (client_id,))
    brand_kits = db.all_("SELECT * FROM brand_kits WHERE client_id=? ORDER BY id DESC",
                         (client_id,))
    parent = db.one("SELECT id, name FROM clients WHERE id=?", (c["parent_id"],)) \
        if c["parent_id"] else None
    # Candidate parents: every other client except this one's descendants
    # (picking a descendant would be a cycle — the route rejects it anyway).
    descendants = clients.descendant_ids(client_id)
    blocked = {client_id, *descendants}
    parent_choices = [r for r in db.all_(
        "SELECT id, name FROM clients ORDER BY name") if r["id"] not in blocked]
    # Read-only roster of the venues/regions under this client (group->venue
    # direction; the parent selector above covers venue->group). Top-down order
    # preserved from the descendant_ids helper.
    children = []
    if descendants:
        rows = {r["id"]: r for r in db.all_(
            "SELECT id, name, company FROM clients WHERE id IN (%s)"
            % ",".join("?" * len(descendants)), descendants)}
        children = [rows[i] for i in descendants if i in rows]
    licenses = db.all_(
        """SELECT id, title, usage_tier, exclusivity, status, published, fee_cents,
                  starts_on, ends_on, perpetual,
                  CAST(julianday(ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
           FROM licenses WHERE holder_client_id=? AND deleted_at IS NULL
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                    ends_on IS NULL, ends_on""", (client_id,))
    # Reverse lookup: licenses that reach this client WITHOUT it holding them — a
    # group (holder_and_descendants) grant on an ancestor, or an explicit
    # 'specific' grant elsewhere that lists this client. Local import: licenses
    # imports studio.get_client at module load, so a top-level import here cycles.
    from . import licenses as licenses_mod
    covering = licenses_mod.licenses_covering(client_id)
    # Portal audit aggregates — totals across published galleries the client
    # can actually see + the on-disk crop cache (the social-crop ZIP source).
    totals = {
        "assets": sum(g["n_assets"] for g in galleries),
        "favs":   sum(g["n_favs"]   for g in galleries),
        "galleries_published": sum(1 for g in galleries if g["published"]),
        "brand_bytes": sum(b["bytes"] for b in brand),
    }
    crop_bytes = 0
    for g in galleries:
        crops_dir = config.MEDIA_DIR / str(g["id"]) / "crops"
        if crops_dir.exists():
            crop_bytes += sum(f.stat().st_size for f in crops_dir.rglob("*")
                              if f.is_file())
    totals["crop_bytes"] = crop_bytes
    # Lifetime money rollup — invoiced (issued only, drafts excluded), paid is
    # the ground truth from actual payment events (R21), shoots delivered counts
    # projects that reached delivery.
    inv = db.one(
        """SELECT COALESCE(SUM(CASE WHEN status != 'draft' THEN total_cents END), 0) AS invoiced_cents,
                  COUNT(CASE WHEN status != 'draft' THEN 1 END) AS n_invoices
           FROM invoices
           WHERE project_id IN (SELECT id FROM projects WHERE client_id=?)""",
        (client_id,))
    paid = db.one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS paid_cents
           FROM payments
           WHERE invoice_id IN (
             SELECT i.id FROM invoices i JOIN projects p ON p.id=i.project_id
             WHERE p.client_id=?)""",
        (client_id,))
    n_delivered = db.one(
        """SELECT COUNT(*) AS n FROM projects
           WHERE client_id=? AND status IN ('project_closed','archived')""",
        (client_id,))["n"]
    money = {
        "invoiced_cents": inv["invoiced_cents"],
        "paid_cents": paid["paid_cents"],
        "outstanding_cents": max(inv["invoiced_cents"] - paid["paid_cents"], 0),
        "n_invoices": inv["n_invoices"],
        "n_delivered": n_delivered,
    }
    # Cross-session activity feed — the same per-doc events the project page
    # shows, but spanning every session this client has, newest first. Pure-read
    # narration of state already stored (reuses _build_timeline); no new state,
    # so nothing writes to the Notion Activity Log.
    proj_ids = [p["id"] for p in projects]
    timeline = []
    if proj_ids:
        ph = ",".join("?" * len(proj_ids))
        c_proposals = db.all_("SELECT * FROM proposals WHERE project_id IN (%s)" % ph, proj_ids)
        c_contracts = db.all_("SELECT * FROM contracts WHERE project_id IN (%s)" % ph, proj_ids)
        c_invoices = db.all_("SELECT * FROM invoices WHERE project_id IN (%s)" % ph, proj_ids)
        c_emails = db.all_("SELECT * FROM emails_log WHERE project_id IN (%s)" % ph, proj_ids)
        c_payments = db.all_("SELECT pm.* FROM payments pm JOIN invoices i ON i.id=pm.invoice_id "
                             "WHERE i.project_id IN (%s)" % ph, proj_ids)
        timeline = _build_timeline(c_proposals, c_contracts, c_invoices,
                                   c_payments, c_emails)[:40]
    return templates.TemplateResponse(request, "admin/client.html",
                                      {"c": c, "projects": projects, "portal": portal,
                                       "timeline": timeline,
                                       "galleries": galleries, "brand": brand,
                                       "brand_kits": brand_kits,
                                       "parent": parent, "parent_choices": parent_choices,
                                       "children": children,
                                       "licenses": licenses, "covering": covering,
                                       "totals": totals, "money": money,
                                       "blockers": _client_blockers(client_id),
                                       "markets": pricing.MARKETS,
                                       "base_url": config.BASE_URL})


def _client_blockers(client_id: int) -> list[str]:
    """Reasons NOT to silently delete a client — surface as friendly copy so
    Kevin can choose to force-delete with eyes open."""
    blockers: list[str] = []
    n = lambda sql, *p: db.one(sql, p)["n"]
    n_kids = n("SELECT COUNT(*) AS n FROM clients WHERE parent_id=?", client_id)
    if n_kids:
        blockers.append(f"{n_kids} child client{'s' if n_kids != 1 else ''} "
                        "(reparent or detach first)")
    n_gal = n("SELECT COUNT(*) AS n FROM galleries WHERE client_id=?", client_id)
    if n_gal:
        blockers.append(f"{n_gal} linked galler{'ies' if n_gal != 1 else 'y'}")
    n_proj = n("SELECT COUNT(*) AS n FROM projects WHERE client_id=?", client_id)
    if n_proj:
        blockers.append(f"{n_proj} project{'s' if n_proj != 1 else ''}")
    n_brand = n("SELECT COUNT(*) AS n FROM brand_assets WHERE client_id=?", client_id)
    if n_brand:
        blockers.append(f"{n_brand} brand asset{'s' if n_brand != 1 else ''}")
    n_lic = n("""SELECT COUNT(*) AS n FROM licenses
                 WHERE holder_client_id=? AND deleted_at IS NULL""", client_id)
    if n_lic:
        blockers.append(f"{n_lic} license{'s' if n_lic != 1 else ''}")
    portal = db.one("SELECT visits FROM portals WHERE client_id=?", (client_id,))
    if portal and portal["visits"]:
        blockers.append(f"portal with {portal['visits']} visit"
                        f"{'s' if portal['visits'] != 1 else ''}")
    n_fav = n("""SELECT COUNT(*) AS n FROM favorites f
                 JOIN assets a ON a.id=f.asset_id
                 JOIN galleries g ON g.id=a.gallery_id
                 WHERE g.client_id=?""", client_id)
    if n_fav:
        blockers.append(f"{n_fav} favorite{'s' if n_fav != 1 else ''} across their galleries")
    return blockers


@router.post("/clients/{client_id}/delete")
async def delete_client(client_id: int, force: bool = Form(False)):
    c = get_client(client_id)
    # Children are a HARD blocker: force cannot bypass it, and the DB's
    # ON DELETE RESTRICT would reject the delete anyway. Tree restructuring
    # happens only through the set-parent control, never as a delete side-effect.
    n_kids = db.one("SELECT COUNT(*) AS n FROM clients WHERE parent_id=?",
                    (client_id,))["n"]
    if n_kids:
        raise HTTPException(
            status_code=400,
            detail=f"client still has {n_kids} child client"
                   f"{'s' if n_kids != 1 else ''}; reparent or detach "
                   "them first (force cannot override this).")
    blockers = _client_blockers(client_id)
    if blockers and not force:
        raise HTTPException(
            status_code=400,
            detail="client still has " + ", ".join(blockers) +
                   ". Re-submit with force=1 to delete anyway.")
    # galleries.client_id has no ON DELETE clause (defaults to NO ACTION on
    # SQLite), so explicitly unlink before deleting the client. Galleries
    # survive as unowned; brand asset files on disk need explicit rmtree
    # (only the DB rows cascade through the FK).
    db.run("UPDATE galleries SET client_id=NULL WHERE client_id=?", (client_id,))
    brand_dir = config.BRAND_DIR / str(client_id)
    if brand_dir.exists():
        shutil.rmtree(brand_dir, ignore_errors=True)
    db.run("DELETE FROM clients WHERE id=?", (client_id,))
    log.info("client %s deleted (force=%s, blockers=%d)",
             client_id, force, len(blockers))
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/clients/{client_id}")
async def update_client(client_id: int, name: str = Form(...), company: str = Form(""),
                        email: str = Form(""), phone: str = Form(""),
                        notes: str = Form(""), usage_rights: str = Form(""),
                        market: str = Form(pricing.DEFAULT_MARKET)):
    get_client(client_id)
    # The market drives which base rate card the license-fee suggestion reads;
    # reject anything outside the live vocabulary rather than store a value that
    # would silently fall back to Asheville at suggest time.
    if market not in pricing.MARKETS:
        raise HTTPException(status_code=400, detail=f"unknown market {market!r}")
    db.run("""UPDATE clients SET name=?, company=?, email=?, phone=?, notes=?,
              usage_rights=?, market=? WHERE id=?""",
           (name.strip(), company.strip() or None, email.strip() or None,
            phone.strip() or None, notes.strip() or None,
            usage_rights.strip() or None, market, client_id))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/parent")
async def set_parent(client_id: int, parent_id: str = Form("")):
    """Set (or clear) a client's parent. Two cycle guards: A->A is rejected
    here (and by the DB CHECK as a backstop); A->B->A is rejected by checking
    the proposed parent against this client's descendants before the UPDATE."""
    get_client(client_id)
    pid = parent_id.strip()
    if not pid:
        db.run("UPDATE clients SET parent_id=NULL WHERE id=?", (client_id,))
        return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)
    new_parent = int(pid)
    if new_parent == client_id:
        raise HTTPException(status_code=422, detail="a client cannot be its own parent")
    if not db.one("SELECT id FROM clients WHERE id=?", (new_parent,)):
        raise HTTPException(status_code=404, detail="parent client not found")
    if new_parent in clients.descendant_ids(client_id):
        raise HTTPException(
            status_code=422,
            detail="that client is below this one in the tree — "
                   "setting it as parent would create a cycle")
    db.run("UPDATE clients SET parent_id=? WHERE id=?", (new_parent, client_id))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Portal (Phase 2) ───────────────────────────────────────────────────────

def _backfill_crops(client_id: int) -> int:
    """Queue social crops for every favorited, ready photo in the client's galleries
    (handler is idempotent, so re-queuing existing crops is a cheap no-op)."""
    rows = db.all_("""SELECT DISTINCT f.asset_id FROM favorites f
                      JOIN assets a ON a.id=f.asset_id
                      JOIN galleries g ON g.id=a.gallery_id
                      WHERE g.client_id=? AND a.kind='photo' AND a.status='ready'""",
                   (client_id,))
    for r in rows:
        jobs.enqueue("social_crops", {"asset_id": r["asset_id"]})
    return len(rows)


@router.post("/clients/{client_id}/portal")
async def create_portal(client_id: int):
    get_client(client_id)
    if db.one("SELECT id FROM portals WHERE client_id=?", (client_id,)):
        raise HTTPException(status_code=400, detail="portal already exists")
    db.run("INSERT INTO portals (client_id, slug, pin) VALUES (?,?,?)",
           (client_id, security.new_slug(), security.new_pin()))
    n = _backfill_crops(client_id)
    log.info("portal created for client %s (%d crop jobs queued)", client_id, n)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/portal/publish")
async def toggle_portal(client_id: int, published: bool = Form(False)):
    p = db.one("SELECT id FROM portals WHERE client_id=?", (client_id,))
    if not p:
        raise HTTPException(status_code=404)
    db.run("UPDATE portals SET published=? WHERE id=?", (1 if published else 0, p["id"]))
    if published:
        _backfill_crops(client_id)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Brand assets (Phase 2) ─────────────────────────────────────────────────

@router.post("/clients/{client_id}/brand")
async def upload_brand(client_id: int, files: list[UploadFile]):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rejected = []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
        ext = Path(name).suffix.lower()
        if ext not in BRAND_EXTS:
            rejected.append(name)
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        size = 0
        with (dest_dir / stored).open("wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
                size += len(chunk)
        db.run("INSERT INTO brand_assets (client_id, filename, stored, bytes) "
               "VALUES (?,?,?,?)", (client_id, name, stored, size))
    if rejected:
        log.info("client %s brand upload: rejected %s", client_id, rejected)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/brand/{ba_id}")
async def admin_brand_file(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if not b:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / b["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=b["filename"])


@router.post("/clients/{client_id}/brand/{ba_id}/delete")
async def delete_brand(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if b:
        (config.BRAND_DIR / str(client_id) / b["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_assets WHERE id=?", (ba_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Brand kits (Slice 3 — composite overlay) ───────────────────────────────
# A brand_kit is a single raster logo + placement params composited onto social
# crops at render time. Distinct from brand_assets (the general file locker).
# Newest active kit wins (see app/brand_kits.overlay_for_client).

@router.post("/clients/{client_id}/kits")
async def upload_kit(client_id: int, logo: UploadFile,
                     label: str = Form(""),
                     position: str = Form("br"),
                     opacity: int = Form(100),
                     scale_pct: int = Form(22),
                     margin_pct: int = Form(4)):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    name = _SAFE_NAME.sub("_", Path(logo.filename or "logo").name)
    ext = Path(name).suffix.lower()
    if ext not in KIT_EXTS:
        raise HTTPException(status_code=415, detail=f"brand-kit logo must be PNG/WebP/JPEG, not {ext}")
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"kit_{uuid.uuid4().hex}{ext}"
    size = 0
    with (dest_dir / stored).open("wb") as out:
        while chunk := await logo.read(1 << 20):
            out.write(chunk)
            size += len(chunk)
    db.run("INSERT INTO brand_kits (client_id, label, stored, bytes, position, "
           "opacity, scale_pct, margin_pct) VALUES (?,?,?,?,?,?,?,?)",
           (client_id, label.strip() or None, stored, size, position,
            max(0, min(100, opacity)), max(1, min(100, scale_pct)),
            max(0, min(50, margin_pct))))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/kits/{kit_id}")
async def update_kit(client_id: int, kit_id: int,
                     label: str = Form(""),
                     position: str = Form("br"),
                     opacity: int = Form(100),
                     scale_pct: int = Form(22),
                     margin_pct: int = Form(4),
                     active: int = Form(0)):
    k = db.one("SELECT id FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    db.run("UPDATE brand_kits SET label=?, position=?, opacity=?, scale_pct=?, "
           "margin_pct=?, active=? WHERE id=?",
           (label.strip() or None, position, max(0, min(100, opacity)),
            max(1, min(100, scale_pct)), max(0, min(50, margin_pct)),
            1 if active else 0, kit_id))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/kits/{kit_id}/logo")
async def admin_kit_logo(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / k["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.post("/clients/{client_id}/kits/{kit_id}/delete")
async def delete_kit(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if k:
        (config.BRAND_DIR / str(client_id) / k["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_kits WHERE id=?", (kit_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/projects")
async def create_project(client_id: int, title: str = Form(...)):
    get_client(client_id)
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)",
                 (client_id, title.strip()))
    log.info("project %s created for client %s", pid, client_id)
    return RedirectResponse(f"/admin/studio/projects/{pid}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: int):
    p = get_project(project_id)
    proposals = db.all_("SELECT * FROM proposals WHERE project_id=? ORDER BY created_at DESC",
                        (project_id,))
    contracts = db.all_("SELECT * FROM contracts WHERE project_id=? ORDER BY created_at DESC",
                        (project_id,))
    invoices = db.all_("SELECT * FROM invoices WHERE project_id=? ORDER BY created_at DESC",
                       (project_id,))
    emails = db.all_("SELECT * FROM emails_log WHERE project_id=? ORDER BY created_at DESC",
                     (project_id,))
    galleries = db.all_("SELECT id, title FROM galleries ORDER BY created_at DESC")
    plans = db.all_("SELECT id, title, total_cents, anchor_day, active, last_run_period "
                    "FROM recurring_plans WHERE project_id=? AND deleted_at IS NULL "
                    "ORDER BY created_at DESC", (project_id,))
    # Domain F shot list (inline query, not a shotlist import — keeps studio.py
    # free of the shotlist->studio dependency direction).
    shots = db.all_("SELECT * FROM shot_list WHERE project_id=? AND deleted_at IS NULL "
                    "ORDER BY sort_order, id", (project_id,))
    payments = db.all_("""SELECT pm.* FROM payments pm
                          JOIN invoices i ON i.id=pm.invoice_id
                          WHERE i.project_id=? ORDER BY pm.created_at DESC""",
                       (project_id,))
    timeline = _build_timeline(proposals, contracts, invoices, payments, emails)
    return templates.TemplateResponse(request, "admin/project.html",
                                      {"p": p, "proposals": proposals,
                                       "contracts": contracts, "invoices": invoices,
                                       "emails": emails, "galleries": galleries,
                                       "plans": plans, "shots": shots,
                                       "timeline": timeline,
                                       "shot_categories": usage_vocab.SHOT_CATEGORIES,
                                       "shot_priorities": usage_vocab.SHOT_PRIORITIES,
                                       "statuses": PROJECT_STATUSES,
                                       "base_url": config.BASE_URL})


def _build_timeline(proposals, contracts, invoices, payments, emails):
    """Aggregate doc-status timestamps + payments + email sends into one
    reverse-chronological feed. Read-only narration of state already stored on
    the rows — no new state, no automation."""
    ev = []

    def add(ts, kind, text):
        if ts:
            ev.append({"ts": ts, "kind": kind, "text": text})

    for d in proposals:
        add(d["created_at"], "proposal", f"Proposal “{d['title']}” drafted")
        add(d["sent_at"], "proposal", f"Proposal “{d['title']}” sent")
        add(d["viewed_at"], "proposal", f"Proposal “{d['title']}” viewed by client")
        add(d["accepted_at"], "proposal", f"Proposal “{d['title']}” accepted")
    for d in contracts:
        add(d["created_at"], "contract", f"Contract “{d['title']}” drafted")
        add(d["sent_at"], "contract", f"Contract “{d['title']}” sent")
        add(d["viewed_at"], "contract", f"Contract “{d['title']}” viewed by client")
        add(d["signed_at"], "contract",
            f"Contract “{d['title']}” signed by {d['signer_name'] or 'client'}")
    for d in invoices:
        add(d["created_at"], "invoice", f"Invoice “{d['title']}” drafted")
        add(d["sent_at"], "invoice", f"Invoice “{d['title']}” sent")
        add(d["viewed_at"], "invoice", f"Invoice “{d['title']}” viewed by client")
        add(d["paid_at"], "invoice", f"Invoice “{d['title']}” paid in full")
    for d in payments:
        add(d["created_at"], "payment",
            f"Payment received · ${d['amount_cents'] / 100:.2f} ({d['kind']})")
    for d in emails:
        add(d["created_at"], "email",
            f"Email sent · {d['doc_kind']} “{d['subject']}” to {d['to_email']}")

    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev


@router.post("/projects/{project_id}/workspace/publish")
async def publish_workspace(project_id: int):
    p = get_project(project_id)
    slug = p["workspace_slug"] or security.new_slug()
    pin = p["workspace_pin"] or security.new_pin()
    db.run("""UPDATE projects SET workspace_slug=?, workspace_pin=?,
              workspace_published=1 WHERE id=?""", (slug, pin, project_id))
    log.info("workspace published for project %s", project_id)
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/workspace/unpublish")
async def unpublish_workspace(project_id: int):
    get_project(project_id)
    # Keep the slug/PIN so re-publishing reuses the same link; just close it.
    db.run("UPDATE projects SET workspace_published=0 WHERE id=?", (project_id,))
    log.info("workspace unpublished for project %s", project_id)
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


# ── Testimonials ──────────────────────────────────────────────────────────

@router.get("/testimonials", response_class=HTMLResponse)
async def testimonials_list(request: Request):
    rows = db.all_("""SELECT t.*, g.title AS gallery_title, g.slug AS gallery_slug
                      FROM testimonials t
                      LEFT JOIN galleries g ON g.id=t.gallery_id
                      ORDER BY t.position, t.id DESC""")
    galleries = db.all_("SELECT id, title FROM galleries ORDER BY created_at DESC")
    return templates.TemplateResponse(request, "admin/testimonials.html",
                                      {"testimonials": rows, "galleries": galleries,
                                       "base_url": config.BASE_URL})


@router.post("/testimonials")
async def create_testimonial(quote: str = Form(...),
                             attribution_name: str = Form(...),
                             business: str = Form(""),
                             gallery_id: int | None = Form(None),
                             position: int = Form(0),
                             published: bool = Form(False)):
    if not (quote.strip() and attribution_name.strip()):
        raise HTTPException(status_code=400, detail="quote and name required")
    tid = db.run("""INSERT INTO testimonials (quote, attribution_name, business,
                                              gallery_id, position, published)
                    VALUES (?,?,?,?,?,?)""",
                 (quote.strip(), attribution_name.strip(),
                  business.strip() or None, gallery_id, position,
                  1 if published else 0))
    log.info("testimonial %s created", tid)
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/testimonials/{tid}")
async def update_testimonial(tid: int, quote: str = Form(...),
                             attribution_name: str = Form(...),
                             business: str = Form(""),
                             gallery_id: int | None = Form(None),
                             position: int = Form(0),
                             published: bool = Form(False)):
    if not db.one("SELECT id FROM testimonials WHERE id=?", (tid,)):
        raise HTTPException(status_code=404)
    db.run("""UPDATE testimonials SET quote=?, attribution_name=?, business=?,
              gallery_id=?, position=?, published=? WHERE id=?""",
           (quote.strip(), attribution_name.strip(), business.strip() or None,
            gallery_id, position, 1 if published else 0, tid))
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/testimonials/{tid}/delete")
async def delete_testimonial(tid: int):
    db.run("DELETE FROM testimonials WHERE id=?", (tid,))
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/projects/{project_id}")
async def update_project(project_id: int, title: str = Form(...),
                         status: str = Form(...), notes: str = Form(""),
                         gallery_id: int | None = Form(None),
                         notion_page_id: str = Form(""),
                         shoot_date: str = Form("")):
    get_project(project_id)
    if status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    db.run("""UPDATE projects SET title=?, status=?, notes=?, gallery_id=?,
              notion_page_id=?, shoot_date=?,
              stage_changed_at=CASE WHEN status=? THEN stage_changed_at
                                    ELSE datetime('now') END
              WHERE id=?""",
           (title.strip(), status, notes.strip() or None, gallery_id,
            notion_page_id.strip() or None, shoot_date.strip() or None,
            status, project_id))
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/status")
async def move_project_status(project_id: int, status: str = Form(...)):
    """Kanban quick-move: change only a project's pipeline stage. The full project
    form (update_project) still owns title/notes/gallery edits; this is the
    board's drag-to-column equivalent — one field, one write, back to the board."""
    get_project(project_id)
    if status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    db.run("""UPDATE projects SET status=?,
              stage_changed_at=CASE WHEN status=? THEN stage_changed_at
                                    ELSE datetime('now') END
              WHERE id=?""", (status, status, project_id))
    log.info("project %s moved to status %s", project_id, status)
    return RedirectResponse("/admin/studio#projects", status_code=303)
