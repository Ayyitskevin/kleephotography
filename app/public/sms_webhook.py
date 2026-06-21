"""Inbound SMS webhook — Quo (formerly OpenPhone) posts here on each new message.

Mirrors the Stripe webhook posture (pay.py): 503 until the signing secret is set,
fail-closed signature check, idempotent by the provider's message id. An inbound
text from a number we've never seen auto-creates a kind='sms' inquiry so it lands
in the Inbox and reuses every convert action (quote / client / dismiss) unchanged.

Ships INERT: with no MISE_QUO_WEBHOOK_SECRET this route returns 503 and writes
nothing. See app/sms.py for the wire-format details that must be confirmed against
Quo's live docs before arming.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from .. import config, db, sms

log = logging.getLogger("mise.public.sms_webhook")
router = APIRouter()


def _digits(phone: str) -> str:
    """Last-10 digits, for matching an inbound number to an inquiry regardless of
    +1 / formatting differences."""
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


@router.post("/webhooks/quo")
async def quo_webhook(request: Request):
    if not config.QUO_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="sms webhook not configured")
    raw = await request.body()
    if not sms.verify_webhook(raw, request.headers.get("openphone-signature", "")):
        raise HTTPException(status_code=400, detail="bad signature")
    try:
        event = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="unreadable payload")

    # Only inbound text events create a thread row. Quo nests the message under
    # data.object; tolerate a flatter shape. Anything else (delivery receipts,
    # outbound echoes) is acknowledged and ignored.
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or event.get("data") or {}
    direction = (obj.get("direction") or "").lower()
    if "message" not in etype or direction not in ("incoming", "inbound", "in"):
        return {"ok": True, "ignored": etype or "unknown"}

    from_phone = (obj.get("from") or "").strip()
    body = (obj.get("body") or obj.get("content") or obj.get("text") or "").strip()
    provider_msg_id = (obj.get("id") or "").strip() or None
    if not from_phone:
        return {"ok": True, "ignored": "no from number"}

    # Match against prior SMS inquiries (always stored E.164) by trailing 10 digits,
    # so +1 / formatting differences don't fork a contact into two threads. Web-form
    # inquiries (phone='') are excluded.
    digits = _digits(from_phone)
    inq = db.one(
        "SELECT id FROM inquiries WHERE phone!='' AND "
        "substr(replace(replace(replace(replace(replace(phone,'+',''),'-',''),' ',''),'(',''),')','') , -10) = ? "
        "ORDER BY id DESC LIMIT 1", (digits,)) if digits else None
    if inq:
        inquiry_id = inq["id"]
    else:
        # Unknown number → new kind='sms' inquiry. No name/email yet, so seed
        # honest placeholders (email is NOT NULL); the convert flow fills real
        # details. The first text becomes the inquiry message too.
        inquiry_id = db.run(
            """INSERT INTO inquiries (name, email, business, message, kind, phone, emailed)
               VALUES (?,?,?,?,?,?,0)""",
            (from_phone, "", None, body or "(no text)", "sms", from_phone))
        log.info("inbound sms from new number created inquiry %s", inquiry_id)

    try:
        db.run("""INSERT INTO messages (inquiry_id, direction, channel, body, provider_msg_id)
                  VALUES (?, 'in', 'sms', ?, ?)""",
               (inquiry_id, body, provider_msg_id))
    except db.sqlite3.IntegrityError:
        return {"ok": True, "duplicate": True}  # Quo retries — idempotent by msg id
    log.info("inbound sms recorded on inquiry %s (%d chars)", inquiry_id, len(body))
    return {"ok": True}
