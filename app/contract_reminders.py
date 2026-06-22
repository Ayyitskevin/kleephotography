"""Contract follow-up nudge — an internal heads-up to Kevin (Telegram, never the
client) when a contract has been sent and is still unsigned after
config.CONTRACT_NUDGE_DAYS. Fired off the same recurring sweep as the other
reminders.

This fills a real gap: the admin "Needs you today" list surfaces unanswered
inquiries and unfollowed proposals, but a sent-and-unsigned CONTRACT only sits
passively in "documents in flight" — nothing pushes it. Odysseus can't see it
either (contract signature state lives only in Mise). So this is the one place
that proactively flags a stalled signature.

One-shot per contract via the nudged_unsigned flag: a contract only moves forward
(draft -> sent -> viewed -> signed) with no re-send path, so once nudged it never
re-nudges, and once signed the status filter drops it. The whole sweep no-ops
unless Telegram is configured, and it never sets the flag when disabled — so
enabling alerts later still catches contracts that are already overdue.
"""

import logging

from . import alerts, config, db

log = logging.getLogger("mise.contract_reminders")


def _due() -> list["db.sqlite3.Row"]:
    """Sent-or-viewed contracts past the nudge age whose nudge hasn't fired yet."""
    return db.all_(
        f"""SELECT ct.id, ct.title,
                   c.name AS client_name, c.company,
                   CAST(julianday('now') - julianday(ct.sent_at) AS INTEGER) AS age_d
            FROM contracts ct
            JOIN projects p ON p.id = ct.project_id
            JOIN clients c ON c.id = p.client_id
            WHERE ct.status IN ('sent','viewed')
              AND ct.nudged_unsigned = 0
              AND ct.sent_at IS NOT NULL
              AND ct.sent_at <= datetime('now', '-{int(config.CONTRACT_NUDGE_DAYS)} days')
            ORDER BY ct.sent_at ASC""")


def sweep() -> None:
    """Nudge once per overdue unsigned contract. Best-effort per row — a send
    failure leaves the flag unset so the next sweep retries; never blocks the loop."""
    if not alerts.is_enabled():
        return
    for ct in _due():
        who = ct["company"] or ct["client_name"]
        try:
            alerts.notify(
                f"Contract still unsigned — {ct['title']} · {who} "
                f"(sent {ct['age_d']}d ago). {config.BASE_URL}"
                f"/admin/studio/contracts/{ct['id']}")
            db.run("UPDATE contracts SET nudged_unsigned=1 WHERE id=?", (ct["id"],))
            log.info("contract %s unsigned nudge sent (%sd old)", ct["id"], ct["age_d"])
        except Exception as e:
            log.error("contract %s unsigned nudge failed: %s", ct["id"], e)
