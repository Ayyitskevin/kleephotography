"""Calendly-style scheduling engine — pure-ish logic over the scheduler tables.

Responsibilities:
  * generate open time slots for an event type on a given local day, honouring
    weekly availability, date overrides, duration, slot step, buffers, minimum
    notice, per-day cap, and the booking window;
  * claim a slot atomically so two concurrent visitors cannot double-book.

Time model (see migration 033): availability is authored in business-local
minutes-from-midnight; booking instants are stored UTC. All wall-clock -> UTC
conversion happens here via zoneinfo, so DST is correct without stored offsets.

The claim path uses an explicit ``BEGIN IMMEDIATE`` transaction: the write lock
is taken BEFORE the open-slot re-check, which is what makes the check-then-insert
race-safe under SQLite/WAL (a second writer blocks on the lock, then re-checks and
sees the slot gone). Never trust a slot the client submits — ``book`` re-derives
the day's open slots inside the transaction and rejects anything not in that set.
"""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import config, db, gcal, security

log = logging.getLogger("mise.scheduling")

_UTC = dt.UTC


class SlotTaken(Exception):
    """Raised when a slot is no longer bookable (gone, blocked, or out of policy)."""


class CalendarUnavailable(Exception):
    """Raised when Google Calendar is connected under strict policy but free/busy failed."""


def _biz_tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def _display_tz(name: str) -> ZoneInfo:
    """Visitor's tz for labels, falling back to business tz on anything invalid."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _biz_tz()


def _parse_utc(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)


def _fmt_utc(d: dt.datetime) -> str:
    return d.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S")


def now_utc() -> dt.datetime:
    return dt.datetime.now(_UTC)


# ── event-type lookups ───────────────────────────────────────────────────────


def active_event_types() -> list:
    return db.all_("SELECT * FROM event_types WHERE active=1 ORDER BY position, id")


def event_by_slug(slug: str):
    return db.one("SELECT * FROM event_types WHERE slug=? AND active=1", (slug,))


# ── availability windows for a local day ─────────────────────────────────────


def _windows_for_day(con, et, day: dt.date) -> list[tuple[int, int]]:
    """Return [(start_min, end_min), ...] of business-local availability for `day`.

    A date override (event-specific preferred over global) wins outright: a block
    yields []; a custom-hours override yields its single window. Otherwise the
    weekly rules for that weekday apply (event-specific preferred over global)."""
    iso = day.isoformat()
    ov = con.execute(
        """SELECT available, start_min, end_min FROM date_overrides
           WHERE day=? AND (event_type_id=? OR event_type_id IS NULL)
           ORDER BY event_type_id IS NULL LIMIT 1""",
        (iso, et["id"]),
    ).fetchone()
    if ov is not None:
        if not ov["available"] or ov["start_min"] is None or ov["end_min"] is None:
            return []
        return [(ov["start_min"], ov["end_min"])]

    wd = day.weekday()
    rows = con.execute(
        """SELECT start_min, end_min FROM availability_rules
           WHERE event_type_id=? AND weekday=? ORDER BY start_min""",
        (et["id"], wd),
    ).fetchall()
    if not rows:
        rows = con.execute(
            """SELECT start_min, end_min FROM availability_rules
               WHERE event_type_id IS NULL AND weekday=? ORDER BY start_min""",
            (wd,),
        ).fetchall()
    return [(r["start_min"], r["end_min"]) for r in rows]


def _overlaps(
    con, et, start_utc: dt.datetime, end_utc: dt.datetime, exclude_id: int | None
) -> bool:
    """True if the new booking collides with any confirmed booking. Both sides are
    padded by their OWN buffers (the new one in Python, existing ones in SQL), so a
    buffer protects travel/turnaround time symmetrically — a new slot can sit no
    closer than buffer minutes to an existing booking, regardless of event type."""
    lo = _fmt_utc(start_utc - dt.timedelta(minutes=et["buffer_before_min"]))
    hi = _fmt_utc(end_utc + dt.timedelta(minutes=et["buffer_after_min"]))
    row = con.execute(
        """SELECT COUNT(*) AS n FROM bookings b
           JOIN event_types e ON e.id=b.event_type_id
           WHERE b.status='confirmed'
             AND datetime(b.start_utc, '-'||e.buffer_before_min||' minutes') < ?
             AND datetime(b.end_utc,   '+'||e.buffer_after_min ||' minutes') > ?
             AND (? IS NULL OR b.id != ?)""",
        (hi, lo, exclude_id, exclude_id),
    ).fetchone()
    return row["n"] > 0


def _busy_conflict(
    start_utc: dt.datetime, end_utc: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]] | None
) -> bool:
    """True if [start_utc, end_utc) overlaps any busy interval already on Kevin's
    Google calendar. `busy` is None when the calendar isn't available (fail-open:
    hide nothing). Calendar buffers are intentionally NOT applied here — an
    external event blocks only its own wall-clock span, not the Mise turnaround."""
    if not busy:
        return False
    return any(bs < end_utc and be > start_utc for bs, be in busy)


def _day_count(con, et, day: dt.date, exclude_id: int | None = None) -> int:
    """Confirmed bookings for this event on this LOCAL day (cap accounting).

    A reschedule excludes only the booking being replaced."""
    tz = _biz_tz()
    start = dt.datetime.combine(day, dt.time(), tz).astimezone(_UTC)
    end = start + dt.timedelta(days=1)
    row = con.execute(
        """SELECT COUNT(*) AS n FROM bookings
           WHERE status='confirmed' AND event_type_id=?
             AND start_utc >= ? AND start_utc < ?
             AND (? IS NULL OR id != ?)""",
        (et["id"], _fmt_utc(start), _fmt_utc(end), exclude_id, exclude_id),
    ).fetchone()
    return row["n"]


def _slots_utc(
    con,
    et,
    day: dt.date,
    ref_utc: dt.datetime,
    busy: list[tuple[dt.datetime, dt.datetime]] | None = None,
    exclude_id: int | None = None,
) -> list[dt.datetime]:
    """Open slot start instants (UTC) for `day`, after all policy filters.

    `busy` (optional) is Google free/busy intervals for a range covering `day`;
    a slot that overlaps one is dropped so Mise never offers a time Kevin is
    already booked elsewhere. None = calendar unavailable -> no extra filtering.

    `exclude_id` releases only the original booking's local overlap and day-cap
    accounting while a replacement is claimed, but its current start is not
    offered as a replacement."""
    original_start = None
    if exclude_id is not None:
        original = con.execute(
            """SELECT start_utc FROM bookings
               WHERE id=? AND event_type_id=? AND status='confirmed'""",
            (exclude_id, et["id"]),
        ).fetchone()
        original_start = original["start_utc"] if original else None
    effective_exclude_id = exclude_id if original_start is not None else None
    today_local = ref_utc.astimezone(_biz_tz()).date()
    if day < today_local or (day - today_local).days > et["booking_window_days"]:
        return []
    if et["max_per_day"] and _day_count(con, et, day, effective_exclude_id) >= et["max_per_day"]:
        return []

    tz = _biz_tz()
    dur = dt.timedelta(minutes=et["duration_min"])
    step = et["slot_step_min"] or et["duration_min"]
    notice_cutoff = ref_utc + dt.timedelta(hours=et["min_notice_hours"])
    window_cutoff = ref_utc + dt.timedelta(days=et["booking_window_days"])
    midnight = dt.datetime.combine(day, dt.time(), tz)

    out: list[dt.datetime] = []
    for win_start, win_end in _windows_for_day(con, et, day):
        m = win_start
        while m + et["duration_min"] <= win_end:
            start_local = midnight + dt.timedelta(minutes=m)
            start_utc = start_local.astimezone(_UTC)
            end_utc = start_utc + dur
            if (
                start_utc >= notice_cutoff
                and start_utc <= window_cutoff
                and _fmt_utc(start_utc) != original_start
                and not _busy_conflict(start_utc, end_utc, busy)
                and not _overlaps(con, et, start_utc, end_utc, effective_exclude_id)
            ):
                out.append(start_utc)
            m += step
    return out


# ── public API ───────────────────────────────────────────────────────────────


def slots_for_day(
    et, day: dt.date, visitor_tz: str = "", exclude_id: int | None = None
) -> list[dict]:
    """Render-ready open slots for `day`: each item has the UTC value (form
    payload) plus a label in the visitor's timezone (falling back to business)."""
    disp = _display_tz(visitor_tz)
    tz = _biz_tz()
    day_start = dt.datetime.combine(day, dt.time(), tz).astimezone(_UTC)
    fb = gcal.free_busy(day_start, day_start + dt.timedelta(days=1))
    if fb.unavailable:
        return []
    con = db.connect()
    try:
        starts = _slots_utc(con, et, day, now_utc(), fb.intervals, exclude_id)
    finally:
        con.close()
    out = []
    for s in starts:
        local = s.astimezone(disp)
        out.append({"utc": _fmt_utc(s), "label": local.strftime("%-I:%M %p").lstrip("0")})
    return out


def days_with_slots(et, start_day: dt.date, n_days: int, exclude_id: int | None = None) -> set[str]:
    """ISO days in [start_day, start_day+n_days) that have at least one open slot —
    used to light up the month picker without an HTMX round-trip per day."""
    tz = _biz_tz()
    win_start = dt.datetime.combine(start_day, dt.time(), tz).astimezone(_UTC)
    win_end = dt.datetime.combine(start_day + dt.timedelta(days=n_days), dt.time(), tz).astimezone(
        _UTC
    )
    fb = gcal.free_busy(win_start, win_end)
    if fb.unavailable:
        return set()
    con = db.connect()
    try:
        ref = now_utc()
        return {
            d.isoformat()
            for i in range(n_days)
            for d in [start_day + dt.timedelta(days=i)]
            if _slots_utc(con, et, d, ref, fb.intervals, exclude_id)
        }
    finally:
        con.close()


def book(
    et,
    start_utc_str: str,
    name: str,
    email: str,
    phone: str,
    notes: str,
    visitor_tz: str,
    exclude_id: int | None = None,
) -> tuple[int, str]:
    """Atomically claim a slot. Returns (booking_id, manage_token).

    Raises SlotTaken if the submitted instant is not currently an open slot
    (gone to a race, blocked, out of notice/window, or never valid). Raises
    CalendarUnavailable when Google is connected under strict policy and
    free/busy cannot be verified. The open-set is re-derived inside a BEGIN
    IMMEDIATE transaction, so the decision and the insert are a single
    serialized unit. Google free/busy is fetched before the lock so a network
    stall never holds the write lock."""
    try:
        start_utc = _parse_utc(start_utc_str)
    except ValueError:
        raise SlotTaken("malformed time")
    end_utc = start_utc + dt.timedelta(minutes=et["duration_min"])
    day_local = start_utc.astimezone(_biz_tz()).date()
    token = security.new_slug(20)
    day_start = dt.datetime.combine(day_local, dt.time(), _biz_tz()).astimezone(_UTC)
    fb = gcal.free_busy(day_start, day_start + dt.timedelta(days=1))
    if fb.unavailable:
        raise CalendarUnavailable("google free/busy unavailable")

    con = db.connect()
    con.isolation_level = None  # take manual control of the transaction
    try:
        con.execute("BEGIN IMMEDIATE")
        ref = now_utc()
        open_starts = {
            _fmt_utc(s) for s in _slots_utc(con, et, day_local, ref, fb.intervals, exclude_id)
        }
        if start_utc_str not in open_starts:
            con.execute("ROLLBACK")
            raise SlotTaken("slot no longer available")
        cur = con.execute(
            """INSERT INTO bookings (token, event_type_id, name, email, phone,
                                     notes, start_utc, end_utc, tz, reschedule_of)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                token,
                et["id"],
                name,
                email,
                phone,
                notes,
                start_utc_str,
                _fmt_utc(end_utc),
                visitor_tz,
                exclude_id,
            ),
        )
        bid = cur.lastrowid
        con.execute("COMMIT")
        return bid, token
    except (SlotTaken, CalendarUnavailable):
        raise
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def booking_by_token(token: str):
    return db.one(
        """SELECT b.*, e.name AS event_name, e.slug AS event_slug,
                  e.duration_min, e.location, e.min_notice_hours
           FROM bookings b JOIN event_types e ON e.id=b.event_type_id
           WHERE b.token=?""",
        (token,),
    )


def cancel(token: str, reason: str = "") -> bool:
    """Cancel a confirmed booking. Returns True only if a row actually flipped
    (so a double-click or stale link cannot fire two cancellations)."""
    con = db.connect()
    try:
        cur = con.execute(
            """UPDATE bookings SET status='cancelled', cancel_reason=?,
                      cancelled_at=datetime('now')
               WHERE token=? AND status='confirmed'""",
            (reason, token),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()
