"""Side-effects of a booking: confirmation/cancellation emails (client + Kevin),
the .ics invite, the Odysseus inbox hook, and the dormant Notion writeback.

Kept separate from the routes so the public/admin handlers stay thin and the
'what happens when a booking is made' story lives in one place. Every outbound
step is best-effort and logged — a mail or Notion hiccup must never lose the
booking, which is already committed before any of this runs (fail loud, not lost)."""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import alerts, config, db, gcal, ics, mailer, notion_sync

log = logging.getLogger("mise.booking")
_UTC = dt.UTC


def _load(booking_id: int):
    return db.one(
        """SELECT b.*, e.name AS event_name, e.location, e.description AS event_desc
           FROM bookings b JOIN event_types e ON e.id=b.event_type_id
           WHERE b.id=?""",
        (booking_id,),
    )


def _when(start_utc: str, tzname: str) -> str:
    d = dt.datetime.strptime(start_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
    try:
        local = d.astimezone(ZoneInfo(tzname or config.TIMEZONE))
    except Exception:
        local = d.astimezone(ZoneInfo(config.TIMEZONE))
    return local.strftime("%A, %B %-d, %Y · %-I:%M %p %Z").replace(" 0", " ")


def _manage_url(token: str) -> str:
    return f"{config.BASE_URL}/booking/{token}"


def _mail_failure_alert(booking_id: int, audience: str, reason: str) -> None:
    alerts.ops_alert(
        f"booking_email_failed:{audience}",
        f"Booking {booking_id} {audience} email could not be sent ({reason}). "
        f"Review the booking and contact the client manually: "
        f"{config.BASE_URL}/admin/bookings",
    )


def _link_studio(booking_id: int, inquiry_id: int | None) -> None:
    """Find-or-create the Studio client (always) and, for real-shoot event types,
    a project — so the booking, inquiry, client, project and Notion Session share one
    identity instead of spawning duplicate leads. Best-effort: the booking is already
    committed, so a CRM hiccup here must never lose it (fail loud, not lost).

    Runs before the Notion Session sync so the session-create branch can stamp the
    new project's notion_page_id, unifying project <-> Session for the pipeline."""
    b = db.one(
        """SELECT b.*, e.name AS event_name, e.creates_notion_session
                  FROM bookings b JOIN event_types e ON e.id=b.event_type_id
                  WHERE b.id=?""",
        (booking_id,),
    )
    if not b or b["client_id"]:  # idempotent on re-run
        return

    # A reschedule inherits the original booking's client/project — never a new lead.
    if b["reschedule_of"]:
        prev = db.one(
            "SELECT client_id, project_id FROM bookings WHERE id=?", (b["reschedule_of"],)
        )
        if prev and prev["client_id"]:
            db.run(
                "UPDATE bookings SET client_id=?, project_id=? WHERE id=?",
                (prev["client_id"], prev["project_id"], booking_id),
            )
            return

    existing = db.one("SELECT id FROM clients WHERE email=?", (b["email"],))
    cid = (
        existing["id"]
        if existing
        else db.run(
            "INSERT INTO clients (name, email, phone, notes) VALUES (?,?,?,?)",
            (
                b["name"],
                b["email"],
                b["phone"] or "",
                f"Auto-created from a Mise booking {b['created_at'][:10]}.",
            ),
        )
    )
    if not existing:
        log.info("booking %s created client %s", booking_id, cid)

    pid = None
    # Only real shoots (the same opt-in that spawns a Notion Session) get a project;
    # discovery/consult calls stay client-only so Kevin promotes them by hand if needed.
    if b["creates_notion_session"]:
        title = f"{b['event_name']} — {b['start_utc'][:10]}"
        pid = db.run(
            """INSERT INTO projects (client_id, title, shoot_date, notes)
                        VALUES (?,?,?,?)""",
            (cid, title, b["start_utc"][:10], notion_sync.intake_summary(b) or None),
        )
        log.info("booking %s spawned project %s", booking_id, pid)
        # Fade the mirrored inquiry out of the studio 'to convert' list so Kevin's
        # manual convert button can't double-create the same project.
        if inquiry_id:
            db.run(
                """UPDATE inquiries SET converted_at=datetime('now'),
                      converted_client_id=?, converted_project_id=? WHERE id=?""",
                (cid, pid, inquiry_id),
            )

    db.run("UPDATE bookings SET client_id=?, project_id=? WHERE id=?", (cid, pid, booking_id))


def confirm(booking_id: int) -> None:
    """Email client + Kevin with the invite; mirror to Odysseus + Notion."""
    b = _load(booking_id)
    if not b:
        log.error("confirm: booking %s vanished", booking_id)
        return
    biz_when = _when(b["start_utc"], config.TIMEZONE)
    cli_when = _when(b["start_utc"], b["tz"])
    uid = ics.uid_for(booking_id)
    loc = b["location"] or "Details to follow"
    summary = f"{b['event_name']} · {config.SITE_NAME}"
    details = (
        f"{b['event_desc']}\n\n" if b["event_desc"] else ""
    ) + f"Manage this booking: {_manage_url(b['token'])}"
    gcal_link = ics.google_link(
        summary=summary,
        details=details,
        location=loc,
        start_utc=b["start_utc"],
        end_utc=b["end_utc"],
    )

    if not mailer.configured():
        log.error("booking %s confirmed but mailer not configured — no emails sent", booking_id)
        _mail_failure_alert(booking_id, "confirmation", "mailer is not configured")
    else:
        invite = {
            "filename": "invite.ics",
            "method": "REQUEST",
            "content": ics.build(
                uid=uid,
                summary=summary,
                description=details,
                location=loc,
                start_utc=b["start_utc"],
                end_utc=b["end_utc"],
                organizer_email=config.GMAIL_USER,
                attendee_email=b["email"],
            ),
        }
        client_body = (
            f"Hi {b['name']},\n\n"
            f"Your booking is confirmed:\n\n"
            f"  {b['event_name']}\n  {cli_when}\n  {loc}\n\n"
            f"Add it to your calendar with the attached invite, or here:\n{gcal_link}\n\n"
            f"Need to change or cancel? {_manage_url(b['token'])}\n\n"
            f"— {config.SITE_NAME}\n"
        )
        kevin_body = (
            f"New booking via {config.BASE_URL}\n\n"
            f"Event: {b['event_name']}\nWhen: {biz_when}\n"
            f"Name: {b['name']}\nEmail: {b['email']}\nPhone: {b['phone'] or '—'}\n\n"
            f"{b['notes'] or '(no note)'}\n\nManage: {_manage_url(b['token'])}\n"
        )
        try:
            mailer.send(
                b["email"],
                f"Booking confirmed — {b['event_name']}",
                client_body,
                reply_to=config.GMAIL_USER,
                ics=invite,
            )
        except Exception as e:
            log.error("booking %s client email failed: %s", booking_id, e)
            _mail_failure_alert(booking_id, "client confirmation", type(e).__name__)
        try:
            # Kevin's copy doubles as the Odysseus inquiry_intake hook (it polls his inbox).
            mailer.send(
                config.GMAIL_USER,
                f"Booking — {b['name']} · {b['event_name']} · {biz_when}",
                kevin_body,
                reply_to=b["email"],
                ics=invite,
            )
        except Exception as e:
            log.error("booking %s kevin email failed: %s", booking_id, e)
            _mail_failure_alert(booking_id, "operator confirmation", type(e).__name__)

    # Mise-side inquiry row keeps the admin inquiry list + Odysseus consistent.
    iid = None
    try:
        iid = db.run(
            """INSERT INTO inquiries (name, email, business, message, kind,
                                      shoot_date, service, emailed)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                b["name"],
                b["email"],
                None,
                f"Booked {b['event_name']} for {biz_when}.\n\n{b['notes']}",
                "booking",
                b["start_utc"][:10],
                b["event_name"],
                1 if mailer.configured() else 0,
            ),
        )
        db.run("UPDATE bookings SET inquiry_id=? WHERE id=?", (iid, booking_id))
    except Exception as e:
        log.error("booking %s inquiry-row mirror failed: %s", booking_id, e)

    # Link the booking into the Studio CRM (client always; project for real shoots).
    try:
        _link_studio(booking_id, iid)
    except Exception as e:
        log.error("booking %s studio link failed: %s", booking_id, e)

    try:
        notion_sync.sync_booking(booking_id)
    except Exception as e:
        log.error("booking %s notion writeback failed: %s", booking_id, e)

    # Seed/link the Notion Session spine (no-op unless the event type opted in
    # and NOTION_SESSIONS_DB is armed). Kept separate from the calendar-mirror
    # writeback above — different gate, different failure domain.
    try:
        notion_sync.sync_session_for_booking(booking_id)
    except Exception as e:
        log.error("booking %s notion session sync failed: %s", booking_id, e)

    # Mirror onto Kevin's Google calendar (best-effort; no-op if not connected).
    gcal.on_booking_confirmed(booking_id)


def cancelled(booking_id: int, by_admin: bool = False) -> None:
    """Email both parties a CANCEL invite so the held slot drops off calendars."""
    b = _load(booking_id)
    if not b:
        return
    cli_when = _when(b["start_utc"], b["tz"])
    summary = f"{b['event_name']} · {config.SITE_NAME}"
    if mailer.configured():
        cancel_ics = {
            "filename": "cancel.ics",
            "method": "CANCEL",
            "content": ics.build(
                uid=ics.uid_for(booking_id),
                summary=summary,
                description="This booking was cancelled.",
                location=b["location"] or "",
                start_utc=b["start_utc"],
                end_utc=b["end_utc"],
                organizer_email=config.GMAIL_USER,
                attendee_email=b["email"],
                sequence=1,
                cancelled=True,
            ),
        }
        body = (
            f"Hi {b['name']},\n\nYour booking has been cancelled:\n\n"
            f"  {b['event_name']}\n  {cli_when}\n\n"
            f"Book a new time any time: {config.BASE_URL}/book\n\n"
            f"— {config.SITE_NAME}\n"
        )
        try:
            mailer.send(
                b["email"],
                f"Booking cancelled — {b['event_name']}",
                body,
                reply_to=config.GMAIL_USER,
                ics=cancel_ics,
            )
        except Exception as e:
            log.error("booking %s cancel email failed: %s", booking_id, e)
        if not by_admin:
            try:
                mailer.send(
                    config.GMAIL_USER,
                    f"Booking CANCELLED — {b['name']} · {b['event_name']}",
                    f"{b['name']} cancelled their {b['event_name']} "
                    f"({_when(b['start_utc'], config.TIMEZONE)}).\n"
                    f"Reason: {b['cancel_reason'] or '—'}\n",
                    reply_to=b["email"],
                )
            except Exception as e:
                log.error("booking %s kevin cancel email failed: %s", booking_id, e)
    try:
        notion_sync.sync_booking(booking_id)
    except Exception as e:
        log.error("booking %s notion cancel writeback failed: %s", booking_id, e)

    # Drop the matching Google calendar event (best-effort; no-op if not connected).
    gcal.on_booking_cancelled(booking_id)
