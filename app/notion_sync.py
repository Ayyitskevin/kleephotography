"""Push invoice money status to the Notion Session page.

Keeps Odysseus automations (balance_chaser, digest REVENUE) accurate with zero
Odysseus changes. Property names match Odysseus P_SESSION — its API contract,
do not rename.
"""

import json
import logging
import urllib.request

from . import config, db, hermes_arm

log = logging.getLogger("mise.notion")


def _patch_page(page_id: str, props: dict) -> None:
    req = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{page_id}",
        method="PATCH",
        data=json.dumps({"properties": props}).encode(),
        headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _create_page(database_id: str, props: dict) -> str:
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        method="POST",
        data=json.dumps({"parent": {"database_id": database_id}, "properties": props}).encode(),
        headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["id"]


def sync_invoice(invoice_id: int) -> None:
    d = db.one(
        """SELECT i.*, p.notion_page_id FROM invoices i
                  JOIN projects p ON p.id=i.project_id WHERE i.id=?""",
        (invoice_id,),
    )
    if not d:
        raise ValueError(f"invoice {invoice_id} not found")
    if not config.NOTION_TOKEN or not d["notion_page_id"]:
        log.info(
            "notion sync skipped for invoice %s (token=%s page=%s)",
            invoice_id,
            bool(config.NOTION_TOKEN),
            bool(d["notion_page_id"]),
        )
        return
    _patch_page(
        d["notion_page_id"],
        {
            "Invoice Amount": {"number": d["total_cents"] / 100},
            "Deposit Amount": {"number": d["deposit_cents"] / 100},
            "Invoice Paid": {"checkbox": d["status"] == "paid"},
            "Deposit Paid": {
                "checkbox": bool(d["deposit_cents"]) and d["status"] in ("deposit_paid", "paid")
            },
        },
    )
    log.info("notion session synced from invoice %s (%s)", invoice_id, d["status"])


def sync_gallery(gallery_id: int) -> None:
    d = db.one(
        """SELECT g.slug, g.published, p.title AS project_title,
                         p.notion_page_id, c.name AS client_name, c.company
                  FROM galleries g
                  JOIN projects p ON p.id=g.project_id
                  LEFT JOIN clients c ON c.id=p.client_id
                  WHERE g.id=?""",
        (gallery_id,),
    )
    if not d:
        raise ValueError(f"gallery {gallery_id} not found or not linked to a project")
    if not config.NOTION_TOKEN or not d["notion_page_id"] or not d["published"]:
        log.info(
            "notion gallery sync skipped for %s (token=%s page=%s published=%s)",
            gallery_id,
            bool(config.NOTION_TOKEN),
            bool(d["notion_page_id"]),
            bool(d["published"]),
        )
        return
    _patch_page(
        d["notion_page_id"],
        {
            "Gallery URL": {"url": f"{config.BASE_URL}/g/{d['slug']}"},
            # Publishing a gallery IS the delivery event. Flip the Session to
            # "Delivered" (P_SESSION contract) so Odysseus post_delivery fires its
            # review request off the real Mise event. Enqueued only on the publish
            # transition (admin.galleries), and post_delivery is gated by its
            # "Delivery Processed" checkbox, so the request still goes out once.
            "Status": {"select": {"name": "Delivered"}},
        },
    )
    log.info("notion session delivered + gallery URL set from gallery %s", gallery_id)
    # Arm the +N day owner check that nothing else covers: Odysseus post_delivery
    # SENDS the review request, but no automation verifies a review actually landed.
    # Best-effort — the delivery above already succeeded, so a down Hermes must not
    # fail this job; Hermes dedups by key, so a job retry can't double-arm.
    who = d["company"] or d["client_name"] or d["project_title"]
    hermes_arm.arm(
        key=f"review-check:{gallery_id}",
        text=(
            f"Gallery for {who} was delivered {config.REVIEW_CHECK_DAYS}d ago — "
            f"did the review land? If not, send a quick personal ask. "
            f"{config.BASE_URL}/g/{d['slug']}"
        ),
        when=hermes_arm.at_9am(config.REVIEW_CHECK_DAYS),
    )


_INTAKE_FIELDS = [
    ("venue_address", "Venue / address"),
    ("dish_count", "Dishes / setups"),
    ("parking_notes", "Parking & loading"),
    ("onsite_contact", "On-site contact"),
    ("style_refs", "Style references"),
]


def intake_summary(b) -> str:
    """Human-readable block of the F&B intake a client gave at booking time, for the
    Studio project notes and the Notion Session. Empty string if nothing was filled in.
    Tolerates rows that predate the intake columns (keys() guard)."""
    keys = set(b.keys())
    lines = [
        f"{label}: {b[col]}"
        for col, label in _INTAKE_FIELDS
        if col in keys and (b[col] or "").strip()
    ]
    return ("Shoot intake —\n" + "\n".join(lines)) if lines else ""


def sync_booking(booking_id: int) -> None:
    """One-way mirror of a scheduler booking into the Notion 'Bookings' calendar DB
    (WINDOW doctrine — display only, never read back). Dormant unless both the Mise
    integration token and NOTION_BOOKINGS_DB are set. First call creates the page and
    stores its id on the booking; later calls (e.g. cancellation) patch Status in place.

    Expected Bookings DB properties (Kevin creates + shares the DB with the Mise
    integration): Name (title), When (date), Event (rich_text), Email (email),
    Phone (phone_number), Status (select), Notes (rich_text)."""
    b = db.one(
        """SELECT b.*, e.name AS event_name FROM bookings b
                  JOIN event_types e ON e.id=b.event_type_id WHERE b.id=?""",
        (booking_id,),
    )
    if not b:
        raise ValueError(f"booking {booking_id} not found")
    if not config.NOTION_TOKEN or not config.NOTION_BOOKINGS_DB:
        log.info(
            "notion booking sync skipped for %s (token=%s db=%s)",
            booking_id,
            bool(config.NOTION_TOKEN),
            bool(config.NOTION_BOOKINGS_DB),
        )
        return
    # Re-read immediately before create/patch so concurrent confirm/cancel
    # jobs cannot both decide to create when the stamp is still null.
    b = db.one(
        """SELECT b.*, e.name AS event_name FROM bookings b
                  JOIN event_types e ON e.id=b.event_type_id WHERE b.id=?""",
        (booking_id,),
    )
    if not b:
        raise ValueError(f"booking {booking_id} not found")
    status = "Cancelled" if b["status"] == "cancelled" else "Confirmed"
    start_iso = b["start_utc"].replace(" ", "T") + "Z"
    end_iso = b["end_utc"].replace(" ", "T") + "Z"
    props = {
        "Name": {"title": [{"text": {"content": b["name"]}}]},
        "When": {"date": {"start": start_iso, "end": end_iso}},
        "Event": {"rich_text": [{"text": {"content": b["event_name"]}}]},
        "Email": {"email": b["email"]},
        "Phone": {"phone_number": b["phone"] or None},
        "Status": {"select": {"name": status}},
        "Notes": {"rich_text": [{"text": {"content": (b["notes"] or "")[:1900]}}]},
    }
    if b["notion_page_id"]:
        _patch_page(b["notion_page_id"], {"Status": props["Status"], "When": props["When"]})
        log.info("notion booking %s patched (%s)", booking_id, status)
    else:
        page_id = _create_page(config.NOTION_BOOKINGS_DB, props)
        with db.tx() as con:
            cur = con.execute(
                "UPDATE bookings SET notion_page_id=? WHERE id=? AND notion_page_id IS NULL",
                (page_id, booking_id),
            )
            stamped = cur.rowcount == 1
        if stamped:
            log.info("notion booking %s mirrored as page %s", booking_id, page_id)
        else:
            kept = db.one("SELECT notion_page_id FROM bookings WHERE id=?", (booking_id,))
            log.warning(
                "notion booking %s create raced; remote page %s not stamped (kept %s)",
                booking_id,
                page_id,
                kept["notion_page_id"] if kept else None,
            )


def sync_session_for_booking(booking_id: int) -> None:
    """Seed/link a Notion 'Sessions' page (the pipeline spine) for a confirmed
    booking, so Odysseus's preshoot_pack / balance_chaser / digest attach to it
    exactly as they do for a hand-entered session.

    Gated three ways (all must hold): NOTION_TOKEN + NOTION_SESSIONS_DB set, AND
    the event type opted in via creates_notion_session. Resolution order:
      1. already stamped (notion_session_id) -> done (idempotent, no duplicate);
      2. a reschedule -> reuse the superseded booking's Session (link only);
      3. an existing project's Session matches the client email -> link to it;
      4. otherwise CREATE a new Session with core identity only (name, date,
         status, booking link) — shot-list/preshoot hydration stays in the pipeline.

    Only the create branch writes Session fields. Attach/reschedule branches just
    stamp the link on the booking and never patch the Session, so pipeline-owned
    fields (shoot date, money, shot list) are never clobbered (one-way doctrine)."""
    b = db.one(
        """SELECT b.*, e.name AS event_name, e.creates_notion_session
                  FROM bookings b JOIN event_types e ON e.id=b.event_type_id
                  WHERE b.id=?""",
        (booking_id,),
    )
    if not b:
        raise ValueError(f"booking {booking_id} not found")
    if not config.NOTION_TOKEN or not config.NOTION_SESSIONS_DB:
        log.info(
            "notion session sync skipped for booking %s (token=%s db=%s)",
            booking_id,
            bool(config.NOTION_TOKEN),
            bool(config.NOTION_SESSIONS_DB),
        )
        return
    if not b["creates_notion_session"]:
        log.info("notion session sync skipped for booking %s (event type opted out)", booking_id)
        return
    if b["notion_session_id"]:
        log.info(
            "notion session for booking %s already linked (%s)", booking_id, b["notion_session_id"]
        )
        return

    sid = None
    if b["reschedule_of"]:
        prev = db.one("SELECT notion_session_id FROM bookings WHERE id=?", (b["reschedule_of"],))
        if prev and prev["notion_session_id"]:
            sid = prev["notion_session_id"]
    if not sid:
        m = db.one(
            """SELECT p.notion_page_id FROM projects p
                      JOIN clients c ON c.id=p.client_id
                      WHERE c.email=? AND p.notion_page_id IS NOT NULL
                      ORDER BY p.created_at DESC LIMIT 1""",
            (b["email"],),
        )
        if m:
            sid = m["notion_page_id"]

    if sid:
        db.run("UPDATE bookings SET notion_session_id=? WHERE id=?", (sid, booking_id))
        log.info("notion session for booking %s linked to existing %s", booking_id, sid)
        return

    start_iso = b["start_utc"].replace(" ", "T") + "Z"
    notes = (
        f"Auto-created from a Mise booking.\n"
        f"Client: {b['name']} <{b['email']}>"
        f"{(' · ' + b['phone']) if b['phone'] else ''}\n"
        f"Event: {b['event_name']}\n"
        f"Manage: {config.BASE_URL}/booking/{b['token']}"
    )
    intake = intake_summary(b)
    if intake:
        notes += "\n\n" + intake
    sid = _create_page(
        config.NOTION_SESSIONS_DB,
        {
            "Session Name": {"title": [{"text": {"content": f"{b['event_name']} · {b['name']}"}}]},
            "Shoot Date": {"date": {"start": start_iso}},
            "Status": {"select": {"name": "Booked"}},
            "Auto-Spawned": {"checkbox": True},
            "Session Notes (Quick)": {"rich_text": [{"text": {"content": notes[:1900]}}]},
        },
    )
    db.run("UPDATE bookings SET notion_session_id=? WHERE id=?", (sid, booking_id))
    # Unify the auto-created Studio project with its Session page so the pipeline
    # (and future bookings' email-match above) attach to the same record.
    if b["project_id"]:
        db.run(
            """UPDATE projects SET notion_page_id=?
                  WHERE id=? AND notion_page_id IS NULL""",
            (sid, b["project_id"]),
        )
    log.info("notion session created for booking %s as page %s", booking_id, sid)


def _inquiry_status(q) -> str:
    """Triage state as a single display word — converted wins over dismissed
    (studio.py refuses to dismiss a converted inquiry, so both set = legacy)."""
    if q["converted_at"]:
        return "Converted"
    if q["dismissed_at"]:
        return "Dismissed"
    return "New"


def _inquiry_props(q) -> dict:
    return {
        "Name": {"title": [{"text": {"content": q["name"]}}]},
        "Email": {"email": q["email"]},
        "Phone": {"phone_number": q["phone"] or None},
        "Business": {"rich_text": [{"text": {"content": q["business"] or ""}}]},
        "Niche": {"select": {"name": q["service"]} if q["service"] else None},
        "Kind": {"select": {"name": q["kind"]}},
        "Message": {"rich_text": [{"text": {"content": (q["message"] or "")[:1900]}}]},
        "Submitted": {"date": {"start": q["created_at"].replace(" ", "T") + "Z"}},
        "Status": {"select": {"name": _inquiry_status(q)}},
        "Mise ID": {"number": q["id"]},
    }


def sync_inquiry(inquiry_id: int, dry_run: bool = False) -> dict | None:
    """One-way mirror of an inquiry into the Notion 'Leads' database (WINDOW
    doctrine — display only, never read back). Dormant unless NOTION_TOKEN and
    NOTION_LEADS_DB are both set. First call creates the page and stamps its id
    on the inquiry; later calls (convert/dismiss/undo) patch Status in place, so
    the Notion view tracks triage without ever becoming a second writer of lead
    truth — Mise's inquiries table stays the system of record.

    dry_run=True builds and returns the exact plan with ZERO network calls and
    ZERO db writes — scripts/leads-dryrun.py and the tests use it to show what
    WOULD be written and where.

    Expected Leads DB properties (Kevin creates + shares the DB with the Mise
    integration): Name (title), Email (email), Phone (phone_number), Business
    (rich_text), Niche (select), Kind (select), Message (rich_text), Submitted
    (date), Status (select), Mise ID (number)."""
    q = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not q:
        raise ValueError(f"inquiry {inquiry_id} not found")
    props = _inquiry_props(q)
    armed = bool(config.NOTION_TOKEN and config.NOTION_LEADS_DB)
    if dry_run:
        plan = {
            "armed": armed,
            "action": "patch" if q["notion_page_id"] else "create",
            "target": (
                f"notion page {q['notion_page_id']}"
                if q["notion_page_id"]
                else f"notion leads db {config.NOTION_LEADS_DB or '<MISE_NOTION_LEADS_DB unset>'}"
            ),
            "properties": {"Status": props["Status"]} if q["notion_page_id"] else props,
        }
        log.info("notion lead DRY RUN inquiry %s: %s", inquiry_id, json.dumps(plan, indent=2))
        return plan
    if not armed:
        log.info(
            "notion lead sync skipped for inquiry %s (token=%s db=%s)",
            inquiry_id,
            bool(config.NOTION_TOKEN),
            bool(config.NOTION_LEADS_DB),
        )
        return None
    # Re-read immediately before the create/patch branch so a concurrent job
    # that already stamped page id wins — prevents double-create when intake
    # and triage both enqueue notion_sync_inquiry for the same row.
    q = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not q:
        raise ValueError(f"inquiry {inquiry_id} not found")
    props = _inquiry_props(q)
    if q["notion_page_id"]:
        _patch_page(q["notion_page_id"], {"Status": props["Status"]})
        log.info("notion lead %s status patched (%s)", inquiry_id, _inquiry_status(q))
    else:
        page_id = _create_page(config.NOTION_LEADS_DB, props)
        # Conditional stamp: if another worker raced and stamped first, keep
        # their page id (system of record) and log the orphan remote page for
        # operator cleanup rather than clobbering the stamp.
        with db.tx() as con:
            cur = con.execute(
                "UPDATE inquiries SET notion_page_id=? WHERE id=? AND notion_page_id IS NULL",
                (page_id, inquiry_id),
            )
            stamped = cur.rowcount == 1
        if stamped:
            log.info("notion lead %s mirrored as page %s", inquiry_id, page_id)
        else:
            kept = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (inquiry_id,))
            log.warning(
                "notion lead %s create raced; remote page %s not stamped (kept %s)",
                inquiry_id,
                page_id,
                kept["notion_page_id"] if kept else None,
            )
    return None
