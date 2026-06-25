"""Recurring retainer plans — the monthly Brand Partner content clients.

A plan is a template that GENERATES draft invoices; it never sends or charges.
Kevin still clicks Send on each generated draft and Stripe still collects — the
manual-send doctrine is untouched. Drafts are made two ways, sharing one core
(generate_for_plan): the explicit "Generate this period's draft" button, and the
slice-2 in-process scheduler (see app/scheduler.py) that sweeps due plans on
their anchor_day. last_run_period ('YYYY-MM') is the per-month claim — it caps a
period to one draft, so a double-click OR an overlapping sweep can't duplicate.
"""

import datetime as dt
import json
import logging
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, caption_ai, config, db, security
from ..render import templates
from .proposals import MAX_ITEM_ROWS, parse_items
from .studio import get_project

log = logging.getLogger("mise.admin.recurring")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

MAX_QUOTA_ROWS = 8
CALENDAR_STATUSES = ("planned", "shot", "delivered")
CAPTION_STATUSES = ("draft", "approved")


def get_plan(plan_id: int) -> "db.sqlite3.Row":
    return db.get_or_404(
        "SELECT * FROM recurring_plans WHERE id=? AND deleted_at IS NULL", (plan_id,)
    )


def _period(today: dt.date | None = None) -> str:
    """Current billing period as 'YYYY-MM' — the dedupe key for manual generate."""
    return (today or dt.date.today()).strftime("%Y-%m")


def parse_quota(form) -> str:
    """Collect quota_label_N / quota_target_N rows → JSON of {label, target}.

    The monthly deliverable commitment, separate from the invoice line items —
    advisory only, it never affects billing. Blank-label rows are dropped; a
    target below zero is clamped to zero (a 0-target line is a placeholder, not
    an error)."""
    quota = []
    for i in range(MAX_QUOTA_ROWS):
        label = (form.get(f"quota_label_{i}") or "").strip()
        if not label:
            continue
        try:
            target = max(0, int(form.get(f"quota_target_{i}") or "0"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad quota target on row {i + 1}")
        quota.append({"label": label, "target": target})
    return json.dumps(quota)


@router.post("/projects/{project_id}/recurring")
async def create_plan(project_id: int, title: str = Form(...)):
    get_project(project_id)
    if not title.strip():
        raise HTTPException(status_code=400, detail="title required")
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO recurring_plans (project_id, title) VALUES (?,?)",
            (project_id, title.strip()),
        )
        pid = cur.lastrowid
        audit.log(
            con,
            "recurring_plan",
            pid,
            "create",
            diff={"project_id": project_id, "title": title.strip()},
        )
    log.info("recurring plan %s created for project %s", pid, project_id)
    return RedirectResponse(f"/admin/studio/recurring/{pid}", status_code=303)


@router.get("/recurring/{plan_id}", response_class=HTMLResponse)
async def plan_detail(request: Request, plan_id: int):
    d = get_plan(plan_id)
    p = get_project(d["project_id"])
    items = json.loads(d["line_items"])
    rows = items + [{} for _ in range(max(0, MAX_ITEM_ROWS - len(items)))]
    quota = json.loads(d["quota"])
    quota_rows = quota + [{} for _ in range(max(0, MAX_QUOTA_ROWS - len(quota)))]
    period = _period()
    deliveries = db.all_(
        "SELECT id, label, qty, note, created_at FROM retainer_deliveries "
        "WHERE plan_id=? AND period=? ORDER BY created_at",
        (plan_id, period),
    )
    # Sum what's been logged this period per label, then line it up against each
    # quota target. Deliveries whose label doesn't match a quota line still count
    # in the log but show as "extra" (un-targeted) — the quota is a plan, not a cap.
    delivered = {}
    for r in deliveries:
        delivered[r["label"]] = delivered.get(r["label"], 0) + r["qty"]
    progress = [
        {"label": q["label"], "target": q["target"], "done": delivered.get(q["label"], 0)}
        for q in quota
    ]
    extra = [
        {"label": lbl, "done": n}
        for lbl, n in delivered.items()
        if lbl not in {q["label"] for q in quota}
    ]
    # Content calendar: this period's planned slots, soonest first. Forward-looking
    # planning layer — decoupled from the delivery log above (marking a slot
    # 'delivered' never auto-credits the quota count; that stays a manual log).
    calendar = db.all_(
        "SELECT id, slot_date, label, title, status, note FROM content_calendar "
        "WHERE plan_id=? AND substr(slot_date,1,7)=? ORDER BY slot_date, id",
        (plan_id, period),
    )
    # Caption packs: this period's caption deliverables (manual text in 6a). Like
    # the calendar, DECOUPLED from the delivery log — marking a caption 'approved'
    # never auto-credits the quota count; it only assisted-credit pre-fills the log.
    captions = db.all_(
        "SELECT id, slot_id, period, label, body, status, note, "
        "ai_drafted, ai_model, ai_drafted_at, ai_draft_original FROM retainer_captions "
        "WHERE plan_id=? AND period=? ORDER BY created_at, id",
        (plan_id, period),
    )
    invoices = db.all_(
        "SELECT id, title, total_cents, status, created_at FROM invoices "
        "WHERE recurring_plan_id=? ORDER BY created_at DESC",
        (plan_id,),
    )
    # Assisted-credit pre-fill: when a calendar slot was just flipped to
    # 'delivered', set_calendar_status redirects here with credit_* query params.
    # They seed the delivery-log form's defaults so the human commits in one click
    # — they are display defaults ONLY, never a write (the manual log stays the
    # count's single source of truth; the slice-3 decoupling guarantee holds).
    credit = {
        "label": request.query_params.get("credit_label", ""),
        "qty": request.query_params.get("credit_qty", ""),
        "period": request.query_params.get("credit_period", ""),
    }
    # A failed/blocked "Draft with AI" redirects here with a message to surface
    # (mesh failure, not configured, or no-clobber refusal) — nothing was written.
    caption_error = request.query_params.get("caption_error", "")
    return templates.TemplateResponse(
        request,
        "admin/recurring.html",
        {
            "d": d,
            "p": p,
            "rows": rows,
            "quota_rows": quota_rows,
            "progress": progress,
            "extra": extra,
            "deliveries": deliveries,
            "calendar": calendar,
            "calendar_statuses": CALENDAR_STATUSES,
            "captions": captions,
            "caption_statuses": CAPTION_STATUSES,
            "caption_error": caption_error,
            "ai_caption_enabled": caption_ai.is_enabled(),
            "quota_labels": [q["label"] for q in quota],
            "invoices": invoices,
            "period": period,
            "credit": credit,
            "base_url": config.BASE_URL,
        },
    )


@router.post("/recurring/{plan_id}")
async def update_plan(request: Request, plan_id: int):
    d = get_plan(plan_id)
    form = await request.form()
    items_json, total = parse_items(form)
    try:
        anchor = int(form.get("anchor_day") or "1")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad anchor day")
    if not 1 <= anchor <= 28:
        raise HTTPException(status_code=400, detail="anchor day must be 1–28")
    title = (form.get("title") or "").strip() or d["title"]
    active = 1 if form.get("active") else 0
    notes = (form.get("notes") or "").strip() or None
    quota_json = parse_quota(form)
    with db.tx() as con:
        con.execute(
            "UPDATE recurring_plans SET title=?, line_items=?, total_cents=?, "
            "anchor_day=?, active=?, notes=?, quota=? WHERE id=?",
            (title, items_json, total, anchor, active, notes, quota_json, plan_id),
        )
        audit.log(
            con,
            "recurring_plan",
            plan_id,
            "update",
            diff={"total_cents": total, "anchor_day": anchor, "active": active},
        )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}", status_code=303)


@router.post("/recurring/{plan_id}/deliveries")
async def log_delivery(
    plan_id: int,
    label: str = Form(...),
    qty: int = Form(...),
    period: str = Form(""),
    note: str = Form(""),
):
    """Manually log a deliverable provided this (or another) period. Advisory
    tracking only — never touches invoices/billing, never auto-credited from
    galleries (the count is Kevin's, by doctrine)."""
    d = get_plan(plan_id)  # noqa: F841  # noqa: F841
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be above zero")
    period = period.strip() or _period()
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO retainer_deliveries (plan_id, period, label, qty, note) "
            "VALUES (?,?,?,?,?)",
            (plan_id, period, label, qty, note.strip() or None),
        )
        audit.log(
            con,
            "recurring_plan",
            plan_id,
            "delivery_logged",
            diff={"period": period, "label": label, "qty": qty},
        )
    log.info(
        "retainer plan %s logged delivery %s (%s ×%d) for %s",
        plan_id,
        cur.lastrowid,
        label,
        qty,
        period,
    )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}", status_code=303)


@router.post("/recurring/{plan_id}/deliveries/{delivery_id}/delete")
async def delete_delivery(plan_id: int, delivery_id: int):
    get_plan(plan_id)
    with db.tx() as con:
        # Scope the delete to this plan so a wrong id can't reach another plan's log.
        deleted = con.execute(
            "DELETE FROM retainer_deliveries WHERE id=? AND plan_id=?", (delivery_id, plan_id)
        ).rowcount
        if deleted:
            audit.log(
                con,
                "recurring_plan",
                plan_id,
                "delivery_deleted",
                diff={"delivery_id": delivery_id},
            )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}", status_code=303)


@router.post("/recurring/{plan_id}/calendar")
async def add_calendar_slot(
    plan_id: int,
    slot_date: str = Form(...),
    label: str = Form(...),
    title: str = Form(""),
    note: str = Form(""),
):
    """Schedule a planned content slot on a date. Planning only — no billing
    effect, never auto-credits the quota delivery count."""
    get_plan(plan_id)
    slot_date = slot_date.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", slot_date):
        raise HTTPException(status_code=400, detail="slot date must be YYYY-MM-DD")
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO content_calendar (plan_id, slot_date, label, title, note) "
            "VALUES (?,?,?,?,?)",
            (plan_id, slot_date, label, title.strip() or None, note.strip() or None),
        )
        audit.log(
            con,
            "recurring_plan",
            plan_id,
            "calendar_slot_added",
            diff={"slot_date": slot_date, "label": label},
        )
    log.info(
        "retainer plan %s added calendar slot %s (%s on %s)",
        plan_id,
        cur.lastrowid,
        label,
        slot_date,
    )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}", status_code=303)


@router.post("/recurring/{plan_id}/calendar/{slot_id}/status")
async def set_calendar_status(plan_id: int, slot_id: int, status: str = Form(...)):
    """Advance a slot planned -> shot -> delivered. Scoped to the plan so a wrong
    id can't reach another plan's calendar.

    Assisted credit (NOT auto-credit): on a planned/shot -> delivered transition we
    redirect with credit_* query params that pre-fill the manual delivery-log form
    (label, qty=1, period from the slot date). No retainer_deliveries row is written
    here — the human still clicks Log delivery. The slice-3 decoupling guarantee
    (marking a slot delivered never writes a delivery) is fully preserved; this only
    deletes the re-typing and the forget-to-log failure mode."""
    get_plan(plan_id)
    if status not in CALENDAR_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    prefill = None
    with db.tx() as con:
        prior = con.execute(
            "SELECT status, label, slot_date FROM content_calendar WHERE id=? AND plan_id=?",
            (slot_id, plan_id),
        ).fetchone()
        changed = con.execute(
            "UPDATE content_calendar SET status=? WHERE id=? AND plan_id=?",
            (status, slot_id, plan_id),
        ).rowcount
        if changed:
            audit.log(
                con,
                "recurring_plan",
                plan_id,
                "calendar_slot_status",
                diff={"slot_id": slot_id, "status": status},
            )
            # Fire only on the transition INTO delivered, not on every render / re-save.
            if status == "delivered" and prior and prior["status"] != "delivered":
                prefill = {
                    "credit_label": prior["label"],
                    "credit_qty": 1,
                    "credit_period": prior["slot_date"][:7],
                }
    url = f"/admin/studio/recurring/{plan_id}"
    if prefill:
        url += "?" + urlencode(prefill) + "#log-delivery"
    return RedirectResponse(url, status_code=303)


@router.post("/recurring/{plan_id}/calendar/{slot_id}/delete")
async def delete_calendar_slot(plan_id: int, slot_id: int):
    get_plan(plan_id)
    with db.tx() as con:
        deleted = con.execute(
            "DELETE FROM content_calendar WHERE id=? AND plan_id=?", (slot_id, plan_id)
        ).rowcount
        if deleted:
            audit.log(
                con, "recurring_plan", plan_id, "calendar_slot_deleted", diff={"slot_id": slot_id}
            )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}", status_code=303)


def _caption_slot_id(plan_id: int, raw: str) -> int | None:
    """Validate an optional calendar-slot reference: it must be a slot on THIS
    plan, or we drop it to NULL. Keeps a wrong/foreign id from linking across
    plans; a blank value means a standalone caption (no slot)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        sid = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad slot reference")
    owned = db.one("SELECT id FROM content_calendar WHERE id=? AND plan_id=?", (sid, plan_id))
    return sid if owned else None


@router.post("/recurring/{plan_id}/captions")
async def add_caption(
    plan_id: int,
    label: str = Form(...),
    body: str = Form(...),
    period: str = Form(""),
    slot_id: str = Form(""),
    note: str = Form(""),
):
    """Create a caption deliverable (manual text in 6a — no AI). Decoupled from the
    delivery log: this writes only the caption, never a retainer_deliveries row."""
    get_plan(plan_id)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="caption text required")
    period = period.strip() or _period()
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    sid = _caption_slot_id(plan_id, slot_id)
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO retainer_captions (plan_id, slot_id, period, label, body, note) "
            "VALUES (?,?,?,?,?,?)",
            (plan_id, sid, period, label, body, note.strip() or None),
        )
        audit.log(
            con,
            "recurring_plan",
            plan_id,
            "caption_added",
            diff={"caption_id": cur.lastrowid, "period": period, "label": label},
        )
    log.info("retainer plan %s added caption %s (%s, %s)", plan_id, cur.lastrowid, label, period)
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}#captions", status_code=303)


@router.post("/recurring/{plan_id}/captions/{caption_id}")
async def update_caption(
    plan_id: int,
    caption_id: int,
    label: str = Form(...),
    body: str = Form(...),
    note: str = Form(""),
):
    """Edit a caption's text/label inline. Plan-scoped; writes no delivery row."""
    get_plan(plan_id)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="caption text required")
    with db.tx() as con:
        changed = con.execute(
            "UPDATE retainer_captions SET label=?, body=?, note=? WHERE id=? AND plan_id=?",
            (label, body, note.strip() or None, caption_id, plan_id),
        ).rowcount
        if changed:
            audit.log(
                con,
                "recurring_plan",
                plan_id,
                "caption_updated",
                diff={"caption_id": caption_id, "label": label},
            )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}#captions", status_code=303)


@router.post("/recurring/{plan_id}/captions/{caption_id}/status")
async def set_caption_status(plan_id: int, caption_id: int, status: str = Form(...)):
    """Advance a caption draft -> approved. Plan-scoped.

    Assisted credit (NOT auto-credit): on a draft -> approved transition we redirect
    with credit_* query params that pre-fill the manual delivery-log form (label,
    qty=1, period from the caption). No retainer_deliveries row is written here — the
    human still clicks Log delivery. Same decoupling guarantee as the calendar slot."""
    get_plan(plan_id)
    if status not in CAPTION_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    prefill = None
    with db.tx() as con:
        prior = con.execute(
            "SELECT status, label, period FROM retainer_captions WHERE id=? AND plan_id=?",
            (caption_id, plan_id),
        ).fetchone()
        changed = con.execute(
            "UPDATE retainer_captions SET status=? WHERE id=? AND plan_id=?",
            (status, caption_id, plan_id),
        ).rowcount
        if changed:
            audit.log(
                con,
                "recurring_plan",
                plan_id,
                "caption_status",
                diff={"caption_id": caption_id, "status": status},
            )
            # Fire only on the transition INTO approved, not on every re-save.
            if status == "approved" and prior and prior["status"] != "approved":
                prefill = {
                    "credit_label": prior["label"],
                    "credit_qty": 1,
                    "credit_period": prior["period"],
                }
    url = f"/admin/studio/recurring/{plan_id}"
    if prefill:
        url += "?" + urlencode(prefill) + "#log-delivery"
    else:
        url += "#captions"
    return RedirectResponse(url, status_code=303)


@router.post("/recurring/{plan_id}/captions/{caption_id}/delete")
async def delete_caption(plan_id: int, caption_id: int):
    get_plan(plan_id)
    with db.tx() as con:
        deleted = con.execute(
            "DELETE FROM retainer_captions WHERE id=? AND plan_id=?", (caption_id, plan_id)
        ).rowcount
        if deleted:
            audit.log(
                con, "recurring_plan", plan_id, "caption_deleted", diff={"caption_id": caption_id}
            )
    return RedirectResponse(f"/admin/studio/recurring/{plan_id}#captions", status_code=303)


def _is_human_body(cap: "db.sqlite3.Row") -> bool:
    """True when `body` holds words a human typed/edited (vs empty, or an untouched
    prior AI draft). The no-clobber guard: a human's words are never destroyed by a
    regenerate click — overwriting them requires an explicit replace."""
    body = (cap["body"] or "").strip()
    if not body:
        return False
    untouched_ai = cap["ai_drafted"] and body == (cap["ai_draft_original"] or "").strip()
    return not untouched_ai


@router.post("/recurring/{plan_id}/captions/{caption_id}/draft")
async def draft_caption(plan_id: int, caption_id: int, replace: str = Form("")):
    """Draft this caption with AI — an EXPLICIT human action (never on page load).

    The draft lands in the editable `body` as a SUGGESTION: status stays 'draft' and
    NO retainer_deliveries row is written — generation is fully severed from
    delivered-status and from the count. Provenance is recorded and the verbatim AI
    output is preserved in ai_draft_original so the (draft -> human-edited) diff is
    recoverable. No-clobber: if `body` holds human words, refuse unless replace=1.
    The mesh call is expected to fail sometimes; on any failure we write nothing and
    surface the message. Odysseus owns model selection — Mise only passes context."""
    d = get_plan(plan_id)
    cap = db.one("SELECT * FROM retainer_captions WHERE id=? AND plan_id=?", (caption_id, plan_id))
    if not cap:
        raise HTTPException(status_code=404)

    def _back(error: str = "") -> RedirectResponse:
        url = f"/admin/studio/recurring/{plan_id}"
        url += ("?" + urlencode({"caption_error": error}) if error else "") + "#captions"
        return RedirectResponse(url, status_code=303)

    if _is_human_body(cap) and not replace:
        return _back("This caption has your edits — use Replace to overwrite with an AI draft.")

    proj = get_project(d["project_id"])
    client = db.one("SELECT name, company FROM clients WHERE id=?", (proj["client_id"],))
    ctx = {
        "label": cap["label"],
        "note": cap["note"] or "",
        "client": client["company"] or client["name"] if client else "",
        "period": cap["period"],
        "plan_title": d["title"],
    }
    try:
        result = caption_ai.draft_caption(ctx)
    except caption_ai.CaptionDraftError as e:
        log.warning("caption %s draft failed: %s", caption_id, e)
        return _back(str(e))

    with db.tx() as con:
        con.execute(
            "UPDATE retainer_captions SET body=?, ai_drafted=1, ai_model=?, "
            "ai_drafted_at=datetime('now'), ai_draft_original=? WHERE id=? AND plan_id=?",
            (result["caption"], result["model"], result["caption"], caption_id, plan_id),
        )
        # Status is deliberately NOT touched — an AI draft is a suggestion, not a
        # delivery. Crediting stays the slice-4/6a human approve -> /deliveries path.
        audit.log(
            con,
            "recurring_plan",
            plan_id,
            "caption_ai_drafted",
            diff={"caption_id": caption_id, "model": result["model"]},
        )
    log.info(
        "retainer plan %s caption %s AI-drafted (model=%s)", plan_id, caption_id, result["model"]
    )
    return _back()


def generate_for_plan(plan: "db.sqlite3.Row", period: str) -> int | None:
    """Insert a DRAFT invoice for `plan` for `period` and claim the period.

    Shared by the manual Generate button and the scheduler sweep — drafts only,
    never sends or charges. The period claim is atomic (UPDATE ... WHERE the plan
    is still active, non-zero, and hasn't run this period), so a concurrent
    button-click and sweep can never double-bill: exactly one wins the claim and
    inserts; the other gets rowcount 0 and returns None. Returns the new invoice
    id, or None if the plan was ineligible / already claimed.
    """
    with db.tx() as con:
        claimed = con.execute(
            "UPDATE recurring_plans SET last_run_period=? "
            "WHERE id=? AND active=1 AND total_cents>0 "
            "AND deleted_at IS NULL AND COALESCE(last_run_period,'')<>?",
            (period, plan["id"], period),
        ).rowcount
        if claimed != 1:
            return None
        cur = con.execute(
            "INSERT INTO invoices (project_id, slug, title, line_items, total_cents, "
            "recurring_plan_id) VALUES (?,?,?,?,?,?)",
            (
                plan["project_id"],
                security.new_slug(),
                f"{plan['title']} — {period}",
                plan["line_items"],
                plan["total_cents"],
                plan["id"],
            ),
        )
        iid = cur.lastrowid
        audit.log(
            con,
            "invoice",
            iid,
            "create",
            diff={
                "recurring_plan_id": plan["id"],
                "period": period,
                "total_cents": plan["total_cents"],
            },
        )
    log.info("recurring plan %s generated draft invoice %s for %s", plan["id"], iid, period)
    return iid


def run_due_plans(today: dt.date | None = None) -> int:
    """Scheduler entry point: generate this period's draft for every active plan
    whose anchor day has arrived and that hasn't run yet. Idempotent — the period
    claim in generate_for_plan means repeated sweeps can't double-bill, so this is
    safe to call as often as the thread wakes. Returns the count generated.
    """
    today = today or dt.date.today()
    period = _period(today)
    due = db.all_(
        "SELECT * FROM recurring_plans WHERE active=1 AND total_cents>0 "
        "AND deleted_at IS NULL AND anchor_day<=? AND COALESCE(last_run_period,'')<>?",
        (today.day, period),
    )
    n = sum(1 for plan in due if generate_for_plan(plan, period) is not None)
    if n:
        log.info("recurring sweep: generated %d draft(s) for %s", n, period)
    return n


@router.post("/recurring/{plan_id}/generate")
async def generate_draft(plan_id: int):
    d = get_plan(plan_id)
    if not d["active"]:
        raise HTTPException(status_code=400, detail="plan is paused — activate it first")
    if d["total_cents"] <= 0:
        raise HTTPException(status_code=400, detail="plan total must be above zero")
    period = _period()
    if d["last_run_period"] == period:
        raise HTTPException(status_code=400, detail=f"already generated a draft for {period}")
    iid = generate_for_plan(d, period)
    if iid is None:  # raced a concurrent sweep/click between the check and the claim
        raise HTTPException(status_code=400, detail=f"already generated a draft for {period}")
    return RedirectResponse(f"/admin/studio/invoices/{iid}", status_code=303)


@router.post("/recurring/{plan_id}/delete")
async def delete_plan(plan_id: int):
    d = get_plan(plan_id)
    with db.tx() as con:
        con.execute("UPDATE recurring_plans SET deleted_at=datetime('now') WHERE id=?", (plan_id,))
        audit.log(con, "recurring_plan", plan_id, "delete")
    log.info("recurring plan %s soft-deleted", plan_id)
    return RedirectResponse(f"/admin/studio/projects/{d['project_id']}", status_code=303)
