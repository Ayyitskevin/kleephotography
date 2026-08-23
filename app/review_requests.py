"""Post-delivery Google review asks — the top lead lever working photographers
name (50+ reviews is the competitive baseline), automated the way the research
says to: hang the ask on the delivery event, make it warm, and route the
unhappy to a private reply instead of the public form.

Mirrors gallery_reminders: fired from the recurring sweeper thread, idempotent
per gallery (review_requested_at IS NULL = unsent), a send failure leaves the
stamp unset so the next sweep retries, and nothing here can block the loop.

Two throttles, because they protect different people:
  - per GALLERY, one-shot — the same delivery never asks twice;
  - per CLIENT, a cooldown (MISE_REVIEW_COOLDOWN_DAYS) across ALL their
    galleries — a family with three sessions this year is one relationship,
    not three ask opportunities. Review fatigue turns a happy client into an
    annoyed one, which is strictly worse than no ask.

Dormant unless MISE_GOOGLE_REVIEW_URL is set (the direct "write a review" link
from the Google Business Profile share button). Expired galleries are never
asked: "review the photos you can no longer open" is an embarrassment, not an
ask.
"""

import datetime as dt
import logging

from . import config, db, features, mailer

log = logging.getLogger("mise.review_requests")


def _due(today: dt.date) -> list["db.sqlite3.Row"]:
    """Published, unexpired galleries whose ask is ripe and whose client is
    reachable and outside the cooldown.

    Ripeness anchors on created_at (the house convention — see the proofing
    nudge): MISE_REVIEW_ASK_DAYS after the gallery went up, long enough that
    they've actually seen their photos, soon enough that the delight is fresh.
    """
    ripe = (today - dt.timedelta(days=config.REVIEW_ASK_DAYS)).isoformat()
    cooldown = (today - dt.timedelta(days=config.REVIEW_COOLDOWN_DAYS)).isoformat()
    return db.all_(
        """SELECT g.id, g.slug, g.title, g.client_id,
                  c.name AS client_name, c.email AS client_email
           FROM galleries g JOIN clients c ON c.id = g.client_id
           WHERE g.type='gallery' AND g.published=1
             AND g.review_requested_at IS NULL
             AND c.email IS NOT NULL AND c.email <> ''
             AND substr(g.created_at,1,10) <= ?
             AND (g.expires_at IS NULL OR g.expires_at >= ?)
             AND NOT EXISTS (
               SELECT 1 FROM galleries g2
               WHERE g2.client_id = g.client_id
                 AND g2.review_requested_at IS NOT NULL
                 AND substr(g2.review_requested_at,1,10) > ?)""",
        (ripe, today.isoformat(), cooldown),
    )


def _send(g) -> None:
    body = (
        f"Hi {g['client_name']},\n\n"
        f'I hope you\'re loving the photos from "{g["title"]}" — it was a real '
        f"pleasure working with you.\n\n"
        f"If you have two minutes, a short Google review makes an enormous "
        f"difference for a small studio like mine. It's the first thing people "
        f"see when they're deciding who to trust with their own photos:\n\n"
        f"  {config.GOOGLE_REVIEW_URL}\n\n"
        f"And if anything about your gallery or the experience wasn't right, "
        f"please just reply to this email instead — I'd genuinely rather hear "
        f"it from you directly and fix it.\n\n"
        f"Thank you again,\n{config.SITE_NAME}\n"
    )
    mailer.send(
        g["client_email"],
        f"Two minutes to help a small studio? — {config.SITE_NAME}",
        body,
        reply_to=config.GMAIL_USER,
    )


def sweep() -> None:
    """Send due review asks. Best-effort per gallery; never blocks the loop."""
    if not features.review_engine_enabled() or not mailer.configured():
        return
    for g in _due(dt.date.today()):
        try:
            _send(g)
            db.run(
                "UPDATE galleries SET review_requested_at=datetime('now') WHERE id=?",
                (g["id"],),
            )
            log.info("review ask sent for gallery %s (client %s)", g["id"], g["client_id"])
        except Exception as e:
            log.error("review ask for gallery %s failed: %s", g["id"], e)
