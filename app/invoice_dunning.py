"""Invoice dunning — escalating client emails for overdue invoices, fired off
the recurring sweeper thread (no cron, no second process), mirroring
booking_reminders / review_requests: polite at due+3, firmer at due+7, final at
due+14. Idempotent per (invoice, stage) via the invoice_dunning tracker; a
stage is stamped only AFTER a successful send, so an SMTP hiccup leaves it
unstamped and the next sweep retries (ops/AT-LEAST-ONCE.md — the duplicate is
the acceptable failure, the silent drop is not).

STRICTLY notification-only. No late fees, no auto-charge, no status changes:
the ONLY table this module writes is invoice_dunning. Payments state stays
single-writer through the Stripe webhook, and invoice sends stay Kevin's —
dunning only chases invoices he already sent (status sent/viewed/deposit_paid;
a draft was never in front of the client and 'paid' owes nothing). "Overdue"
is the board's own judgement — due_date past on the studio wall clock
(studio._today(), the one financial clock) — so the client is never chased on
a day the dashboard doesn't call late.

Stages are WINDOWED like booking_reminders, not queued: only the most
escalated open stage fires, so an invoice already 20 days overdue when the
feature is armed gets one final notice, never a three-email barrage. Each
stamp records the due_date it chased — extending an invoice's due date re-arms
the ladder for the new date without any admin-side reset hook.

Dormant unless MISE_INVOICE_DUNNING is set (OFF by default): a deploy never
starts emailing clients about money until the owner arms it. No emails_log
row on purpose — doc_kind='invoice' rows are reserved for the manual
"Email to client" send (common.doc_emails_on_record leans on that), and the
invoice_dunning row IS this send's record.
"""

import datetime as dt
import logging

from . import config, db, features, mailer
from .admin import studio

log = logging.getLogger("mise.invoice_dunning")

# (stage, days overdue when its window opens). Each window closes when the
# next opens; the last has no close — it is the final notice, one-shot.
_STAGES = (("due3", 3), ("due7", 7), ("due14", 14))


def _stage_for(days_overdue: int) -> str | None:
    """The most escalated stage whose window has opened, or None if too fresh."""
    stage = None
    for name, opens in _STAGES:
        if days_overdue >= opens:
            stage = name
    return stage


def _owed_cents(inv) -> int:
    """Outstanding balance for display — the same rule as the board's AR figure
    (common.open_invoice_balance): after a deposit, what's left; otherwise the
    total. Display-only; the pay page (/i/{slug}) stays the authority on the
    exact next charge."""
    if inv["status"] == "deposit_paid":
        return (inv["total_cents"] or 0) - (inv["deposit_cents"] or 0)
    return inv["total_cents"] or 0


def _due(today: dt.date) -> list[tuple["db.sqlite3.Row", str]]:
    """(invoice_row, stage) pairs for owed, overdue invoices whose most
    escalated open stage is unsent for their current due date. Eligibility is
    the board's own overdue predicate (home_context._ctx_headline_counts),
    plus a reachable client email."""
    rows = db.all_(
        """SELECT i.id, i.slug, i.title, i.due_date, i.status,
                  i.total_cents, i.deposit_cents,
                  c.name AS client_name, c.email AS client_email
           FROM invoices i JOIN projects p ON p.id=i.project_id
                           JOIN clients c ON c.id=p.client_id
           WHERE i.status IN ('sent','viewed','deposit_paid')
             AND i.due_date IS NOT NULL AND i.due_date < ?
             AND c.email IS NOT NULL AND c.email <> ''""",
        (today.isoformat(),),
    )
    out = []
    for inv in rows:
        try:
            due = dt.date.fromisoformat(inv["due_date"][:10])
        except (ValueError, TypeError):
            continue
        stage = _stage_for((today - due).days)
        if stage is None:
            continue
        stamped = db.one(
            "SELECT 1 AS x FROM invoice_dunning WHERE invoice_id=? AND stage=? AND due_date=?",
            (inv["id"], stage, inv["due_date"]),
        )
        if stamped is None:
            out.append((inv, stage))
    return out


def _send(inv, stage: str) -> None:
    url = f"{config.BASE_URL}/i/{inv['slug']}"
    owed = f"${_owed_cents(inv) / 100:,.2f}"
    due = inv["due_date"][:10]
    if stage == "due3":
        subject = f'A gentle reminder — invoice "{inv["title"]}"'
        lead = (
            f'Just a gentle reminder — the invoice for "{inv["title"]}" was due '
            f"on {due}, and {owed} is still outstanding. If you've already sent "
            f"payment, please disregard this note (it can take a moment to land)."
        )
    elif stage == "due7":
        subject = f'Following up — invoice "{inv["title"]}" is past due'
        lead = (
            f'Following up on my earlier note — the invoice for "{inv["title"]}" '
            f"was due on {due} and is now more than a week past due, with {owed} "
            f"outstanding. I'd really appreciate it if you could settle it soon."
        )
    else:
        subject = f'Final reminder — invoice "{inv["title"]}" is two weeks past due'
        lead = (
            f'This is a final reminder — the invoice for "{inv["title"]}" was due '
            f"on {due} and is now more than two weeks past due, with {owed} "
            f"outstanding. Please settle it at your earliest convenience."
        )
    body = (
        f"Hi {inv['client_name']},\n\n"
        f"{lead}\n\n"
        f"You can view and pay it securely here:\n\n"
        f"  {url}\n\n"
        f"And if anything about this invoice isn't right, or you'd like to talk "
        f"through timing, just reply to this email — I'd rather sort it out "
        f"together.\n\n"
        f"Thank you,\n{config.SITE_NAME}\n"
    )
    mailer.send(inv["client_email"], subject, body, reply_to=config.GMAIL_USER)


def sweep() -> None:
    """Send due dunning notices. Best-effort per invoice — a send hiccup leaves
    the stage unstamped so the next sweep retries; never blocks the loop."""
    if not features.invoice_dunning_enabled() or not mailer.configured():
        return
    for inv, stage in _due(studio._today()):
        try:
            _send(inv, stage)
            db.run(
                "INSERT INTO invoice_dunning (invoice_id, stage, due_date) VALUES (?,?,?)",
                (inv["id"], stage, inv["due_date"]),
            )
            log.info("invoice %s dunning %s sent", inv["id"], stage)
        except Exception as e:
            log.error("invoice %s dunning %s failed: %s", inv["id"], stage, e)
