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

from .. import booking_notify, config, db, ics, scheduling, security
from ..render import templates

log = logging.getLogger("mise.public.scheduling")
router = APIRouter()


def _today_local() -> dt.date:
    return dt.datetime.now(dt.UTC).astimezone(ZoneInfo(config.TIMEZONE)).date()


def _valid_email(e: str) -> bool:
    return "@" in e and "." in e.rsplit("@", 1)[-1]


def _month_ctx(et, year: int, month: int, today: dt.date, visitor_tz: str) -> dict:
    """Month grid + the set of days (ISO) that actually have open slots."""
    cal = calendar.Calendar(firstweekday=6)  # Sunday-first, matches admin calendar
    weeks = cal.monthdayscalendar(year, month)
    ndays = calendar.monthrange(year, month)[1]
    avail = scheduling.days_with_slots(et, dt.date(year, month, 1), ndays)
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


def _picker_ctx(et, request: Request, *, is_reschedule=False, token=""):
    """Shared context for the day/slot picker (new booking and reschedule)."""
    q = request.query_params
    today = _today_local()
    try:
        year = int(q.get("year") or today.year)
        month = int(q.get("month") or today.month)
        if not (1 <= month <= 12 and today.year <= year <= today.year + 3):
            year, month = today.year, today.month
    except ValueError:
        year, month = today.year, today.month
    visitor_tz = (q.get("tz") or "").strip()
    ctx = {
        "e": et,
        "is_reschedule": is_reschedule,
        "token": token,
        "error": None,
        "slots": None,
        "sel_day": None,
        "sel_start": None,
    }
    ctx.update(_month_ctx(et, year, month, today, visitor_tz))

    sel_day = (q.get("day") or "").strip()
    if sel_day and sel_day in ctx["avail"]:
        day = dt.date.fromisoformat(sel_day)
        slots = scheduling.slots_for_day(et, day, visitor_tz)
        ctx["sel_day"] = sel_day
        ctx["slots"] = slots
        sel_start = (q.get("start") or "").strip()
        if sel_start and any(s["utc"] == sel_start for s in slots):
            ctx["sel_start"] = sel_start
    return ctx


# ── booking funnel ───────────────────────────────────────────────────────────


@router.get("/book", response_class=HTMLResponse)
async def book_index(request: Request):
    from .site import BOOK_FAQS

    events = scheduling.active_event_types()
    return templates.TemplateResponse(
        request,
        "public/book_index.html",
        {"events": events, "faqs": BOOK_FAQS, "faq_heading": "Common questions"},
    )


@router.get("/book/{slug}", response_class=HTMLResponse)
async def book_event(request: Request, slug: str):
    et = scheduling.event_by_slug(slug)
    if not et:
        raise HTTPException(status_code=404)
    ctx = _picker_ctx(et, request)
    ctx["form_action"] = f"/book/{slug}"
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
):
    et = scheduling.event_by_slug(slug)
    if not et:
        raise HTTPException(status_code=404)
    if website.strip():  # honeypot — silently "succeed"
        return RedirectResponse("/book", status_code=303)
    ip = security.client_ip(request)
    name, email = name.strip(), email.strip().lower()
    phone, notes = phone.strip()[:40], notes.strip()[:2000]

    def repicker(msg: str, code: int = 400):
        ctx = _picker_ctx(et, request)
        ctx["form_action"] = f"/book/{slug}"
        ctx["error"] = msg
        return templates.TemplateResponse(request, "public/book_event.html", ctx, status_code=code)

    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_BOOK):
        return repicker(
            "You've booked a few times recently — give me a moment before booking another.", 429
        )
    if not (name and _valid_email(email)):
        return repicker("Please enter your name and a valid email.")
    try:
        bid, token = scheduling.book(et, start, name, email, phone, notes, tz)
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
    return templates.TemplateResponse(
        request, "public/booking_manage.html", {"b": b, "gcal": gcal, "tz_name": config.TIMEZONE}
    )


@router.get("/booking/{token}/invite.ics")
async def invite(token: str):
    b = scheduling.booking_by_token(token)
    if not b:
        raise HTTPException(status_code=404)
    content = ics.build(
        uid=ics.uid_for(b["id"]),
        summary=f"{b['event_name']} · {config.SITE_NAME}",
        description=b["notes"] or "",
        location=b["location"] or "",
        start_utc=b["start_utc"],
        end_utc=b["end_utc"],
        organizer_email=config.GMAIL_USER or "noreply@kleephotography.com",
        attendee_email=b["email"],
        cancelled=(b["status"] != "confirmed"),
        sequence=0 if b["status"] == "confirmed" else 1,
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
    ctx = _picker_ctx(et, request, is_reschedule=True, token=token)
    ctx["form_action"] = f"/booking/{token}/reschedule"
    ctx["old_when"] = b["start_utc"]
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
    except scheduling.SlotTaken:
        ctx = _picker_ctx(et, request, is_reschedule=True, token=token)
        ctx["form_action"] = f"/booking/{token}/reschedule"
        ctx["old_when"] = b["start_utc"]
        ctx["error"] = "Sorry — that time was just taken. Please pick another."
        return templates.TemplateResponse(request, "public/book_event.html", ctx, status_code=409)
    # New slot held; release the old one and email the fresh invite.
    scheduling.cancel(token, "Rescheduled")
    booking_notify.confirm(new_id)
    log.info("booking %s rescheduled -> %s", b["id"], new_id)
    return RedirectResponse(f"/booking/{new_token}", status_code=303)
