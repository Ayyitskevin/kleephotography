"""Post-shoot owner nudge — when a confirmed booking's end time has just passed,
arm a single deferred "pull, cull, and back up the cards" reminder via Hermes
(hermes_arm), fired off the same recurring sweep as the other reminders.

This fills a real gap: nothing in Mise or Odysseus reminds Kevin to offload and
back up a freshly-shot card set — balance_chaser only acts on money, and the
pre-shoot nudges stop at the shoot. It's owner-facing (Telegram via Hermes), never
the client.

One-shot per booking via the armed_postshoot flag, set only after Hermes accepts
the arm — a down Hermes leaves it 0 so the next sweep retries (and Hermes dedups by
key as a second guard). The detection window is bounded (_LOOKBACK_HOURS): on first
deploy only shoots that ended very recently arm, so the back-filled flag default
can't flood Kevin with nudges for years of past bookings. A reschedule makes a fresh
booking row with the flag at its default, so the new shoot date arms on its own.
"""

import datetime as dt
import logging

from . import config, db, hermes_arm

log = logging.getLogger("mise.postshoot")

_UTC = dt.timezone.utc
# Only shoots that ended within this window are eligible. Wide enough to survive a
# few missed sweeps / a short restart, narrow enough that the back-filled flag
# default never reaches back into old bookings on first deploy.
_LOOKBACK_HOURS = 12


def _due(now: dt.datetime):
    floor = (now - dt.timedelta(hours=_LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    return db.all_("""SELECT b.id, b.name, e.name AS event_name
                      FROM bookings b JOIN event_types e ON e.id=b.event_type_id
                      WHERE b.status='confirmed' AND b.armed_postshoot=0
                        AND b.end_utc <= ? AND b.end_utc > ?
                      ORDER BY b.end_utc ASC""", (now_s, floor))


def sweep() -> None:
    """Arm one post-shoot reminder per just-finished booking. Best-effort per row —
    a Hermes hiccup leaves the flag unset so the next sweep retries; never blocks
    the loop. No-ops entirely when the Hermes reminder net isn't configured."""
    if not hermes_arm.is_enabled():
        return
    now = dt.datetime.now(tz=_UTC)
    for b in _due(now):
        ok = hermes_arm.arm(
            key=f"postshoot:{b['id']}",
            text=(f"Shoot wrapped for {b['name']} ({b['event_name']}) — pull, cull, "
                  f"and back up the cards before anything else lands on them."),
            when=hermes_arm.at_9am(config.POSTSHOOT_CULL_DAYS))
        if ok:
            db.run("UPDATE bookings SET armed_postshoot=1 WHERE id=?", (b["id"],))
            log.info("booking %s post-shoot reminder armed", b["id"])
