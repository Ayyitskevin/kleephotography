"""Inbound Quo webhook — texts AND calls land in the unified Inbox.

Quo (formerly OpenPhone) posts here on each new message and call event. Mirrors
the Stripe webhook posture (pay.py): 503 until the signing secret is set,
fail-closed signature check, idempotent by the provider's id. An inbound text or
call from a number we've never seen auto-creates an inquiry (kind='sms' or
'call') so it lands in the Inbox and reuses every convert action (quote / client
/ dismiss) unchanged.

Calls log as channel='call' rows in the same thread as texts: call.completed
creates the row (Incoming/Outgoing/Missed + duration); later transcript/summary
events append voicemail text to that same row, matched by call id.

Ships INERT: with no MISE_QUO_WEBHOOK_SECRET this route returns 503 and writes
nothing. See app/sms.py for the wire-format details (OpenPhone v1 signing).
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from .. import config, db, sms

log = logging.getLogger("mise.public.sms_webhook")
router = APIRouter()

# Call statuses that mean nobody connected — rendered as a "Missed call".
_MISSED = {"missed", "no-answer", "no_answer", "unanswered", "rejected", "busy",
           "declined", "voicemail"}


def _digits(phone: str) -> str:
    """Last-10 digits, for matching an inbound number to an inquiry regardless of
    +1 / formatting differences."""
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def _match_inquiry(phone: str):
    """Find the most recent inquiry whose stored phone shares the trailing 10
    digits, so +1 / formatting differences don't fork a contact into two threads.
    Web-form inquiries (phone='') are excluded."""
    digits = _digits(phone)
    if not digits:
        return None
    return db.one(
        "SELECT id FROM inquiries WHERE phone!='' AND "
        "substr(replace(replace(replace(replace(replace(phone,'+',''),'-',''),' ',''),'(',''),')','') , -10) = ? "
        "ORDER BY id DESC LIMIT 1", (digits,))


def _ensure_inquiry(phone: str, first_body: str, kind: str) -> int:
    """Existing thread for this number, or a new one. New inquiries seed honest
    placeholders (email is NOT NULL); the convert flow fills real details."""
    inq = _match_inquiry(phone)
    if inq:
        return inq["id"]
    iid = db.run(
        """INSERT INTO inquiries (name, email, business, message, kind, phone, emailed)
           VALUES (?,?,?,?,?,?,0)""",
        (phone, "", None, first_body or "(no text)", kind, phone))
    log.info("inbound %s from new number created inquiry %s", kind, iid)
    return iid


def _call_summary(direction: str, status: str, duration) -> str:
    """One-line human summary for a call message bubble."""
    label = "Outgoing" if direction == "out" else "Incoming"
    if status in _MISSED:
        return "Missed call" if direction == "in" else f"{label} call · {status}"
    try:
        secs = int(duration or 0)
    except (TypeError, ValueError):
        secs = 0
    if secs:
        m, s = divmod(secs, 60)
        return f"{label} call · {m}m{s:02d}s"
    return f"{label} call"


def _call_enrichment(etype: str, obj: dict) -> str:
    """Text to append to an existing call row from a transcript/summary event.
    Defensive about field names — verified against a live call before trusting."""
    if etype == "call.transcript.completed":
        dlg = obj.get("dialogue") or obj.get("transcript") or obj.get("segments")
        if isinstance(dlg, list):
            parts = [(s.get("content") or s.get("text") or "").strip()
                     for s in dlg if isinstance(s, dict)]
            joined = " ".join(p for p in parts if p)
            return f"Transcript: {joined}" if joined else ""
        if isinstance(dlg, str) and dlg.strip():
            return f"Transcript: {dlg.strip()}"
        return ""
    if etype == "call.summary.completed":
        summ = obj.get("summary")
        if isinstance(summ, list):
            joined = " ".join(str(x).strip() for x in summ if x)
            return f"Summary: {joined}" if joined else ""
        if isinstance(summ, str) and summ.strip():
            return f"Summary: {summ.strip()}"
        return ""
    return ""  # recording.completed carries a media URL only — skipped for v1


def _handle_call(etype: str, obj: dict) -> dict:
    # Enrichment events append to the existing call row; they never create one.
    if etype != "call.completed":
        call_id = (obj.get("callId") or obj.get("id") or "").strip()
        text = _call_enrichment(etype, obj)
        if call_id and text:
            row = db.one("SELECT id, body FROM messages "
                         "WHERE provider_msg_id=? AND channel='call'", (call_id,))
            if row:
                db.run("UPDATE messages SET body=? WHERE id=?",
                       (f"{row['body']}\n{text}", row["id"]))
                return {"ok": True, "enriched": etype}
        return {"ok": True, "ignored": etype}

    raw_dir = (obj.get("direction") or "").lower()
    direction = "out" if raw_dir in ("outgoing", "outbound", "out") else "in"
    # Other party: the caller for inbound, the callee for outbound. `to` may be a
    # string or a list depending on payload shape.
    to = obj.get("to")
    to_one = (to[0] if isinstance(to, list) and to
              else to if isinstance(to, str) else "")
    other = ((obj.get("from") if direction == "in" else to_one) or "").strip()
    if not other:
        return {"ok": True, "ignored": "no call number"}

    status = (obj.get("status") or "").lower()
    body = _call_summary(direction, status, obj.get("duration"))
    call_id = (obj.get("id") or "").strip() or None

    inquiry_id = _ensure_inquiry(other, body, "call")
    try:
        db.run("""INSERT INTO messages (inquiry_id, direction, channel, body, provider_msg_id)
                  VALUES (?, ?, 'call', ?, ?)""",
               (inquiry_id, direction, body, call_id))
    except db.sqlite3.IntegrityError:
        return {"ok": True, "duplicate": True}  # Quo retries — idempotent by call id
    log.info("call recorded on inquiry %s (%s, %s)", inquiry_id, direction, status or "?")
    return {"ok": True}


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

    # Quo nests the message/call under data.object; tolerate a flatter shape.
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or event.get("data") or {}

    if etype.startswith("call."):
        return _handle_call(etype, obj)

    # Only inbound text events create a thread row. Anything else (delivery
    # receipts, outbound echoes) is acknowledged and ignored.
    direction = (obj.get("direction") or "").lower()
    if "message" not in etype or direction not in ("incoming", "inbound", "in"):
        return {"ok": True, "ignored": etype or "unknown"}

    from_phone = (obj.get("from") or "").strip()
    body = (obj.get("body") or obj.get("content") or obj.get("text") or "").strip()
    provider_msg_id = (obj.get("id") or "").strip() or None
    if not from_phone:
        return {"ok": True, "ignored": "no from number"}

    inquiry_id = _ensure_inquiry(from_phone, body, "sms")
    try:
        db.run("""INSERT INTO messages (inquiry_id, direction, channel, body, provider_msg_id)
                  VALUES (?, 'in', 'sms', ?, ?)""",
               (inquiry_id, body, provider_msg_id))
    except db.sqlite3.IntegrityError:
        return {"ok": True, "duplicate": True}  # Quo retries — idempotent by msg id
    log.info("inbound sms recorded on inquiry %s (%d chars)", inquiry_id, len(body))
    return {"ok": True}
