"""Booking reminders — a T-48h and T-24h email nudge to the client, plus a T-24h
SMS when the booking carries a phone number and Quo is armed, fired off the
recurring sweeper thread (no cron, no second process). Idempotent per booking via
the reminded_48h / reminded_24h / reminded_sms flags, so the sweep can run as often
as it likes and each booking gets at most one of each, and never one after the
start time.

Windows are chosen so a late booking can't trigger a stale nudge: the 48h reminder
only fires while the start is 24-48h out, the 24h reminder while it's 0-24h out. A
booking made 10h ahead therefore gets just the single 24h nudge. A reschedule makes
a fresh booking row (flags reset), so the new time is reminded and the cancelled row
is skipped. Each channel stamps only its own flag and only after a successful send —
a booking reminded by email while SMS was dormant still gets the text once Quo is
armed, as long as its window is open. Kevin isn't re-pinged — it's already on his
calendar."""

import datetime as dt
import logging

from . import config, db, mailer, sms
from .booking_notify import _manage_url, _when

log = logging.getLogger("mise.reminders")
_UTC = dt.UTC
_REMINDER_COLS = frozenset({"reminded_48h", "reminded_24h"})


def _due(now: dt.datetime):
    """(booking_row, kind) pairs for confirmed bookings whose matching reminder is
    now due and unsent. kind is '48h' or '24h' (email) or 'sms' (T-24h text)."""
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    horizon = (now + dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.all_(
        """SELECT b.*, e.name AS event_name, e.location
                      FROM bookings b JOIN event_types e ON e.id=b.event_type_id
                      WHERE b.status='confirmed'
                        AND b.start_utc > ? AND b.start_utc <= ?""",
        (now_s, horizon),
    )
    out = []
    for b in rows:
        start = dt.datetime.strptime(b["start_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
        hrs = (start - now).total_seconds() / 3600
        if 24 < hrs <= 48 and not b["reminded_48h"]:
            out.append((b, "48h"))
        elif 0 < hrs <= 24 and not b["reminded_24h"]:
            out.append((b, "24h"))
        # SMS is its own channel, not part of the email chain: a booking whose
        # 24h email already went out may still owe the text (Quo armed later,
        # or the last send failed). No phone -> never due, never stamped.
        if 0 < hrs <= 24 and not b["reminded_sms"] and (b["phone"] or "").strip():
            out.append((b, "sms"))
    return out


def sweep() -> None:
    """Send any due reminders. Best-effort per booking — a send hiccup leaves the
    flag unset so the next sweep retries; it never blocks the recurring loop."""
    email_on = mailer.configured()
    sms_on = sms.configured()
    if not (email_on or sms_on):
        return
    now = dt.datetime.now(tz=_UTC)
    for b, kind in _due(now):
        if kind == "sms":
            # Leave the flag unset while Quo is dormant: arming SMS later must
            # still catch every booking whose window is open.
            if not sms_on:
                continue
            text = (
                f"{config.SITE_NAME} reminder: {b['event_name']} tomorrow — "
                f"{_when(b['start_utc'], b['tz'])}. "
                f"Change or cancel: {_manage_url(b['token'])}"
            )
            try:
                sms.send(b["phone"], text)
                db.run("UPDATE bookings SET reminded_sms=1 WHERE id=?", (b["id"],))
                log.info("booking %s sms reminder sent", b["id"])
            except Exception:
                log.exception("booking %s sms reminder failed", b["id"])
            continue
        if not email_on:
            continue
        col = "reminded_48h" if kind == "48h" else "reminded_24h"
        lead = "in about two days" if kind == "48h" else "tomorrow"
        cli_when = _when(b["start_utc"], b["tz"])
        loc = b["location"] or "Details to follow"
        body = (
            f"Hi {b['name']},\n\n"
            f"A quick reminder — your booking is {lead}:\n\n"
            f"  {b['event_name']}\n  {cli_when}\n  {loc}\n\n"
            f"Need to change or cancel? {_manage_url(b['token'])}\n\n"
            f"— {config.SITE_NAME}\n"
        )
        try:
            mailer.send(
                b["email"], f"Reminder — {b['event_name']} {lead}", body, reply_to=config.GMAIL_USER
            )
            db.run(f"UPDATE bookings SET {db.ident(col, _REMINDER_COLS)}=1 WHERE id=?", (b["id"],))
            log.info("booking %s %s reminder sent", b["id"], kind)
        except Exception:
            log.exception("booking %s %s reminder failed", b["id"], kind)
