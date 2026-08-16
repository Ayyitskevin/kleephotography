"""List announcements — one message to the people who already know the work.

The owned channel the research kept pointing at: email reaches ~99% of a list
while Instagram shows a post to 5–20% of followers. This is deliberately NOT a
marketing suite — one audience (past clients ∪ gallery-gate captures, deduped,
minus unsubscribes), one plain-text message, one send. Its first real job is
announcing pay-to-book mini-session days.

Deliveries ride the job queue: one durable job per recipient, staged in the
same transaction as the announcement row (the pay.py pattern — the record and
the work commit together), dispatched after commit. Each delivery re-checks
the unsubscribe ledger at send time, throttles itself with a small sleep so a
few hundred sends trickle through Gmail instead of tripping its rate limits,
and carries the CAN-SPAM unsubscribe link in the footer.

Unsubscribe tokens are the signed email itself (no table of tokens to leak or
prune). Signed with a 10-year window rather than the 90-day cookie default —
an unsubscribe link in a client's inbox must work essentially forever.
"""

import logging
import random
import time

from . import config, db, mailer, security

log = logging.getLogger("mise.announcements")

# CAN-SPAM: the link must keep working long after the send. 10 years.
_UNSUB_MAX_AGE = 10 * 365 * 24 * 3600


def unsubscribe_token(email: str) -> str:
    return security.sign(email.strip().lower())


def email_from_token(token: str) -> str | None:
    val = security.unsign(token, max_age=_UNSUB_MAX_AGE)
    # A signed value from another flow (a visitor cookie, an admin session)
    # unsigns to a random token, never to an address — the shape check keeps
    # this route from ever recording garbage in the legal ledger.
    if val and "@" in val and 3 <= len(val) <= 320:
        return val.lower()
    return None


def is_unsubscribed(email: str) -> bool:
    return (
        db.one("SELECT 1 AS x FROM unsubscribes WHERE email=?", (email.strip().lower(),))
        is not None
    )


def record_unsubscribe(email: str) -> None:
    db.run("INSERT OR IGNORE INTO unsubscribes (email) VALUES (?)", (email.strip().lower(),))


def audience() -> list[dict]:
    """Deduped {email, name} across clients and gallery-gate captures.

    Client rows win the name (a real relationship beats a download-gate entry);
    lowercased emails are the dedup key so Client@ and client@ are one person.
    Unsubscribed addresses never appear — filtered here AND re-checked at send.
    """
    rows = db.all_(
        """SELECT LOWER(TRIM(email)) AS email, MAX(name) AS name, MIN(pri) AS pri FROM (
             SELECT email, name, 0 AS pri FROM clients
              WHERE email IS NOT NULL AND TRIM(email) <> ''
             UNION ALL
             SELECT email, NULL AS name, 1 AS pri FROM visitors
              WHERE email IS NOT NULL AND TRIM(email) <> ''
           )
           WHERE LOWER(TRIM(email)) NOT IN (SELECT email FROM unsubscribes)
           GROUP BY LOWER(TRIM(email))
           ORDER BY email"""
    )
    return [{"email": r["email"], "name": r["name"] or ""} for r in rows]


def enqueue_send(subject: str, body: str) -> tuple[int, int]:
    """Create the announcement and stage one delivery job per recipient.

    Returns (announcement_id, recipient_count). Staged inside one transaction
    so the paper-trail row and its deliveries commit together; dispatched after
    commit so a rollback can never leave live jobs for a record that does not
    exist (the exact doctrine of the Stripe webhook's Notion job).
    """
    from . import jobs

    people = audience()
    job_ids: list[int] = []
    with db.tx() as con:
        aid = con.execute(
            "INSERT INTO announcements (subject, body, audience_count) VALUES (?,?,?)",
            (subject, body, len(people)),
        ).lastrowid
        for p in people:
            job_ids.append(
                jobs.stage(
                    con,
                    "announcement_email",
                    {"announcement_id": aid, "email": p["email"], "name": p["name"]},
                )
            )
    jobs.dispatch(job_ids)
    log.info("announcement %s staged for %d recipients", aid, len(people))
    return aid, len(people)


def deliver(announcement_id: int, email: str, name: str) -> None:
    """One recipient's send — the announcement_email job handler.

    Re-checks the ledger (they may have unsubscribed between staging and now),
    then sleeps a beat so a burst of a few hundred trickles through Gmail
    rather than slamming its rate limits — this blocks one job worker, which is
    exactly the throttle we want for an announcement burst.
    """
    a = db.one("SELECT * FROM announcements WHERE id=?", (announcement_id,))
    if not a:
        log.error("announcement %s vanished; skipping %s", announcement_id, email)
        return
    if is_unsubscribed(email):
        db.run(
            "UPDATE announcements SET skipped_count=skipped_count+1 WHERE id=?",
            (announcement_id,),
        )
        return
    greeting = f"Hi {name}," if name else "Hi,"
    footer = (
        f"\n\n—\nYou're getting this because you've worked with {config.SITE_NAME} "
        f"or downloaded one of my galleries. One click and you'll never hear from "
        f"me again: {config.BASE_URL}/u/{unsubscribe_token(email)}\n"
    )
    time.sleep(1 + random.random())  # Gmail-friendly trickle, per-recipient jitter
    mailer.send(
        email, a["subject"], f"{greeting}\n\n{a['body']}{footer}", reply_to=config.GMAIL_USER
    )
    db.run(
        "UPDATE announcements SET sent_count=sent_count+1 WHERE id=?",
        (announcement_id,),
    )
