"""Invoices at /i/{slug} + Stripe Checkout + signature-verified webhook."""

import json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import alerts, audit, config, db, features, jobs, security
from ..render import templates
from . import telemetry

log = logging.getLogger("mise.public.pay")
router = APIRouter()

# Real Stripe events are a few KB; anything near 1 MB is junk. The signature is
# verified only after the body is read, so an unbounded read is a memory DoS —
# reject on Content-Length up front and on the actual length after reading.
_MAX_BODY = 1 << 20


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
def view_invoice(request: Request, slug: str, thanks: str = ""):
    d = _invoice_or_404(slug)
    # Read telemetry only — never payment state. Gated so a mail scanner or a
    # HEAD probe cannot fabricate "the client has seen this invoice"
    # (app/public/telemetry.py).
    if d["status"] == "sent" and telemetry.is_human_view(request):
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
    # htmx polls the status region while awaiting_confirmation (the template
    # carries hx-get then, instead of the old <meta refresh>). Same context —
    # a rendering fork, never a payment-logic one.
    template = (
        "public/_invoice_status.html"
        if request.headers.get("hx-request") == "true"
        else "public/invoice.html"
    )
    return templates.TemplateResponse(
        request,
        template,
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
def view_receipt(request: Request, slug: str):
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


def _stripe_value(obj, key: str, default=None):
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, default)


def _invoice_checkout(d, amount: int, kind: str, slug: str):
    """Return one authoritative Checkout Session for this invoice installment."""
    currency = "usd"
    prior_id = d["stripe_session_id"]
    if prior_id:
        try:
            prior = stripe.checkout.Session.retrieve(
                prior_id,
                api_key=config.STRIPE_SECRET_KEY,
            )
        except stripe.StripeError as exc:
            log.error("invoice %s checkout %s could not be verified: %s", d["id"], prior_id, exc)
            raise HTTPException(status_code=503, detail="payment is temporarily unavailable")

        snapshot_matches = (
            d["stripe_checkout_amount_cents"] == amount
            and d["stripe_checkout_kind"] == kind
            and d["stripe_checkout_currency"] == currency
        )
        status = _stripe_value(prior, "status")
        if snapshot_matches and status in ("open", "complete"):
            return prior
        if status == "open":
            try:
                stripe.checkout.Session.expire(
                    prior_id,
                    api_key=config.STRIPE_SECRET_KEY,
                    idempotency_key=f"mise:invoice:{d['id']}:expire:{prior_id}",
                )
            except stripe.StripeError as exc:
                log.error(
                    "invoice %s stale checkout %s could not be expired: %s",
                    d["id"],
                    prior_id,
                    exc,
                )
                raise HTTPException(status_code=503, detail="payment is temporarily unavailable")

    label = {"deposit": "Deposit", "balance": "Balance", "full": "Payment"}[kind]
    predecessor = prior_id or "initial"
    session = stripe.checkout.Session.create(
        api_key=config.STRIPE_SECRET_KEY,
        idempotency_key=f"mise:invoice:{d['id']}:{kind}:{amount}:{predecessor}",
        mode="payment",
        payment_method_types=["card", "us_bank_account"],
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency,
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
    db.run(
        """UPDATE invoices
              SET stripe_session_id=?, stripe_checkout_amount_cents=?,
                  stripe_checkout_kind=?, stripe_checkout_currency=?
            WHERE id=?""",
        (session.id, amount, kind, currency, d["id"]),
    )
    log.info("invoice %s checkout %s created (%s, %s cents)", d["id"], session.id, kind, amount)
    return session


@router.post("/i/{slug}/pay")
def pay_invoice(request: Request, slug: str):
    d = _invoice_or_404(slug)
    amount, kind = next_payment(d)
    if not amount:
        raise HTTPException(status_code=400, detail="nothing due on this invoice")
    if not features.stripe_enabled():
        raise HTTPException(status_code=503, detail="online payment is not configured")
    session = _invoice_checkout(d, amount, kind, slug)
    if _stripe_value(session, "status") == "complete":
        return RedirectResponse(f"/i/{slug}?thanks=1", status_code=303)
    url = _stripe_value(session, "url")
    if not url:
        raise HTTPException(status_code=503, detail="payment is temporarily unavailable")
    return RedirectResponse(url, status_code=303)


class _PaymentStateConflict(Exception):
    pass


def _ack_unapplied(
    event_id: str, invoice_id, code: str, detail: str, diff: dict, *, entity: str = "invoice"
) -> dict:
    """Acknowledge a webhook that can never be applied, instead of 4xx-ing it.

    Stripe treats ANY non-2xx as a failed delivery, retries for ~3 days, and
    counts the failures toward auto-disabling the endpoint — after which real
    payment events stop arriving and the first symptom is missing money. The
    conditions below are permanent: retrying cannot un-settle an invoice, cannot
    change what a stale Checkout session charged, and cannot conjure an invoice
    row. So the 409/404 bought nothing — none of these record a payment either
    way — while spending the endpoint's health and firing one unthrottled alert
    per delivery for three days.

    Stripe's retries were also the only thing keeping such an anomaly persistent,
    so acknowledging moves the record somewhere durable: an append-only audit row
    that outlives the process and survives Telegram being unconfigured. The alert
    is best-effort on top of that, not the system of record.
    """
    try:
        with db.tx() as con:
            audit.log(con, entity, invoice_id, f"stripe_{code}", diff=diff, actor="stripe")
    except Exception:
        # The audit row is the durable half, but failing to write it must not
        # resurrect the retry storm — log loudly and still acknowledge.
        log.exception("could not audit stripe anomaly invoice=%s event=%s", invoice_id, event_id)
    alerts.payment_anomaly(invoice_id, event_id, code, detail, entity=entity)
    return {"ok": True, "unapplied": code}


def _apply_booking_payment(event, session, metadata) -> dict:
    """Confirm exactly one authorized payment for a pay-to-book hold."""
    from .. import booking_notify

    booking_id = metadata["booking_id"] if "booking_id" in metadata else None
    if not booking_id or not str(booking_id).isdigit():
        return {"ok": True, "ignored": "no booking_id"}
    booking_id = int(booking_id)
    session_id = str(_stripe_value(session, "id", "") or "")
    amount_total = int(_stripe_value(session, "amount_total", 0) or 0)
    currency = str(_stripe_value(session, "currency", "") or "").lower()
    b = db.one("SELECT * FROM bookings WHERE id=?", (booking_id,))
    if not b:
        return _ack_unapplied(
            event["id"],
            booking_id,
            "unknown_booking",
            f"session {session_id} names booking {booking_id}, which does not exist",
            {"session_id": session_id, "amount_total": amount_total, "currency": currency},
            entity="booking",
        )
    if db.one("SELECT id FROM booking_payments WHERE stripe_event_id=?", (event["id"],)):
        return {"ok": True, "duplicate": True}

    expected_session = b["stripe_session_id"]
    expected_amount = b["stripe_checkout_amount_cents"]
    expected_currency = b["stripe_checkout_currency"]
    if not expected_session or expected_amount is None or not expected_currency:
        return _ack_unapplied(
            event["id"],
            booking_id,
            "checkout_not_bound",
            "booking has no complete authoritative Checkout snapshot",
            {"session_id": session_id, "amount_total": amount_total, "currency": currency},
            entity="booking",
        )
    if session_id != expected_session:
        return _ack_unapplied(
            event["id"],
            booking_id,
            "stale_session",
            f"session {session_id} is not authoritative for this booking",
            {"session_id": session_id, "expected_session_id": expected_session},
            entity="booking",
        )
    if amount_total != expected_amount or currency != expected_currency:
        return _ack_unapplied(
            event["id"],
            booking_id,
            "checkout_mismatch",
            "session amount or currency differs from the authorized Checkout snapshot",
            {
                "session_id": session_id,
                "got_amount": amount_total,
                "got_currency": currency,
                "expected_amount": expected_amount,
                "expected_currency": expected_currency,
            },
            entity="booking",
        )
    if b["status"] == "cancelled":
        return _ack_unapplied(
            event["id"],
            booking_id,
            "paid_after_release",
            f"{amount_total} cents arrived for a booking hold that was already "
            f"released ({b['cancel_reason'] or 'cancelled'}) — the slot may have "
            f"been re-sold; refund or re-book by hand",
            {"amount_total": amount_total, "session_id": session_id},
            entity="booking",
        )
    if b["status"] == "confirmed":
        return _ack_unapplied(
            event["id"],
            booking_id,
            "already_settled",
            "a distinct paid Checkout arrived after this booking was confirmed",
            {"amount_total": amount_total, "session_id": session_id},
            entity="booking",
        )
    if b["status"] != "pending_payment":
        return _ack_unapplied(
            event["id"],
            booking_id,
            "state_conflict",
            f"booking state {b['status']} cannot accept payment",
            {"amount_total": amount_total, "session_id": session_id},
            entity="booking",
        )

    try:
        with db.tx() as con:
            con.execute(
                """INSERT INTO booking_payments
                       (booking_id, stripe_event_id, stripe_session_id, amount_cents)
                   VALUES (?,?,?,?)""",
                (booking_id, event["id"], session_id, amount_total),
            )
            changed = con.execute(
                """UPDATE bookings
                      SET status='confirmed', paid_cents=?, paid_at=datetime('now')
                    WHERE id=? AND status='pending_payment'""",
                (amount_total, booking_id),
            )
            if changed.rowcount != 1:
                raise _PaymentStateConflict
    except _PaymentStateConflict:
        return _ack_unapplied(
            event["id"],
            booking_id,
            "state_conflict",
            "booking state changed while applying the payment",
            {"amount_total": amount_total, "session_id": session_id},
            entity="booking",
        )
    except db.sqlite3.IntegrityError:
        if db.one("SELECT id FROM booking_payments WHERE stripe_event_id=?", (event["id"],)):
            return {"ok": True, "duplicate": True}
        prior = db.one(
            """SELECT id FROM booking_payments
                 WHERE booking_id=? OR stripe_session_id=?""",
            (booking_id, session_id),
        )
        return _ack_unapplied(
            event["id"],
            booking_id,
            "duplicate_payment" if prior else "constraint_conflict",
            "a distinct Stripe event conflicts with an existing booking payment",
            {"amount_total": amount_total, "session_id": session_id},
            entity="booking",
        )
    # Side-effects only after the money and state transition are durable.
    booking_notify.confirm(booking_id)
    log.info(
        "booking %s confirmed by payment: %s cents (event %s)",
        booking_id,
        amount_total,
        event["id"],
    )
    return {"ok": True}


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Read the body, then get off the loop.

    Everything past the read is blocking: signature verification is HMAC over
    the payload, and the handler then makes roughly six database round-trips
    (invoice lookup, idempotency probe, the payment transaction, the staged job)
    before it answers. On the event loop those stall EVERY other in-flight
    request — and this is the one route whose caller is a machine that retries,
    so a slow database here turns into Stripe redelivering into a server already
    struggling.

    The body read itself cannot move: it is the await. Same split as the rest of
    the de-async sweep (see the fence in tests/test_deasync.py); the size checks
    stay here because they touch nothing but the request.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="payload too large")
    payload = await request.body()
    if len(payload) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="payload too large")
    signature = request.headers.get("stripe-signature", "")
    return await run_in_threadpool(_process_stripe_webhook, payload, signature)


def _process_stripe_webhook(payload: bytes, signature: str):
    # The configured check moved in here with the rest of the app-code calls, so
    # an oversized body on an unconfigured host now answers 413 before 503. Both
    # are refusals and the combination cannot arise from Stripe.
    if not features.stripe_webhook_enabled():
        raise HTTPException(status_code=503, detail="webhook not configured")
    try:
        event = stripe.Webhook.construct_event(payload, signature, config.STRIPE_WEBHOOK_SECRET)
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

    # StripeObject has no .get() — `in` + [] is the equivalent. A foreign/stray
    # checkout session on the same Stripe account carries no usable invoice_id:
    # ack and ignore instead of KeyError → 500, which makes Stripe retry and
    # eventually disable the webhook endpoint.
    metadata = (session["metadata"] if "metadata" in session else None) or {}
    if ("kind" in metadata and metadata["kind"] == "booking") or "booking_id" in metadata:
        return _apply_booking_payment(event, session, metadata)
    invoice_id = metadata["invoice_id"] if "invoice_id" in metadata else None
    if not invoice_id or not str(invoice_id).isdigit():
        return {"ok": True, "ignored": "no invoice_id"}
    invoice_id = int(invoice_id)
    kind = metadata["kind"] if "kind" in metadata else None
    d = db.one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not d:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "unknown_invoice",
            f"session {session['id']} names invoice {invoice_id}, which does not exist",
            {"session_id": session["id"], "amount_total": int(session["amount_total"] or 0)},
        )
    # Stripe retries the same event_id after a 5xx / timeout. Honor idempotency
    # BEFORE amount/kind checks — a successful deposit changes what is "owed",
    # so a retry would otherwise look like a mismatch and 409 forever.
    if db.one("SELECT id FROM payments WHERE stripe_event_id=?", (event["id"],)):
        return {"ok": True, "duplicate": True}
    session_id = str(_stripe_value(session, "id", "") or "")
    amount_total = int(_stripe_value(session, "amount_total", 0) or 0)
    currency = str(_stripe_value(session, "currency", "") or "").lower()
    expected_session = d["stripe_session_id"]
    expected_amount = d["stripe_checkout_amount_cents"]
    expected_kind = d["stripe_checkout_kind"]
    expected_currency = d["stripe_checkout_currency"]

    if (
        not expected_session
        or expected_amount is None
        or not expected_kind
        or not expected_currency
    ):
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "checkout_not_bound",
            "invoice has no complete authoritative Checkout snapshot",
            {"session_id": session_id, "amount_total": amount_total, "currency": currency},
        )
    if session_id != expected_session:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "stale_session",
            f"session {session_id} is not authoritative for this invoice",
            {"session_id": session_id, "expected_session_id": expected_session},
        )
    if kind != expected_kind or amount_total != expected_amount or currency != expected_currency:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "checkout_mismatch",
            "session kind, amount, or currency differs from the authorized Checkout snapshot",
            {
                "got_kind": kind,
                "got_amount": amount_total,
                "got_currency": currency,
                "expected_kind": expected_kind,
                "expected_amount": expected_amount,
                "expected_currency": expected_currency,
                "session_id": session_id,
            },
        )

    owed_cents, owed_kind = next_payment(d)
    if not owed_cents:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "already_settled",
            f"invoice is fully settled but a payment of {amount_total} cents arrived",
            {"amount_total": amount_total, "kind": kind, "session_id": session_id},
        )
    if kind != owed_kind or amount_total != owed_cents:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "invoice_changed",
            "invoice state changed after Checkout creation",
            {
                "got_kind": kind,
                "got_amount": amount_total,
                "owed_kind": owed_kind,
                "owed_amount": owed_cents,
                "session_id": session_id,
            },
        )
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
                changed = con.execute(
                    """UPDATE invoices SET status='deposit_paid'
                         WHERE id=? AND status IN ('sent','viewed')""",
                    (invoice_id,),
                )
            elif kind == "balance":
                changed = con.execute(
                    """UPDATE invoices SET status='paid', paid_at=datetime('now')
                         WHERE id=? AND status='deposit_paid'""",
                    (invoice_id,),
                )
            else:
                changed = con.execute(
                    """UPDATE invoices SET status='paid', paid_at=datetime('now')
                         WHERE id=? AND status IN ('sent','viewed')""",
                    (invoice_id,),
                )
            if changed.rowcount != 1:
                raise _PaymentStateConflict
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
            # Staged INSIDE the transaction, not enqueued after it. jobs.enqueue
            # writes its own row in a separate transaction, so a crash, a restart,
            # or a killed worker between the commit above and that call left the
            # payment recorded with no sync job and nothing to notice it: Stripe's
            # retry short-circuits on the duplicate event id, the sweeper only
            # re-offers rows that exist, and Notion stays silently stale. Staging
            # makes the job as durable as the payment — they commit together or
            # not at all, which is the pattern uploads.py already uses for work
            # far less important than this.
            notion_job = jobs.stage(con, "notion_sync_invoice", {"invoice_id": invoice_id})
    except _PaymentStateConflict:
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "state_conflict",
            "invoice state changed while applying the payment",
            {"session_id": session_id, "kind": kind, "amount_total": amount_total},
        )
    except db.sqlite3.IntegrityError:
        if db.one("SELECT id FROM payments WHERE stripe_event_id=?", (event["id"],)):
            return {"ok": True, "duplicate": True}
        prior = db.one(
            """SELECT id FROM payments
                 WHERE (invoice_id=? AND kind=?) OR stripe_session_id=?""",
            (invoice_id, kind, session_id),
        )
        return _ack_unapplied(
            event["id"],
            invoice_id,
            "duplicate_installment" if prior else "constraint_conflict",
            "a distinct Stripe event conflicts with an existing payment invariant",
            {"session_id": session_id, "kind": kind, "amount_total": amount_total},
        )
    # Offer it to the pool only after the commit: a durable row means a lost
    # dispatch is recoverable (the sweeper picks it up), while dispatching a row
    # that might still roll back is not.
    jobs.dispatch([notion_job])
    log.info(
        "invoice %s payment recorded: %s %s cents (event %s)",
        invoice_id,
        kind,
        session["amount_total"],
        event["id"],
    )
    return {"ok": True}
