"""Two-way SMS via Quo (formerly OpenPhone) — provider-agnostic adapter.

The Inbox treats SMS exactly like email: a thread of messages hanging off an
inquiry. This module is the ONLY place that knows Quo's wire format, so swapping
providers later (Twilio, etc.) means rewriting this file and nothing else.

Ships INERT: with no Quo keys in .env, configured() is false — outbound send is a
no-op-by-refusal (raises SmsError, the route greys the SMS toggle) and the inbound
/webhooks/quo route returns 503. Email keeps flowing through mailer.py unchanged.

IMPORTANT — verify against live Quo docs before arming. Quo rebranded from
OpenPhone in late 2025; the send endpoint, auth header, and webhook-signature
scheme below follow OpenPhone's public v1 API. When Kevin provisions real keys,
confirm each against Quo's current docs and adjust ONLY this file. verify_webhook
fails CLOSED, so a scheme mismatch rejects inbound (safe) rather than trusting it.
"""

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("mise.sms")


class SmsError(Exception):
    """Any reason a text could not be sent. Message is safe to surface in admin
    (no secrets, no stack)."""


def configured() -> bool:
    """Armed only when an API key AND a from-number are set. Either unset -> the
    Inbox's SMS channel stays cleanly dormant."""
    return bool(config.QUO_API_KEY and config.QUO_NUMBER)


def send(to: str, body: str) -> str:
    """Send one SMS from the business Quo number to `to` (E.164). Returns the
    provider message id (stored on the messages row for idempotency/audit).

    Raises SmsError on every failure path so the caller writes nothing on failure."""
    if not configured():
        raise SmsError("SMS is not configured")
    to = (to or "").strip()
    body = (body or "").strip()
    if not to:
        raise SmsError("no recipient phone number")
    if not body:
        raise SmsError("message body is empty")
    req = urllib.request.Request(
        f"{config.QUO_API_BASE}/messages", method="POST",
        data=json.dumps({"from": config.QUO_NUMBER, "to": [to],
                         "content": body}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": config.QUO_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=config.QUO_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SmsError(f"Quo returned HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SmsError(f"Quo unreachable: {e.reason if hasattr(e, 'reason') else e}")
    except (ValueError, json.JSONDecodeError):
        raise SmsError("Quo returned an unreadable response")
    # OpenPhone/Quo nests the created message under "data": {"id": ...}; tolerate a
    # flat {"id": ...} too. A missing id is non-fatal — the text went out — so fall
    # back to "" (the messages row simply carries no provider id).
    msg = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    msg_id = (msg.get("id") or "").strip() if isinstance(msg, dict) else ""
    log.info("sms sent via Quo to %s (%d chars, id=%s)", to, len(body), msg_id or "?")
    return msg_id


def verify_webhook(raw: bytes, signature_header: str) -> bool:
    """Verify an inbound Quo webhook HMAC. Fails CLOSED (returns False) on any
    malformed/absent header or secret — never trust an unverifiable payload.

    Scheme (OpenPhone v1): header `openphone-signature: hmac;1;<ts>;<base64 sig>`,
    where sig = HMAC-SHA256(key=base64-decode(signing secret), msg="<ts>.<rawbody>")
    base64-encoded. CONFIRM against Quo's current docs before arming."""
    secret = config.QUO_WEBHOOK_SECRET
    if not secret or not signature_header:
        return False
    parts = signature_header.split(";")
    if len(parts) != 4 or parts[0] != "hmac":
        return False
    _, _version, timestamp, provided = parts
    try:
        key = base64.b64decode(secret)
    except (ValueError, TypeError):
        return False
    signed = timestamp.encode() + b"." + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, provided)
