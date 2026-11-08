"""Invoices — line items + optional deposit split. Send locks; Stripe handles payment,
and a payment received outside Stripe (cash, check, transfer) is recorded here."""

import datetime as dt
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, config, db, jobs, security
from ..public import pay
from ..redirects import redirect_local
from ..render import templates
from . import common, studio
from .lookups import get_project
from .proposals import MAX_ITEM_ROWS, parse_items

log = logging.getLogger("mise.admin.invoices")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Ways a client pays outside Stripe. The key is stored in the audit row.
OFFLINE_METHODS = {
    "cash": "Cash",
    "check": "Check",
    "bank_transfer": "Bank transfer",
    "other": "Other",
}
MAX_REFERENCE = 200
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def get_invoice(invoice_id: int) -> "db.sqlite3.Row":
    return db.get_or_404("SELECT * FROM invoices WHERE id=?", (invoice_id,))


@router.post("/projects/{project_id}/invoices")
def create_invoice(project_id: int):
    p = get_project(project_id)
    accepted = db.one(
        """SELECT line_items, total_cents FROM proposals
                         WHERE project_id=? AND status='accepted'
                         ORDER BY accepted_at DESC LIMIT 1""",
        (project_id,),
    )
    items = accepted["line_items"] if accepted else "[]"
    total = accepted["total_cents"] if accepted else 0
    did = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents)
                    VALUES (?,?,?,?,?)""",
        (project_id, security.new_slug(), f"Invoice — {p['title']}", items, total),
    )
    log.info("invoice %s created for project %s (seeded=%s)", did, project_id, bool(accepted))
    return RedirectResponse(f"/admin/studio/invoices/{did}", status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int):
    d = get_invoice(invoice_id)
    p = get_project(d["project_id"])
    items = json.loads(d["line_items"])
    rows = items + [{} for _ in range(max(0, MAX_ITEM_ROWS - len(items)))]
    payments = db.all_("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,))
    # How each offline payment was paid lives in its audit row (no schema change).
    offline = {
        r["entity_id"]: json.loads(r["diff_json"])
        for r in db.all_(
            """SELECT a.entity_id, a.diff_json FROM audit_log a
                 JOIN payments p ON p.id = a.entity_id
                WHERE a.entity_type='payment' AND a.action='offline_payment'
                  AND p.invoice_id=?""",
            (invoice_id,),
        )
    }
    owed_cents, owed_kind = pay.next_payment(d)
    return templates.TemplateResponse(
        request,
        "admin/invoice.html",
        {
            "d": d,
            "p": p,
            "rows": rows,
            "payments": payments,
            "offline": offline,
            "offline_methods": OFFLINE_METHODS,
            "owed_cents": owed_cents if d["status"] != "draft" else 0,
            "owed_kind": owed_kind,
            "today": studio._today().isoformat(),
            "base_url": config.BASE_URL,
            "email_sends": common.doc_emails_on_record("invoice", invoice_id),
        },
    )


@router.post("/invoices/{invoice_id}")
async def update_invoice(request: Request, invoice_id: int):
    d = await run_in_threadpool(get_invoice, invoice_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent invoices are locked")
    form = await request.form()
    return await run_in_threadpool(_update_invoice, d, form, invoice_id)


def _update_invoice(d: "db.sqlite3.Row", form, invoice_id: int):
    items_json, total = parse_items(form)
    try:
        deposit = common.parse_form_cents(form, "deposit")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad deposit amount")
    if deposit < 0 or deposit > total:
        raise HTTPException(status_code=400, detail="deposit must be between 0 and the total")
    db.run(
        """UPDATE invoices SET title=?, line_items=?, total_cents=?, deposit_cents=?,
              due_date=?, terms=? WHERE id=?""",
        (
            (form.get("title") or "").strip() or d["title"],
            items_json,
            total,
            deposit,
            (form.get("due_date") or "").strip() or None,
            (form.get("terms") or "").strip() or None,
            invoice_id,
        ),
    )
    return redirect_local(f"/admin/studio/invoices/{invoice_id}")


@router.post("/invoices/{invoice_id}/duplicate")
def duplicate_invoice(invoice_id: int):
    """Clone a locked invoice (sent/viewed/paid) into a fresh editable draft.
    Copies title/line items/total/deposit/due date/terms under a new slug; the new
    draft carries no payments, Stripe session, or paid status. The original — and the
    payments recorded against it — is untouched."""
    d = get_invoice(invoice_id)
    did = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items,
                    total_cents, deposit_cents, due_date, terms)
                    VALUES (?,?,?,?,?,?,?,?)""",
        (
            d["project_id"],
            security.new_slug(),
            d["title"],
            d["line_items"],
            d["total_cents"],
            d["deposit_cents"],
            d["due_date"],
            d["terms"],
        ),
    )
    log.info("invoice %s duplicated → %s (new draft)", invoice_id, did)
    return RedirectResponse(f"/admin/studio/invoices/{did}", status_code=303)


@router.post("/invoices/{invoice_id}/send")
def mark_invoice_sent(invoice_id: int):
    d = get_invoice(invoice_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    if d["total_cents"] <= 0:
        raise HTTPException(status_code=400, detail="invoice total must be above zero")
    db.run("UPDATE invoices SET status='sent', sent_at=datetime('now') WHERE id=?", (invoice_id,))
    jobs.enqueue("notion_sync_invoice", {"invoice_id": invoice_id})
    log.info("invoice %s marked sent", invoice_id)
    return redirect_local(f"/admin/studio/invoices/{invoice_id}")


@router.post("/invoices/{invoice_id}/offline-payment")
async def record_offline_payment(request: Request, invoice_id: int):
    form = await request.form()
    return await run_in_threadpool(_record_offline_payment, invoice_id, form)


def _record_offline_payment(invoice_id: int, form):
    """Record one payment received outside Stripe (cash, check, bank transfer).

    Money rules, all checked before anything is written:
    - only a sent or viewed invoice (deposit or full) or a deposit_paid one
      (balance) takes a payment; a draft or a fully paid invoice refuses;
    - the kind is the installment the invoice's state owes (pay.next_payment), and
      the amount must equal it exactly; there is no partial or over-payment;
    - the date received is a real date, not after the studio's today.
    The payment is then applied by pay.record_invoice_payment, the webhook's own
    function, so the invoice and project move exactly as a Stripe payment would,
    and the Notion sync is staged the same way. Nothing is sent to the client,
    as with a Stripe payment. One audit row in the same transaction records who
    recorded it and how it was paid. A second submit of the same installment is
    refused by the amount check, and a race by UNIQUE(invoice_id, kind)."""
    d = get_invoice(invoice_id)
    if d["status"] == "draft":
        raise HTTPException(status_code=400, detail="send the invoice before recording a payment")
    owed_cents, kind = pay.next_payment(d)
    if not owed_cents:
        raise HTTPException(status_code=400, detail="this invoice is already paid in full")
    method = (form.get("method") or "").strip()
    if method not in OFFLINE_METHODS:
        raise HTTPException(status_code=400, detail="pick cash, check, bank transfer or other")
    try:
        amount = common.parse_form_cents(form, "amount")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad amount")
    if amount != owed_cents:
        raise HTTPException(
            status_code=400,
            detail=f"the {kind} due is ${owed_cents / 100:.2f}; record exactly that amount",
        )
    raw_date = (form.get("received_on") or "").strip()
    try:
        if not _ISO_DATE.fullmatch(raw_date):
            raise ValueError
        received = dt.date.fromisoformat(raw_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date received must be YYYY-MM-DD")
    if received > studio._today():
        raise HTTPException(status_code=400, detail="the date received cannot be in the future")
    reference = (form.get("reference") or "").strip()
    if len(reference) > MAX_REFERENCE:
        raise HTTPException(
            status_code=400, detail=f"keep the reference under {MAX_REFERENCE} characters"
        )
    try:
        with db.tx() as con:
            # Re-read under the write lock: a Stripe payment may have landed since
            # the read above, and then this installment is no longer the one owed.
            current = con.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if pay.next_payment(current) != (amount, kind):
                raise pay.PaymentStateConflict
            payment_id, notion_job = pay.record_invoice_payment(
                con, current, kind, amount, received_at=f"{received.isoformat()} 12:00:00"
            )
            audit.log(
                con,
                "payment",
                payment_id,
                "offline_payment",
                diff={
                    "invoice_id": invoice_id,
                    "kind": kind,
                    "amount_cents": amount,
                    "method": method,
                    "received_on": received.isoformat(),
                    "reference": reference,
                },
            )
    except pay.PaymentStateConflict:
        raise HTTPException(
            status_code=409, detail="the invoice changed meanwhile; reload and check"
        )
    except db.sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="this installment is already recorded")
    jobs.dispatch([notion_job])
    log.info(
        "invoice %s offline payment recorded: %s %s cents by %s (payment %s)",
        invoice_id,
        kind,
        amount,
        method,
        payment_id,
    )
    return redirect_local(f"/admin/studio/invoices/{invoice_id}")
