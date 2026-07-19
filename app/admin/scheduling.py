"""Scheduler admin — event types, weekly availability, date overrides, bookings.

The public/visitor side lives in app/public/scheduling.py; this is the owner's
console. Mutations go through db.tx() + audit.log so a change to what the public
booking page offers is observable (R14). Slug is a public URL token (/book/{slug})
so it is charset-validated and IMMUTABLE after create, like crop-preset slugs.
"""

import calendar as _calendar
import datetime as dt
import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, booking_notify, db, gcal, scheduling, security
from ..render import templates

log = logging.getLogger("mise.admin.scheduling")
router = APIRouter(prefix="/admin/scheduling", dependencies=[Depends(security.require_admin)])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_EVENT_COLS = frozenset(
    {
        "name",
        "description",
        "duration_min",
        "location",
        "color",
        "buffer_before_min",
        "buffer_after_min",
        "min_notice_hours",
        "max_per_day",
        "booking_window_days",
        "slot_step_min",
        "position",
        "creates_notion_session",
    }
)


def _min_to_hhmm(m: int | None) -> str:
    if m is None:
        return ""
    return f"{m // 60:02d}:{m % 60:02d}"


def _hhmm_to_min(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not match:
        raise HTTPException(status_code=400, detail="bad time")
    h, m = (int(part) for part in match.groups())
    if h == 24 and m == 0:
        return 1440
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=400, detail="time out of range")
    return h * 60 + m


def _posint(form, key: str, lo: int, hi: int, default: int = 0) -> int:
    raw = (form.get(key) or "").strip()
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad {key}")
    if not (lo <= v <= hi):
        raise HTTPException(status_code=400, detail=f"{key} out of range ({lo}–{hi})")
    return v


def _get_event(event_id: int) -> "db.sqlite3.Row":
    e = db.one("SELECT * FROM event_types WHERE id=?", (event_id,))
    if not e:
        raise HTTPException(status_code=404)
    return e


def _fmt_dur(mins: int) -> str:
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    if m == 0:
        return f"{h} hr" if h == 1 else f"{h} hrs"
    return f"{h}h {m}m"


def _fmt_clock(total_min: int) -> str:
    h, m = divmod(total_min, 60)
    ap = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}:{m:02d} {ap}"


def _hours_label(hhmm_start: str, hhmm_end: str) -> str:
    def _m(s: str) -> int:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)

    return f"{_fmt_clock(_m(hhmm_start))} – {_fmt_clock(_m(hhmm_end))}"


def _global_week() -> list[dict]:
    """The default weekly schedule (event_type_id IS NULL), one row per weekday.
    The UI edits a single window per day; the engine supports more (date overrides
    cover exceptions)."""
    rows = db.all_("""SELECT weekday, MIN(start_min) AS s, MAX(end_min) AS e
                      FROM availability_rules WHERE event_type_id IS NULL
                      GROUP BY weekday""")
    by_wd = {r["weekday"]: r for r in rows}
    out = []
    for wd in range(7):
        r = by_wd.get(wd)
        out.append(
            {
                "wd": wd,
                "label": WEEKDAYS[wd],
                "on": r is not None,
                "start": _min_to_hhmm(r["s"]) if r else "09:00",
                "end": _min_to_hhmm(r["e"]) if r else "17:00",
            }
        )
    return out


# ── main console ─────────────────────────────────────────────────────────────

_GERR = {
    "state": "Connection request expired or didn't match — please try again.",
    "denied": "Google sign-in was cancelled.",
    "exchange": "Google rejected the connection. Re-check the OAuth client config and try again.",
}


def _to_local(start_utc: str):
    return scheduling._parse_utc(start_utc).astimezone(scheduling._biz_tz())


def _sched_overview(events, week) -> dict:
    """Honest projection of the booking page state onto the prototype's layout:
    session-type cards, weekly-hours rows, a mini month calendar with booked-day
    dots, and the next few upcoming bookings — all from real rows."""
    tz = scheduling._biz_tz()
    today = scheduling.now_utc().astimezone(tz).date()

    types = [
        {
            "id": e["id"],
            "slug": e["slug"],
            "name": e["name"],
            "dot": e["color"],
            "on": bool(e["active"]),
            "meta": f"{_fmt_dur(e['duration_min'])} · {e['location'] or 'No location set'}",
            "upcoming": e["upcoming"],
            "upcoming_label": (f"{e['upcoming']} upcoming" if e["upcoming"] else "No bookings"),
        }
        for e in events
    ]

    days = [
        {**d, "hours": _hours_label(d["start"], d["end"]) if d["on"] else "Unavailable"}
        for d in week
    ]

    # Upcoming list (next 5 confirmed).
    ups = db.all_("""SELECT b.start_utc, b.name, e.name AS event_name FROM bookings b
                     JOIN event_types e ON e.id=b.event_type_id
                     WHERE b.status='confirmed' AND b.start_utc >= datetime('now')
                     ORDER BY b.start_utc LIMIT 5""")
    upcoming = []
    for b in ups:
        ld = _to_local(b["start_utc"])
        d = ld.date()
        clock = ld.strftime("%-I:%M %p")
        if d == today:
            time_lbl, soon = f"Today · {clock}", True
        elif d == today + dt.timedelta(days=1):
            time_lbl, soon = f"Tomorrow · {clock}", False
        else:
            time_lbl, soon = clock, False
        upcoming.append(
            {
                "day": ld.strftime("%-d"),
                "mon": ld.strftime("%b"),
                "client": b["name"],
                "type": b["event_name"],
                "time": time_lbl,
                "soon": soon,
            }
        )

    next_up = (
        upcoming[0]["time"]
        .replace("Today · ", "today ")
        .replace("Tomorrow · ", "tomorrow ")
        .lower()
        if upcoming
        else None
    )

    # Mini calendar (current local month, Sunday-first) + booked-day dots.
    ndays = _calendar.monthrange(today.year, today.month)[1]
    first = today.replace(day=1)
    lo = (first - dt.timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    hi = (first + dt.timedelta(days=ndays + 1)).strftime("%Y-%m-%d 00:00:00")
    booked: set[int] = set()
    for r in db.all_(
        """SELECT start_utc FROM bookings
                        WHERE status='confirmed' AND start_utc>=? AND start_utc<?""",
        (lo, hi),
    ):
        ld = _to_local(r["start_utc"]).date()
        if ld.year == today.year and ld.month == today.month:
            booked.add(ld.day)

    cells = [{"empty": True}] * ((first.weekday() + 1) % 7)
    cells += [{"day": n} for n in range(1, ndays + 1)]
    while len(cells) % 7:
        cells.append({"empty": True})
    cal = []
    for c in cells:
        is_today = c.get("day") == today.day and not c.get("empty")
        cal.append(
            {
                "day": c.get("day", ""),
                "empty": c.get("empty", False),
                "today": is_today,
                "booked": (c.get("day") in booked) and not is_today,
            }
        )

    week_mon = today - dt.timedelta(days=today.weekday())
    booked_week = sum(1 for d in booked if 0 <= (first.replace(day=d) - week_mon).days <= 6)

    return {
        "types": types,
        "days": days,
        "upcoming": upcoming,
        "next_up": next_up,
        "booked_week": booked_week,
        "cal": cal,
        "cal_label": first.strftime("%B %Y"),
        "dow": ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"],
    }


@router.get("", response_class=HTMLResponse)
async def home(request: Request):
    events = db.all_("""SELECT et.*,
                        (SELECT COUNT(*) FROM bookings b
                         WHERE b.event_type_id=et.id AND b.status='confirmed'
                           AND b.start_utc >= datetime('now')) AS upcoming
                        FROM event_types et ORDER BY et.position, et.id""")
    week = _global_week()
    ctx = {
        "events": events,
        "week": week,
        "tz": scheduling.config.TIMEZONE,
        "gcal": gcal.status(),
        "g_error": _GERR.get(request.query_params.get("gerr")),
    }
    ctx.update(_sched_overview(events, week))
    return templates.TemplateResponse(request, "admin/scheduling.html", ctx)


@router.post("/availability")
async def save_availability(request: Request):
    """Replace the global weekly schedule from the 7-day form (idempotent).

    The console renders one switch per day; clicking a switch posts the current
    state (hidden on_/start_/end_ per day) plus toggle=<wd> for the day to flip."""
    form = await request.form()
    flip = (form.get("toggle") or "").strip()
    rows = []
    for wd in range(7):
        on = form.get(f"on_{wd}") == "1"
        if str(wd) == flip:
            on = not on
        if on:
            s = _hhmm_to_min(form.get(f"start_{wd}")) or 9 * 60
            e = _hhmm_to_min(form.get(f"end_{wd}")) or 17 * 60
            if e <= s:
                s, e = 9 * 60, 17 * 60
            rows.append((wd, s, e))
    with db.tx() as con:
        con.execute("DELETE FROM availability_rules WHERE event_type_id IS NULL")
        for wd, s, e in rows:
            con.execute(
                "INSERT INTO availability_rules (event_type_id,weekday,start_min,end_min) "
                "VALUES (NULL,?,?,?)",
                (wd, s, e),
            )
        audit.log(
            con, "availability", 0, "set_global", diff={"days": [WEEKDAYS[wd] for wd, _, _ in rows]}
        )
    return RedirectResponse("/admin/scheduling", status_code=303)


@router.post("/override")
async def add_override(
    request: Request,
    day: str = Form(...),
    mode: str = Form("block"),
    start: str = Form(""),
    end: str = Form(""),
):
    try:
        override_day = dt.date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date")
    if mode not in {"block", "hours"}:
        raise HTTPException(status_code=400, detail="bad override mode")
    if mode == "hours":
        s, e = _hhmm_to_min(start), _hhmm_to_min(end)
        if s is None or e is None or e <= s:
            raise HTTPException(status_code=400, detail="end must be after start")
        avail, smin, emin = 1, s, e
    else:
        avail, smin, emin = 0, None, None
    with db.tx() as con:
        previous = [
            {
                "day": row["day"],
                "available": row["available"],
                "start_min": row["start_min"],
                "end_min": row["end_min"],
            }
            for row in con.execute(
                """SELECT day, available, start_min, end_min FROM date_overrides
                   WHERE event_type_id IS NULL AND day IN (?,?) ORDER BY id""",
                (day, override_day),
            ).fetchall()
        ]
        con.execute(
            "DELETE FROM date_overrides WHERE event_type_id IS NULL AND day IN (?,?)",
            (day, override_day),
        )
        cur = con.execute(
            """INSERT INTO date_overrides (event_type_id,day,available,start_min,end_min)
                       VALUES (NULL,?,?,?,?)""",
            (override_day, avail, smin, emin),
        )
        audit.log(
            con,
            "date_override",
            cur.lastrowid,
            "set",
            diff={
                "previous": previous,
                "new": {
                    "day": override_day,
                    "available": avail,
                    "start_min": smin,
                    "end_min": emin,
                },
            },
        )
    return RedirectResponse("/admin/scheduling", status_code=303)


@router.post("/override/{override_id}/delete")
async def del_override(override_id: int):
    with db.tx() as con:
        con.execute(
            "DELETE FROM date_overrides WHERE id=? AND event_type_id IS NULL", (override_id,)
        )
        audit.log(con, "date_override", override_id, "delete")
    return RedirectResponse("/admin/scheduling", status_code=303)


# ── Google Calendar connection (OAuth) ───────────────────────────────────────

_STATE_COOKIE = "g_oauth_state"


@router.get("/google/connect")
async def google_connect(request: Request):
    """Kick off the OAuth consent flow. A random state is stashed in an HttpOnly
    cookie and echoed to Google, then re-checked on callback (CSRF defence)."""
    if not gcal.configured():
        raise HTTPException(status_code=400, detail="Google client id/secret not set")
    state = security.new_slug(24)
    resp = RedirectResponse(gcal.auth_url(state), status_code=303)
    security.set_session_cookie(resp, _STATE_COOKIE, state, max_age=600)
    return resp


@router.get("/google/callback")
async def google_callback(request: Request):
    """Consent return leg. Verify state, trade the code for a refresh token, and
    land back on the console with a success or error banner."""
    q = request.query_params
    cookie_state = request.cookies.get(_STATE_COOKIE)

    def _back(gerr: str | None = None):
        url = "/admin/scheduling" + (f"?gerr={gerr}" if gerr else "")
        r = RedirectResponse(url, status_code=303)
        security.delete_session_cookie(r, _STATE_COOKIE)
        return r

    if q.get("error"):
        return _back("denied")
    state = q.get("state") or ""
    if not cookie_state or not state or state != cookie_state:
        return _back("state")
    code = q.get("code") or ""
    if not code:
        return _back("exchange")
    try:
        gcal.exchange_code(code)
    except gcal.GcalError as e:
        log.warning("google oauth exchange failed: %s", e)
        return _back("exchange")
    with db.tx() as con:
        audit.log(con, "google_calendar", 1, "connect")
    return _back()


@router.post("/google/disconnect")
async def google_disconnect(request: Request):
    gcal.disconnect()
    with db.tx() as con:
        audit.log(con, "google_calendar", 1, "disconnect")
    return RedirectResponse("/admin/scheduling", status_code=303)


# ── event types ──────────────────────────────────────────────────────────────


@router.post("/event")
async def create_event(
    request: Request, name: str = Form(...), slug: str = Form(...), duration_min: int = Form(30)
):
    name, slug = name.strip(), slug.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug: lowercase letters, digits, hyphens")
    if db.one("SELECT 1 FROM event_types WHERE slug=?", (slug,)):
        raise HTTPException(status_code=400, detail="slug already in use")
    if not (5 <= duration_min <= 1440):
        raise HTTPException(status_code=400, detail="duration 5–1440 min")
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO event_types (slug,name,duration_min) VALUES (?,?,?)",
            (slug, name, duration_min),
        )
        audit.log(con, "event_type", cur.lastrowid, "create", diff={"slug": slug, "name": name})
        eid = cur.lastrowid
    return RedirectResponse(f"/admin/scheduling/event/{eid}", status_code=303)


@router.get("/event/{event_id}", response_class=HTMLResponse)
async def edit_event(request: Request, event_id: int):
    e = _get_event(event_id)
    return templates.TemplateResponse(
        request, "admin/scheduling_event.html", {"e": e, "base_url": scheduling.config.BASE_URL}
    )


@router.post("/event/{event_id}")
async def update_event(request: Request, event_id: int):
    e = _get_event(event_id)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    duration = _posint(form, "duration_min", 5, 1440, e["duration_min"])
    fields = {
        "name": name,
        "description": (form.get("description") or "").strip(),
        "duration_min": duration,
        "location": (form.get("location") or "").strip(),
        "color": (form.get("color") or "#b3552e").strip()[:9],
        "buffer_before_min": _posint(form, "buffer_before_min", 0, 480),
        "buffer_after_min": _posint(form, "buffer_after_min", 0, 480),
        "min_notice_hours": _posint(form, "min_notice_hours", 0, 8760, 12),
        "max_per_day": _posint(form, "max_per_day", 0, 50),
        "booking_window_days": _posint(form, "booking_window_days", 1, 365, 60),
        "slot_step_min": _posint(form, "slot_step_min", 0, 480),
        "position": _posint(form, "position", 0, 999),
        "creates_notion_session": 1 if form.get("creates_notion_session") else 0,
    }
    sets = ", ".join(f"{db.ident(k, _EVENT_COLS)}=?" for k in fields)
    with db.tx() as con:
        con.execute(f"UPDATE event_types SET {sets} WHERE id=?", (*fields.values(), event_id))
        audit.log(
            con,
            "event_type",
            event_id,
            "update",
            diff={k: [e[k], v] for k, v in fields.items() if e[k] != v},
        )
    return RedirectResponse(f"/admin/scheduling/event/{event_id}", status_code=303)


@router.post("/event/{event_id}/toggle")
async def toggle_event(event_id: int):
    e = _get_event(event_id)
    new = 0 if e["active"] else 1
    with db.tx() as con:
        con.execute("UPDATE event_types SET active=? WHERE id=?", (new, event_id))
        audit.log(con, "event_type", event_id, "activate" if new else "deactivate")
    return RedirectResponse("/admin/scheduling", status_code=303)


@router.post("/event/{event_id}/delete")
async def delete_event(event_id: int):
    e = _get_event(event_id)
    n = db.one("SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=?", (event_id,))
    if n["n"]:
        # Bookings reference this event — deactivate instead of orphaning history.
        raise HTTPException(
            status_code=400, detail="event has bookings; deactivate it instead of deleting"
        )
    with db.tx() as con:
        con.execute("DELETE FROM event_types WHERE id=?", (event_id,))
        audit.log(con, "event_type", event_id, "delete", diff={"slug": e["slug"]})
    return RedirectResponse("/admin/scheduling", status_code=303)


# ── bookings list + admin cancel ─────────────────────────────────────────────


@router.get("/bookings", response_class=HTMLResponse)
async def bookings(request: Request):
    upcoming = db.all_("""SELECT b.*, e.name AS event_name FROM bookings b
                          JOIN event_types e ON e.id=b.event_type_id
                          WHERE b.status='confirmed' AND b.start_utc >= datetime('now')
                          ORDER BY b.start_utc""")
    past = db.all_("""SELECT b.*, e.name AS event_name FROM bookings b
                      JOIN event_types e ON e.id=b.event_type_id
                      WHERE b.status!='confirmed' OR b.start_utc < datetime('now')
                      ORDER BY b.start_utc DESC LIMIT 100""")
    return templates.TemplateResponse(
        request,
        "admin/scheduling_bookings.html",
        {"upcoming": upcoming, "past": past, "tz": scheduling.config.TIMEZONE},
    )


@router.post("/booking/{booking_id}/cancel")
async def admin_cancel(booking_id: int):
    b = db.one("SELECT token FROM bookings WHERE id=?", (booking_id,))
    if not b:
        raise HTTPException(status_code=404)
    if scheduling.cancel(b["token"], "Cancelled by Kevin Lee Photography"):
        booking_notify.cancelled(booking_id, by_admin=True)
    return RedirectResponse("/admin/scheduling/bookings", status_code=303)
