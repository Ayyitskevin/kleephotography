"""Idempotent owner-notification delivery for public inquiries.

The public form stores the inquiry first, then enqueues ``inquiry_owner_email``.
Workers claim send rights with a short in_flight lock so concurrent jobs and
manual retries cannot double-send. Delivery stamps ``owner_email_delivered_at``
(and legacy ``emailed=1``) only after SMTP accepts the message. Failure
categories are privacy-safe codes only — never raw SMTP payloads or visitor PII.
"""

from __future__ import annotations

import logging

from . import alerts, config, db, jobs, mailer

log = logging.getLogger("mise.inquiry_notify")

# Stale in_flight claims (worker crash after claim / before stamp) may be reclaimed.
_IN_FLIGHT_STALE_SECONDS = 300

FAIL_SMTP = "smtp_error"
FAIL_MAILER_OFF = "mailer_not_configured"
FAIL_UNKNOWN = "unknown"


def enqueue_owner_email(inquiry_id: int) -> int | None:
    """Enqueue one owner-email job if the lead still needs delivery.

    Skips when already delivered. Does not inspect the queue for duplicates —
    the handler is idempotent under concurrent execution.
    """
    row = db.one(
        "SELECT owner_email_delivered_at, emailed FROM inquiries WHERE id=?",
        (inquiry_id,),
    )
    if not row:
        return None
    if row["owner_email_delivered_at"] or row["emailed"]:
        return None
    return jobs.enqueue("inquiry_owner_email", {"inquiry_id": inquiry_id})


def _record_failure(inquiry_id: int, category: str) -> None:
    code = category if category in {FAIL_SMTP, FAIL_MAILER_OFF} else FAIL_UNKNOWN
    db.run(
        """UPDATE inquiries
              SET owner_email_status='failed',
                  owner_email_failure_category=?,
                  owner_email_last_attempted_at=datetime('now')
            WHERE id=? AND owner_email_delivered_at IS NULL""",
        (code, inquiry_id),
    )
    alerts.inquiry_owner_email_failed(inquiry_id, code)


def _claim_send(inquiry_id: int) -> bool:
    """Atomically claim the right to send. Returns False if already delivered
    or another worker holds a fresh in_flight claim."""
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT owner_email_delivered_at, owner_email_status,
                      owner_email_last_attempted_at, emailed
                 FROM inquiries WHERE id=?""",
            (inquiry_id,),
        ).fetchone()
        if not row:
            return False
        if row["owner_email_delivered_at"] or row["emailed"]:
            return False
        # Reclaim stale in_flight (crash after claim).
        stale_ok = row["owner_email_status"] != "in_flight"
        if row["owner_email_status"] == "in_flight" and row["owner_email_last_attempted_at"]:
            age = con.execute(
                """SELECT CAST(
                     (julianday('now') - julianday(?)) * 86400 AS INTEGER
                   ) AS sec""",
                (row["owner_email_last_attempted_at"],),
            ).fetchone()
            stale_ok = bool(
                age and age["sec"] is not None and age["sec"] >= _IN_FLIGHT_STALE_SECONDS
            )
        if row["owner_email_status"] == "in_flight" and not stale_ok:
            return False
        cur = con.execute(
            """UPDATE inquiries
                  SET owner_email_status='in_flight',
                      owner_email_attempts=owner_email_attempts + 1,
                      owner_email_last_attempted_at=datetime('now'),
                      owner_email_failure_category=NULL
                WHERE id=? AND owner_email_delivered_at IS NULL
                  AND (emailed IS NULL OR emailed=0)""",
            (inquiry_id,),
        )
        return cur.rowcount == 1


def _stamp_delivered(inquiry_id: int) -> bool:
    with db.tx() as con:
        cur = con.execute(
            """UPDATE inquiries
                  SET owner_email_delivered_at=datetime('now'),
                      owner_email_status='delivered',
                      owner_email_failure_category=NULL,
                      emailed=1
                WHERE id=? AND owner_email_delivered_at IS NULL""",
            (inquiry_id,),
        )
        return cur.rowcount == 1


def deliver_owner_email(inquiry_id: int) -> None:
    """Job handler: claim → send → stamp. Idempotent; fail-loud for retries."""
    row = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not row:
        raise ValueError(f"inquiry {inquiry_id} not found")
    if row["owner_email_delivered_at"] or row["emailed"]:
        log.info("inquiry %s owner email already delivered — skip", inquiry_id)
        return

    if not _claim_send(inquiry_id):
        # Lost claim or already delivered by peer — re-check delivery.
        again = db.one(
            "SELECT owner_email_delivered_at, emailed FROM inquiries WHERE id=?",
            (inquiry_id,),
        )
        if again and (again["owner_email_delivered_at"] or again["emailed"]):
            return
        # Another worker is mid-send; fail soft so the job can retry later.
        raise RuntimeError("owner_email_claim_busy")

    if not mailer.configured():
        _record_failure(inquiry_id, FAIL_MAILER_OFF)
        raise RuntimeError(FAIL_MAILER_OFF)

    body = (
        f"New inquiry via kleephotography.com\n\n"
        f"Name: {row['name']}\nEmail: {row['email']}\n"
        f"Phone: {(row['phone'] or '—')}\n"
        f"Business: {row['business'] or '—'}\n\n{row['message']}\n"
    )
    try:
        mailer.send(
            config.GMAIL_USER,
            f"New inquiry — {row['name']}",
            body,
            reply_to=row["email"],
        )
    except Exception:
        _record_failure(inquiry_id, FAIL_SMTP)
        raise RuntimeError(FAIL_SMTP) from None

    if _stamp_delivered(inquiry_id):
        log.info("inquiry %s owner email delivered", inquiry_id)
    else:
        # Peer stamped first — message may have double-sent only if both passed
        # SMTP before either stamp; claim lock prevents that for concurrent jobs.
        log.info("inquiry %s owner email stamp raced — delivery already recorded", inquiry_id)


def _col(inq, key, default=None):
    try:
        keys = inq.keys()
    except Exception:
        return default
    if key not in keys:
        return default
    val = inq[key]
    return default if val is None else val


def delivery_view(inq) -> dict:
    """Inbox-facing delivery status (no visitor PII beyond existing row display)."""
    delivered_at = _col(inq, "owner_email_delivered_at")
    attempts = int(_col(inq, "owner_email_attempts", 0) or 0)
    last = _col(inq, "owner_email_last_attempted_at")
    category = _col(inq, "owner_email_failure_category")

    if delivered_at or inq["emailed"]:
        return {
            "label": "Owner email",
            "state": "ok",
            "detail": "Delivered to studio inbox."
            if delivered_at
            else "Notification sent (or you replied from Inbox).",
            "retryable": False,
            "attempts": attempts,
            "last_attempted": last,
            "delivered_at": delivered_at,
            "failure_category": None,
        }

    if not mailer.configured() or category == FAIL_MAILER_OFF:
        detail = (
            "Mailer not configured — lead is stored; owner email not sent. "
            "Configure mailer, then retry owner email."
        )
        cat = FAIL_MAILER_OFF
        state = "bad"
    elif category == FAIL_SMTP or attempts:
        detail = (
            "Owner notification not delivered"
            + (f" ({category})" if category else "")
            + (f" · attempts {attempts}" if attempts else "")
            + ". Lead is stored — retry owner email or reply from Inbox."
        )
        cat = category or FAIL_SMTP
        state = "bad"
    else:
        detail = "Owner email pending delivery."
        cat = None
        state = "warn"

    return {
        "label": "Owner email",
        "state": state,
        "detail": detail,
        "retryable": True,
        "attempts": attempts,
        "last_attempted": last,
        "delivered_at": None,
        "failure_category": cat,
    }
