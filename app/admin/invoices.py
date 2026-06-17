"""Invoices — line items + optional deposit split. Send locks; Stripe handles payment."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, jobs, security
from ..render import templates
from .proposals import MAX_ITEM_ROWS, parse_items
from .studio import get_project

log = logging.getLogger("mise.admin.invoices")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])


def get_invoice(invoice_id: int) -> "db.sqlite3.Row":
    d = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not d:
        raise HTTPException(status_code=404)
    return d


@router.post("/projects/{project_id}/invoices")
async def create_invoice(project_id: int):
    p = get_project(project_id)
    accepted = db.one("""SELECT line_items, total_cents FROM proposals
                         WHERE project_id=? AND status='accepted'
                         ORDER BY accepted_at DESC LIMIT 1""", (project_id,))
    items = accepted["line_items"] if accepted else "[]"
    total = accepted["total_cents"] if accepted else 0
    did = db.run("""INSERT INTO invoices (project_id, slug, title, line_items, total_cents)
                    VALUES (?,?,?,?,?)""",
                 (project_id, security.new_slug(), f"Invoice — {p['title']}", items, total))
    log.info("invoice %s created for project %s (seeded=%s)", did, project_id, bool(accepted))
    return RedirectResponse(f"/admin/studio/invoices/{did}", status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail(request: Request, invoice_id: int):
    d = get_invoice(invoice_id)
    p = get_project(d["project_id"])
    items = json.loads(d["line_items"])
    rows = items + [{} for _ in range(max(0, MAX_ITEM_ROWS - len(items)))]
    payments = db.all_("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,))
    return templates.TemplateResponse(request, "admin/invoice.html",
                                      {"d": d, "p": p, "rows": rows, "payments": payments,
                                       "base_url": config.BASE_URL})


@router.post("/invoices/{invoice_id}")
async def update_invoice(request: Request, invoice_id: int):
    d = get_invoice(invoice_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent invoices are locked")
    form = await request.form()
    items_json, total = parse_items(form)
    try:
        deposit = round(float(form.get("deposit") or "0") * 100)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad deposit amount")
    if deposit < 0 or deposit > total:
        raise HTTPException(status_code=400, detail="deposit must be between 0 and the total")
    db.run("""UPDATE invoices SET title=?, line_items=?, total_cents=?, deposit_cents=?,
              due_date=?, terms=? WHERE id=?""",
           ((form.get("title") or "").strip() or d["title"], items_json, total, deposit,
            (form.get("due_date") or "").strip() or None,
            (form.get("terms") or "").strip() or None, invoice_id))
    return RedirectResponse(f"/admin/studio/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/duplicate")
async def duplicate_invoice(invoice_id: int):
    """Clone a locked invoice (sent/viewed/paid) into a fresh editable draft.
    Copies title/line items/total/deposit/due date/terms under a new slug; the new
    draft carries no payments, Stripe session, or paid status. The original — and the
    payments recorded against it — is untouched."""
    d = get_invoice(invoice_id)
    did = db.run("""INSERT INTO invoices (project_id, slug, title, line_items,
                    total_cents, deposit_cents, due_date, terms)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (d["project_id"], security.new_slug(), d["title"], d["line_items"],
                  d["total_cents"], d["deposit_cents"], d["due_date"], d["terms"]))
    log.info("invoice %s duplicated → %s (new draft)", invoice_id, did)
    return RedirectResponse(f"/admin/studio/invoices/{did}", status_code=303)


@router.post("/invoices/{invoice_id}/send")
async def mark_invoice_sent(invoice_id: int):
    d = get_invoice(invoice_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    if d["total_cents"] <= 0:
        raise HTTPException(status_code=400, detail="invoice total must be above zero")
    db.run("UPDATE invoices SET status='sent', sent_at=datetime('now') WHERE id=?",
           (invoice_id,))
    jobs.enqueue("notion_sync_invoice", {"invoice_id": invoice_id})
    log.info("invoice %s marked sent", invoice_id)
    return RedirectResponse(f"/admin/studio/invoices/{invoice_id}", status_code=303)
