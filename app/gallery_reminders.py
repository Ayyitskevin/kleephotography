"""Gallery client reminders — two one-shot client nudges fired off the recurring
sweeper thread (no cron, no second process), mirroring booking_reminders:

  - EXPIRY: as a published gallery nears its expires_at, email the client a
    "download before it's gone" reminder (config.GALLERY_EXPIRY_REMINDER_DAYS out).
  - PROOFING: when a published gallery still has unmet section proof_targets and
    has been waiting config.GALLERY_PROOF_NUDGE_DAYS days, nudge the client to
    finish picking favorites.

Each is idempotent per gallery via the reminded_expiry / reminded_proofing flags,
so the sweep can run as often as it likes and a gallery gets at most one of each.
Only galleries linked to a client WITH an email are eligible — a gallery carrying
just a free-text client_name has no address to reach. Email only (no SMS), and
Kevin isn't re-pinged; these go to the client. A send failure leaves the flag
unset so the next sweep retries, and never blocks the recurring loop.

reminded_expiry is reset to 0 when the gallery's expiry date changes (see
admin.galleries.update_gallery), so an extended gallery re-reminds near its new date.
"""

import datetime as dt
import logging

from . import config, db, mailer

log = logging.getLogger("mise.gallery_reminders")


def _days_phrase(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _due_expiry(today: dt.date) -> list[tuple["db.sqlite3.Row", int]]:
    """(gallery_row, days_until) for published galleries whose expiry reminder is
    now due and unsent — within the lead window and not yet past expiry."""
    rows = db.all_(
        """SELECT g.id, g.slug, g.title, g.expires_at,
                  c.name AS client_name, c.email AS client_email
           FROM galleries g JOIN clients c ON c.id = g.client_id
           WHERE g.type='gallery' AND g.published=1
             AND g.reminded_expiry=0 AND g.expires_at IS NOT NULL
             AND c.email IS NOT NULL AND c.email <> ''""")
    out = []
    for g in rows:
        try:
            exp = dt.date.fromisoformat(g["expires_at"][:10])
        except (ValueError, TypeError):
            continue
        days = (exp - today).days
        if 0 <= days <= config.GALLERY_EXPIRY_REMINDER_DAYS:
            out.append((g, days))
    return out


def _due_proofing(today: dt.date) -> list["db.sqlite3.Row"]:
    """Published galleries with at least one unmet section proof_target that have
    been waiting (by created_at) at least the nudge threshold, reminder unsent."""
    cutoff = (today - dt.timedelta(days=config.GALLERY_PROOF_NUDGE_DAYS)).isoformat()
    return db.all_(
        """SELECT g.id, g.slug, g.title,
                  c.name AS client_name, c.email AS client_email
           FROM galleries g JOIN clients c ON c.id = g.client_id
           WHERE g.type='gallery' AND g.published=1
             AND g.reminded_proofing=0
             AND c.email IS NOT NULL AND c.email <> ''
             AND substr(g.created_at,1,10) <= ?
             AND EXISTS (
               SELECT 1 FROM sections s
               WHERE s.gallery_id=g.id
                 AND s.proof_target IS NOT NULL AND s.proof_target > 0
                 AND (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                      JOIN assets a ON a.id=f.asset_id
                      WHERE a.section_id=s.id) < s.proof_target)""",
        (cutoff,))


def _send_expiry(g, days: int) -> None:
    url = f"{config.BASE_URL}/g/{g['slug']}"
    when = _days_phrase(days)
    body = (f"Hi {g['client_name']},\n\n"
            f"A quick heads-up — your gallery \"{g['title']}\" comes down {when}. "
            f"Make sure you've saved everything you'd like to keep before then:\n\n"
            f"  {url}\n\n"
            f"— {config.SITE_NAME}\n")
    mailer.send(g["client_email"], f"Your gallery \"{g['title']}\" expires {when}",
                body, reply_to=config.GMAIL_USER)


def _send_proofing(g) -> None:
    url = f"{config.BASE_URL}/g/{g['slug']}"
    body = (f"Hi {g['client_name']},\n\n"
            f"Just a nudge — your gallery \"{g['title']}\" is ready and a few sections "
            f"still need your picks. Tap the heart on the ones you'd like, here:\n\n"
            f"  {url}\n\n"
            f"— {config.SITE_NAME}\n")
    mailer.send(g["client_email"], f"A reminder to pick your favorites — \"{g['title']}\"",
                body, reply_to=config.GMAIL_USER)


def sweep() -> None:
    """Send any due gallery reminders. Best-effort per gallery — a mail hiccup
    leaves the flag unset so the next sweep retries; it never blocks the loop."""
    if not mailer.configured():
        return
    today = dt.date.today()
    for g, days in _due_expiry(today):
        try:
            _send_expiry(g, days)
            db.run("UPDATE galleries SET reminded_expiry=1 WHERE id=?", (g["id"],))
            log.info("gallery %s expiry reminder sent (%s days out)", g["id"], days)
        except Exception as e:
            log.error("gallery %s expiry reminder failed: %s", g["id"], e)
    for g in _due_proofing(today):
        try:
            _send_proofing(g)
            db.run("UPDATE galleries SET reminded_proofing=1 WHERE id=?", (g["id"],))
            log.info("gallery %s proofing nudge sent", g["id"])
        except Exception as e:
            log.error("gallery %s proofing nudge failed: %s", g["id"], e)
