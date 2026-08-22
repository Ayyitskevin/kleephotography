"""Owner notification when a client accepts or declines a proposal.

app/public/docs.py commits the status UPDATE first, then stages this through
the job queue (at-least-once — ops/AT-LEAST-ONCE.md), so a slow or down
channel can never fail the client's accept/decline action: the enqueue is
wrapped and never raises, and delivery runs on a queue worker after the
response has gone. Both channels are internal nudges to Kevin — never a
client-facing send — riding the existing dormant patterns:

- Telegram via alerts.notify(): no-ops unless MISE_TELEGRAM_TOKEN +
  MISE_TELEGRAM_CHAT_ID are set, and never raises (fire-and-forget thread).
- Owner email via mailer.send() to config.GMAIL_USER: skipped with a log line
  when the mailer is not configured. Unlike inquiry_notify, an unconfigured
  mailer is NOT a job failure here — the decision is already on the board and
  the accept/decline is durable, so a dormant channel staying dormant is not
  an error worth parking a retry over.

A CONFIGURED mailer that fails to send does raise, so the queue parks and
retries (fail loud, not lost). The Telegram line fires before the email
attempt and carries no send-stamp, so a retry after an SMTP failure can
repeat it — per ops/AT-LEAST-ONCE.md the duplicate is the acceptable
failure, and a claim lock like inquiry_notify's is not worth buying for an
internal nudge. Caller-side dedup for alerts.notify(): accept and decline are
one-shot (the route's status gate refuses a second decision), so exactly one
job is ever staged per decision.
"""

import logging

from . import alerts, config, db, jobs, mailer

log = logging.getLogger("mise.proposal_notify")

DECISIONS = ("accepted", "declined")


def enqueue_decision(proposal_id: int, decision: str) -> int | None:
    """Stage the owner notification for a committed accept/decline.

    NEVER raises: the client's decision is already committed, so an enqueue
    failure costs a log line, never a 500 on the client's response. One honest
    window remains: a process death between the decision's commit and this
    INSERT loses the nudge with no log line at all (the board still shows the
    decision). That matches the inquiry-notify precedent; money paths close
    the same window with in-transaction jobs.stage() instead — see pay.py."""
    try:
        return jobs.enqueue(
            "proposal_decision_notify", {"proposal_id": proposal_id, "decision": decision}
        )
    except Exception:
        log.exception(
            "proposal %s %s notify enqueue failed (decision is committed and stands)",
            proposal_id,
            decision,
        )
        return None


def _load(proposal_id: int) -> "db.sqlite3.Row":
    return db.one(
        """SELECT pr.id, pr.title, pr.total_cents,
                  p.title AS project_title, c.name AS client_name, c.company,
                  c.email AS client_email
             FROM proposals pr
             JOIN projects p ON p.id=pr.project_id
             JOIN clients c ON c.id=p.client_id
            WHERE pr.id=?""",
        (proposal_id,),
    )


def deliver_decision(proposal_id: int, decision: str) -> None:
    """Job handler: Telegram line + owner email. Cheap to repeat (module doc)."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown proposal decision {decision!r}")
    d = _load(proposal_id)
    if not d:
        log.warning("proposal %s vanished before %s notify — nothing sent", proposal_id, decision)
        return
    who = d["company"] or d["client_name"]
    total = f"${(d['total_cents'] or 0) / 100:,.2f}"
    url = f"{config.BASE_URL}/admin/studio/proposals/{proposal_id}"
    alerts.notify(f"Proposal {decision} — {d['title']} · {who} ({total}). {url}")
    if not mailer.configured():
        log.info(
            "proposal %s %s: mailer not configured — owner email skipped", proposal_id, decision
        )
        return
    body = (
        f"{who} {decision} your proposal.\n\n"
        f"Proposal: {d['title']}\n"
        f"Project: {d['project_title']}\n"
        f"Total: {total}\n\n"
        f"Open it in Mise: {url}\n"
    )
    mailer.send(
        config.GMAIL_USER,
        f"Proposal {decision} — {d['title']} · {who}",
        body,
        reply_to=d["client_email"] or "",
    )
    log.info("proposal %s %s owner email sent", proposal_id, decision)
