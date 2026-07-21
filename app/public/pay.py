"""Invoices at /i/{slug} + Stripe Checkout + signature-verified webhook."""

import json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import alerts, config, db, features, jobs, security
from ..render import templates

log = logging.getLogger("mise.public.pay")
router = APIRouter()


def _invoice_or_404(slug: str) -> "db.sqlite3.Row":
    d = db.one(
        """SELECT i.*, p.title AS project_title, c.name AS client_name,
                         c.company, c.email AS client_email
                  FROM invoices i
                  JOIN projects p ON p.id=i.project_id
                  JOIN clients c ON c.id=p.client_id
                  WHERE i.slug=?""",
        (slug,),
    )
    if not d or d["status"] == "draft":
        raise HTTPException(status_code=404)
    return d


def next_payment(d: "db.sqlite3.Row") -> tuple[int, str]:
    """(amount_cents, kind) still owed — (0, '') when settled."""
    if d["status"] == "paid":
        return 0, ""
    if d["status"] == "deposit_paid":
        return d["total_cents"] - d["deposit_cents"], "balance"
    if d["deposit_cents"]:
        return d["deposit_cents"], "deposit"
    return d["total_cents"], "full"


@router.get("/i/{slug}", response_class=HTMLResponse)
async def view_invoice(request: Request, slug: str, thanks: str = ""):
    d = _invoice_or_404(slug)
    if d["status"] == "sent":
        db.run(
            "UPDATE invoices SET status='viewed', viewed_at=datetime('now') WHERE id=?", (d["id"],)
        )
        log.info("invoice %s viewed from %s", d["id"], security.client_ip(request))
    amount, kind = next_payment(d)
    paid_cents = db.one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS c
                           FROM payments WHERE invoice_id=?""",
        (d["id"],),
    )["c"]
    # Stripe bounces the client back here with ?thanks=1 after Checkout. If the
    # webhook that records the payment hasn't landed yet (cards: seconds; ACH:
    # days), the invoice would still read "amount due" with a live Pay button —
    # moments after they paid, inviting a double charge. The return param is
    # treated as PRESENTATION ONLY (reassure + hide the button); the webhook
    # stays the sole writer of payment state, so a forged ?thanks=1 can't mark
    # anything paid. A recent payment row means the webhook already landed and
    # the normal paid/deposit copy tells the story, so no banner.
    awaiting_confirmation = False
    if thanks and amount:
        recent = db.one(
            """SELECT 1 AS x FROM payments WHERE invoice_id=?
               AND created_at > datetime('now', '-30 minutes')""",
            (d["id"],),
        )
        awaiting_confirmation = recent is None
    return templates.TemplateResponse(
        request,
        "public/invoice.html",
        {
            "d": d,
            "items": json.loads(d["line_items"]),
            "amount_due": amount,
            "pay_kind": kind,
            "paid_cents": paid_cents,
            "payments_on": features.stripe_enabled(),
            "awaiting_confirmation": awaiting_confirmation,
        },
    )


@router.get("/i/{slug}/receipt", response_class=HTMLResponse)
async def view_receipt(request: Request, slug: str):
    """Printable receipt — a read-only render of payments Stripe already
    recorded, so it can never disagree with what was charged. 404 until at
    least one payment exists."""
    d = _invoice_or_404(slug)
    payments = db.all_(
        """SELECT amount_cents, kind, created_at
                          FROM payments WHERE invoice_id=?
                          ORDER BY created_at""",
        (d["id"],),
    )
    if not payments:
        raise HTTPException(status_code=404)
    paid_cents = sum(p["amount_cents"] for p in payments)
    return templates.TemplateResponse(
        request,
        "public/receipt.html",
        {
            "d": d,
            "payments": payments,
            "paid_cents": paid_cents,
            "remaining_cents": max(0, d["total_cents"] - paid_cents),
        },
    )


@router.post("/i/{slug}/pay")
async def pay_invoice(request: Request, slug: str):
    d = _invoice_or_404(slug)
    amount, kind = next_payment(d)
    if not amount:
        raise HTTPException(status_code=400, detail="nothing due on this invoice")
    if not features.stripe_enabled():
        raise HTTPException(status_code=503, detail="online payment is not configured")
    label = {"deposit": "Deposit", "balance": "Balance", "full": "Payment"}[kind]
    session = stripe.checkout.Session.create(
        api_key=config.STRIPE_SECRET_KEY,
        mode="payment",
        payment_method_types=["card", "us_bank_account"],
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount,
                    "product_data": {"name": f"{label} — {d['title']}"},
                },
            }
        ],
        customer_email=d["client_email"] or None,
        metadata={"invoice_id": str(d["id"]), "kind": kind},
        success_url=f"{config.BASE_URL}/i/{slug}?thanks=1",
        cancel_url=f"{config.BASE_URL}/i/{slug}",
    )
    db.run("UPDATE invoices SET stripe_session_id=? WHERE id=?", (session.id, d["id"]))
    log.info("invoice %s checkout %s created (%s, %s cents)", d["id"], session.id, kind, amount)
    return RedirectResponse(session.url, status_code=303)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not features.stripe_webhook_enabled():
        raise HTTPException(status_code=503, detail="webhook not configured")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, request.headers.get("stripe-signature", ""), config.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="bad signature")

    if event["type"] not in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        return {"ok": True, "ignored": event["type"]}
    session = event["data"]["object"]
    if session["payment_status"] != "paid":  # ACH settles via the async event
        return {"ok": True, "pending": True}

    invoice_id = int(session["metadata"]["invoice_id"])
    kind = session["metadata"]["kind"]
    d = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not d:
        log.error("stripe webhook for unknown invoice %s", invoice_id)
        raise HTTPException(status_code=404)
    # Stripe retries the same event_id after a 5xx / timeout. Honor idempotency
    # BEFORE amount/kind checks — a successful deposit changes what is "owed",
    # so a retry would otherwise look like a mismatch and 409 forever.
    if db.one("SELECT id FROM payments WHERE stripe_event_id=?", (event["id"],)):
        return {"ok": True, "duplicate": True}
    # Defense in depth: Checkout metadata + amount_total are not trusted blindly.
    # A stale session (client paid an old Checkout after the invoice changed) or
    # metadata drift must not mark the wrong amount/kind paid.
    owed_cents, owed_kind = next_payment(d)
    amount_total = int(session["amount_total"] or 0)
    if not owed_cents:
        log.error("stripe webhook for settled invoice %s (event %s)", invoice_id, event["id"])
        alerts.security_alert(f"Stripe webhook for already-settled invoice {invoice_id}")
        raise HTTPException(status_code=409, detail="invoice already settled")
    if kind != owed_kind or amount_total != owed_cents:
        log.error(
            "stripe webhook mismatch invoice %s: got kind=%s amount=%s; owed kind=%s amount=%s",
            invoice_id,
            kind,
            amount_total,
            owed_kind,
            owed_cents,
        )
        alerts.security_alert(
            f"Stripe webhook amount/kind mismatch on invoice {invoice_id}: "
            f"got {kind}/{amount_total}, owed {owed_kind}/{owed_cents}"
        )
        raise HTTPException(status_code=409, detail="payment does not match amount owed")
    # Record the payment and advance invoice + project state as one atomic unit:
    # a crash between these writes would otherwise leave the payment logged but the
    # invoice unpaid, and Stripe's retry would short-circuit on the duplicate event
    # id (below) without ever repairing it. The INSERT runs first, so a duplicate
    # event rolls the whole tx back with nothing else written.
    try:
        with db.tx() as con:
            con.execute(
                """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                      amount_cents, kind) VALUES (?,?,?,?,?)""",
                (invoice_id, event["id"], session["id"], amount_total, kind),
            )
            if kind == "deposit":
                con.execute("UPDATE invoices SET status='deposit_paid' WHERE id=?", (invoice_id,))
            else:
                con.execute(
                    "UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?",
                    (invoice_id,),
                )
            # Payment landed → advance the project to Retainer Paid (the funnel's
            # money gate). Only moves forward from pre-payment stages; never rewinds
            # a project already at session planning / closed / archived.
            con.execute(
                """UPDATE projects SET status='retainer_paid',
                      stage_changed_at=datetime('now') WHERE id=?
                      AND status IN ('inquiry_received','consultation_call',
                                     'proposal_sent','contract_signed')""",
                (d["project_id"],),
            )
    except db.sqlite3.IntegrityError:
        return {"ok": True, "duplicate": True}  # Stripe retries — idempotent by event id
    jobs.enqueue("notion_sync_invoice", {"invoice_id": invoice_id})
    log.info(
        "invoice %s payment recorded: %s %s cents (event %s)",
        invoice_id,
        kind,
        session["amount_total"],
        event["id"],
    )
    return {"ok": True}
