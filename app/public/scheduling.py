"""Public booking — the Calendly-style funnel: pick an event, a day, a time, confirm.

Server-rendered and query-param driven (?year=&month=&day=&start=&tz=) so the core
flow needs no JavaScript; a tiny inline script just detects the visitor's timezone
once and reloads with it so slot labels show in their local time. Every slot the
client submits is re-validated inside scheduling.book() — the client's slot list is
never trusted. Honeypot + per-IP throttle guard the POST like the other public forms.
"""

import calendar
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import booking_notify, config, db, features, ics, scheduling, security, specialties
from ..render import templates

log = logging.getLogger("mise.public.scheduling")
router = APIRouter()


def _today_local() -> dt.date:
    return dt.datetime.now(dt.UTC).astimezone(ZoneInfo(config.TIMEZONE)).date()


def _valid_email(e: str) -> bool:
    return "@" in e and "." in e.rsplit("@", 1)[-1]


def _month_ctx(
    et, year: int, month: int, today: dt.date, visitor_tz: str, exclude_id: int | None = None
) -> dict:
    """Month grid + the set of days (ISO) that actually have open slots."""
    cal = calendar.Calendar(firstweekday=6)  # Sunday-first, matches admin calendar
    weeks = cal.monthdayscalendar(year, month)
    ndays = calendar.monthrange(year, month)[1]
    avail = scheduling.days_with_slots(et, dt.date(year, month, 1), ndays, exclude_id)
    max_day = today + dt.timedelta(days=et["booking_window_days"])
    prev_m = dt.date(year, month, 1) - dt.timedelta(days=1)
    next_m = dt.date(year, month, 1) + dt.timedelta(days=32)
    return {
        "weeks": weeks,
        "avail": avail,
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "today": today,
        "has_prev": dt.date(year, month, 1) > dt.date(today.year, today.month, 1),
        "has_next": dt.date(next_m.year, next_m.month, 1)
        <= dt.date(max_day.year, max_day.month, 1),
        "prev_year": prev_m.year,
        "prev_month": prev_m.month,
        "next_year": next_m.year,
        "next_month": next_m.month,
        "visitor_tz": visitor_tz or config.TIMEZONE,
    }


def _picker_ctx(
    et,
    request: Request,
    *,
    is_reschedule=False,
    token="",
    submitted_start="",
    submitted_tz="",
    exclude_id=None,
):
    """Shared context for the day/slot picker (new booking and reschedule)."""
    q = request.query_params
    today = _today_local()
    submitted_instant = None
    if submitted_start:
        try:
            submitted_instant = dt.datetime.strptime(submitted_start, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=dt.UTC
            )
        except ValueError:
            pass
    try:
        submitted_day = (
            submitted_instant.astimezone(ZoneInfo(config.TIMEZONE)).date()
            if submitted_instant
            else None
        )
        year = submitted_day.year if submitted_day else int(q.get("year") or today.year)
        month = submitted_day.month if submitted_day else int(q.get("month") or today.month)
        if not (1 <= month <= 12 and today.year <= year <= today.year + 3):
            year, month = today.year, today.month
    except ValueError:
        year, month = today.year, today.month
    visitor_tz = (submitted_tz or q.get("tz") or "").strip()
    ctx = {
        "e": et,
        "is_reschedule": is_reschedule,
        "token": token,
        "error": None,
        "slots": None,
        "sel_day": None,
        "sel_start": None,
        "selected_slot_label": "",
        "submitted": {},
    }
    ctx.update(_month_ctx(et, year, month, today, visitor_tz, exclude_id))

    sel_day = submitted_day.isoformat() if submitted_day else (q.get("day") or "").strip()
    if sel_day and (submitted_start or sel_day in ctx["avail"]):
        day = dt.date.fromisoformat(sel_day)
        slots = scheduling.slots_for_day(et, day, visitor_tz, exclude_id)
        ctx["sel_day"] = sel_day
        ctx["slots"] = slots
        sel_start = submitted_start or (q.get("start") or "").strip()
        if submitted_start or (sel_start and any(s["utc"] == sel_start for s in slots)):
            ctx["sel_start"] = sel_start
    elif submitted_start:
        ctx["sel_start"] = submitted_start
    if submitted_instant:
        try:
            display_tz = ZoneInfo(visitor_tz or config.TIMEZONE)
        except Exception:
            display_tz = ZoneInfo(config.TIMEZONE)
        ctx["selected_slot_label"] = submitted_instant.astimezone(display_tz).strftime("%-I:%M %p")
    return ctx


# ── booking funnel ───────────────────────────────────────────────────────────


@router.get("/book", response_class=HTMLResponse)
async def book_index(request: Request):
    from .site import _portfolio_assets, _public_photo_spec
    from .site_catalog import BOOK_ACTIVE_PROMISES, BOOK_FAQS, BOOK_PROMISES

    events = scheduling.active_event_types()
    book_rows = _portfolio_assets()[:1]
    book_photo = book_rows[0] if book_rows else None
    return templates.TemplateResponse(
        request,
        "public/book_index.html",
        {
            "events": events,
            "faqs": BOOK_FAQS,
            "faq_heading": "Common questions",
            "book_promises": BOOK_ACTIVE_PROMISES if events else BOOK_PROMISES,
            "book_photo": book_photo,
            "book_image": _public_photo_spec(book_photo) if book_photo else None,
        },
    )


@router.get("/book/{slug}", response_class=HTMLResponse)
async def book_event(request: Request, slug: str):
    et = scheduling.event_by_slug(slug)
    if not et:
        raise HTTPException(status_code=404)
    ctx = _picker_ctx(et, request)
    ctx["form_action"] = f"/book/{slug}"
    # htmx picker swaps fetch just the card; plain GETs get the full page.
    # Same _picker_ctx either way — this is a rendering fork, never a logic one.
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "public/_book_card.html", ctx)
    return templates.TemplateResponse(request, "public/book_event.html", ctx)


@router.post("/book/{slug}", response_class=HTMLResponse)
async def confirm_booking(
    request: Request,
    slug: str,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    notes: str = Form(""),
    start: str = Form(...),
    tz: str = Form(""),
    website: str = Form(""),
    venue_address: str = Form(""),
    dish_count: str = Form(""),
    parking_notes: str = Form(""),
    style_refs: str = Form(""),
    onsite_contact: str = Form(""),
    aerial_pass: str = Form(""),
):
    et = scheduling.event_by_slug(slug)
    if not et:
        raise HTTPException(status_code=404)
    if website.strip():  # honeypot — silently "succeed"
        return RedirectResponse("/book", status_code=303)
    ip = security.client_ip(request)
    submitted = {
        "name": name,
        "email": email,
        "phone": phone,
        "notes": notes,
        "venue_address": venue_address,
        "dish_count": dish_count,
        "parking_notes": parking_notes,
        "style_refs": style_refs,
        "onsite_contact": onsite_contact,
        "aerial_pass": aerial_pass,
    }
    name, email = name.strip(), email.strip().lower()
    phone, notes = phone.strip()[:40], notes.strip()[:2000]
    # The Aerial Pass add-on (re- event types, aerials_live-gated): zero-schema —
    # it rides the booking's notes, so the admin deck + Notion session see it
    # without a migration. Rate string comes from the ONE place (specialties).
    if aerial_pass and slug.startswith("re-") and features.aerials_live():
        tag = f"AERIAL PASS requested ({specialties.aerial_pass_display()} add-on) — confirm LAANC"
        notes = f"{tag}\n{notes}".strip()[:2000]

    def repicker(msg: str, code: int = 400):
        ctx = _picker_ctx(et, request, submitted_start=start, submitted_tz=tz)
        ctx["form_action"] = f"/book/{slug}"
        ctx["error"] = msg
        ctx["submitted"] = submitted
        return templates.TemplateResponse(request, "public/book_event.html", ctx, status_code=code)

    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        return repicker(
            "You've booked a few times recently — give me a moment before booking another.", 429
        )
    if not (name and _valid_email(email)):
        return repicker("Please enter your name and a valid email.")
    try:
        bid, token = scheduling.book(et, start, name, email, phone, notes, tz)
    except scheduling.CalendarUnavailable:
        return repicker(
            "Calendar sync is temporarily unavailable — please try again in a few minutes.",
            503,
        )
    except scheduling.SlotTaken:
        return repicker("Sorry — that time was just taken. Please pick another.", 409)
    # Persist the F&B intake (shoot event types only) before side-effects fire, so the
    # auto-created project + Notion Session pick it up.
    if et["creates_notion_session"]:
        db.run(
            """UPDATE bookings SET venue_address=?, dish_count=?, parking_notes=?,
                  style_refs=?, onsite_contact=? WHERE id=?""",
            (
                venue_address.strip()[:300],
                dish_count.strip()[:60],
                parking_notes.strip()[:500],
                style_refs.strip()[:1000],
                onsite_contact.strip()[:120],
                bid,
            ),
        )
    security.inquiry_record(ip, security.INQUIRY_BUCKET_BOOK)
    booking_notify.confirm(bid)
    log.info("booking %s confirmed (%s)", bid, slug)
    return RedirectResponse(f"/booking/{token}", status_code=303)


# ── manage: confirmation page, cancel, reschedule, invite download ───────────


@router.get("/booking/{token}", response_class=HTMLResponse)
async def manage(request: Request, token: str):
    b = scheduling.booking_by_token(token)
    if not b:
        raise HTTPException(status_code=404)
    gcal = ""
    if b["status"] == "confirmed":
        summary = f"{b['event_name']} · {config.SITE_NAME}"
        gcal = ics.google_link(
            summary=summary,
            details=b["notes"] or "",
            location=b["location"] or "",
            start_utc=b["start_utc"],
            end_utc=b["end_utc"],
        )
    # Show the time in the zone the client picked the slot in (bookings.tz),
    # not the studio's — otherwise a Pacific client who chose "10:00 AM" is told
    # "1:00 PM (America/New_York)" and misses the appointment.
    tz_name = b["tz"] or config.TIMEZONE
    return templates.TemplateResponse(
        request,
        "public/booking_manage.html",
        {"b": b, "gcal": gcal, "tz_name": tz_name},
    )


@router.get("/booking/{token}/invite.ics")
async def invite(token: str):
    b = scheduling.booking_by_token(token)
    if not b:
        raise HTTPException(status_code=404)
    if (
        b["status"] != "confirmed" and b["cancel_reason"] == "Rescheduled"
    ) or scheduling.has_confirmed_replacement(b["id"]):
        return Response(
            "This booking was rescheduled. Use the latest invite from your confirmation email.",
            status_code=410,
            media_type="text/plain",
        )
    uid, sequence = booking_notify.calendar_identity(b["id"])
    content = ics.build(
        uid=uid,
        summary=f"{b['event_name']} · {config.SITE_NAME}",
        description=b["notes"] or "",
        location=b["location"] or "",
        start_utc=b["start_utc"],
        end_utc=b["end_utc"],
        organizer_email=config.GMAIL_USER or "noreply@kleephotography.com",
        attendee_email=b["email"],
        cancelled=(b["status"] != "confirmed"),
        sequence=sequence if b["status"] == "confirmed" else sequence + 1,
    )
    return Response(
        content,
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="invite.ics"'},
    )


@router.post("/booking/{token}/cancel")
async def cancel_booking(request: Request, token: str, reason: str = Form("")):
    b = scheduling.booking_by_token(token)
    if not b:
        raise HTTPException(status_code=404)
    if b["status"] == "confirmed" and scheduling.cancel(token, reason.strip()[:500]):
        booking_notify.cancelled(b["id"])
    return RedirectResponse(f"/booking/{token}", status_code=303)


@router.get("/booking/{token}/reschedule", response_class=HTMLResponse)
async def reschedule_form(request: Request, token: str):
    b = scheduling.booking_by_token(token)
    if not b or b["status"] != "confirmed":
        raise HTTPException(status_code=404)
    et = scheduling.event_by_slug(b["event_slug"])
    if not et:
        raise HTTPException(status_code=404)
    ctx = _picker_ctx(et, request, is_reschedule=True, token=token, exclude_id=b["id"])
    ctx["form_action"] = f"/booking/{token}/reschedule"
    ctx["old_when"] = b["start_utc"]
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "public/_book_card.html", ctx)
    return templates.TemplateResponse(request, "public/book_event.html", ctx)


@router.post("/booking/{token}/reschedule")
async def do_reschedule(request: Request, token: str, start: str = Form(...), tz: str = Form("")):
    b = scheduling.booking_by_token(token)
    if not b or b["status"] != "confirmed":
        raise HTTPException(status_code=404)
    et = scheduling.event_by_slug(b["event_slug"])
    if not et:
        raise HTTPException(status_code=404)
    try:
        new_id, new_token = scheduling.book(
            et,
            start,
            b["name"],
            b["email"],
            b["phone"],
            b["notes"],
            tz or b["tz"],
            exclude_id=b["id"],
        )
    except (scheduling.SlotTaken, scheduling.CalendarUnavailable) as exc:
        ctx = _picker_ctx(
            et,
            request,
            is_reschedule=True,
            token=token,
            exclude_id=b["id"],
            submitted_start=start,
            submitted_tz=tz,
        )
        ctx["form_action"] = f"/booking/{token}/reschedule"
        ctx["old_when"] = b["start_utc"]
        if isinstance(exc, scheduling.CalendarUnavailable):
            ctx["error"] = (
                "Calendar sync is temporarily unavailable — please try again in a few minutes."
            )
            return templates.TemplateResponse(
                request, "public/book_event.html", ctx, status_code=503
            )
        # Return to the canonical choices instead of re-confirming the stale or
        # out-of-policy value that was just rejected.
        ctx["sel_start"] = None
        ctx["selected_slot_label"] = ""
        ctx["error"] = "Sorry — that time is no longer available. Please pick another."
        return templates.TemplateResponse(request, "public/book_event.html", ctx, status_code=409)
    # scheduling.book commits the replacement and original cancellation as one
    # BEGIN IMMEDIATE transaction. Outbound effects only see the coherent state.
    booking_notify.confirm(new_id)
    log.info("booking %s rescheduled -> %s", b["id"], new_id)
    return RedirectResponse(f"/booking/{new_token}", status_code=303)
