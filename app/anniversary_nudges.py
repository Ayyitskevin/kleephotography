"""Session-anniversary nudges — the lifecycle play the six-figure pros describe:
a wedding client is one booking, a family or portrait client can book yearly for
a decade, but only if someone remembers to invite them back.

This nudges KEVIN, never the client. An automated "book me again!" email is
exactly the wrong register for a relationship business — the machine's job is
remembering, the invitation is his. Fired off the recurring sweep, mirroring
contract_reminders: Telegram-only, one-shot per cycle, the stamp is never
written while alerts are disabled (so enabling Telegram later still surfaces
clients already in the window), and a send failure leaves the stamp unset so
the next sweep retries.

Who is due: clients whose LATEST closed project wrapped roughly a year ago
(inside a bounded window — see _due), who have not already come back on their
own (no newer project in any stage, no upcoming confirmed booking), and who
have not been nudged this cycle. The window's upper bound doubles as the
first-enable flood guard: a client closed three years ago is a reactivation
case, not an anniversary, and enabling this feature must not ping Kevin about
every client the studio has ever had.

Type-agnostic on purpose: for THIS studio a year is a natural re-shoot rhythm
across the board — families grow, menus change, listings turn over, teams need
fresh headshots. The nudge costs Kevin one Telegram line to ignore; a type
filter that guessed wrong would silently cost him the rebooking.
"""

import logging

from . import alerts, config, db

log = logging.getLogger("mise.anniversary_nudges")

# The nudge window is [NUDGE_DAYS, NUDGE_DAYS + _WINDOW_DAYS). Four months wide:
# generous enough that a long alerts outage cannot silently skip a cohort,
# bounded so old history never floods a fresh enablement.
_WINDOW_DAYS = 120
# A client re-nudges only when the previous stamp is older than this — one
# invitation per cycle, forever, without ever going quiet for good.
_RENUDGE_DAYS = 300


def _due() -> list["db.sqlite3.Row"]:
    lo = f"-{config.ANNIVERSARY_NUDGE_DAYS + _WINDOW_DAYS} days"
    hi = f"-{config.ANNIVERSARY_NUDGE_DAYS} days"
    return db.all_(
        f"""SELECT c.id, c.name, c.company, p.title AS last_title,
                   CAST(julianday('now') - julianday(p.stage_changed_at) AS INTEGER) AS age_d
            FROM clients c
            JOIN projects p ON p.id = (
              SELECT id FROM projects
              WHERE client_id = c.id AND status = 'project_closed'
              ORDER BY stage_changed_at DESC LIMIT 1)
            WHERE p.stage_changed_at >= datetime('now', ?)
              AND p.stage_changed_at <= datetime('now', ?)
              AND (c.anniversary_nudged_at IS NULL
                   OR c.anniversary_nudged_at <= datetime('now', '-{_RENUDGE_DAYS} days'))
              AND NOT EXISTS (
                SELECT 1 FROM projects p2
                WHERE p2.client_id = c.id
                  AND p2.created_at > p.stage_changed_at)
              AND NOT EXISTS (
                SELECT 1 FROM bookings b
                WHERE b.client_id = c.id AND b.status = 'confirmed'
                  AND b.start_utc >= datetime('now'))
            ORDER BY p.stage_changed_at ASC""",
        (lo, hi),
    )


def sweep() -> None:
    """One Telegram line per due client. Best-effort per row; never blocks the loop."""
    if not alerts.is_enabled():
        return
    for c in _due():
        who = c["name"] + (f" ({c['company']})" if c["company"] else "")
        months = max(1, round(c["age_d"] / 30))
        try:
            alerts.notify(
                f'Anniversary — it\'s been ~{months} months since "{c["last_title"]}" '
                f"wrapped for {who}. A good week to invite them back for this year's "
                f"session: {config.BASE_URL}/admin/studio/clients/{c['id']}"
            )
            db.run(
                "UPDATE clients SET anniversary_nudged_at=datetime('now') WHERE id=?",
                (c["id"],),
            )
            log.info("anniversary nudge sent for client %s (%sd since close)", c["id"], c["age_d"])
        except Exception as e:
            log.error("anniversary nudge for client %s failed: %s", c["id"], e)
