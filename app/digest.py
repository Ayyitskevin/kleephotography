"""Weekly owner digest — the Monday-morning "what did the machine do last week"
email to Kevin, fired off the recurring sweeper like every other retainer.

Each hourly pass asks: is last week's digest owed, and is it past DIGEST_HOUR
local on (or after) Monday? Send once, stamp digest_log. The stamp is written
only after a successful send, so a mail hiccup retries next pass, and a host
asleep at 7am catches up later that week. A week whose digest never went out
before the next week starts is skipped, not backfilled — a nine-day-old summary
is noise. Numbers are computed on business-timezone week boundaries (local
Monday 00:00 → Monday 00:00) converted to UTC to match the datetime('now')
columns they query.

This is the business-level dead-man switch: ops_monitor notices when the
machine breaks; the digest is how Kevin notices when a flywheel quietly does
nothing all week."""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import announcements, config, db, features, gcal, mailer

log = logging.getLogger("mise.digest")
_UTC = dt.UTC
_FMT = "%Y-%m-%d %H:%M:%S"


def _tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def _week_utc(week_start: dt.date) -> tuple[str, str]:
    """Local [Monday 00:00, next Monday 00:00) as UTC strings for range queries."""
    lo = dt.datetime.combine(week_start, dt.time(), tzinfo=_tz()).astimezone(_UTC)
    hi = dt.datetime.combine(week_start + dt.timedelta(days=7), dt.time(), tzinfo=_tz()).astimezone(
        _UTC
    )
    return lo.strftime(_FMT), hi.strftime(_FMT)


def _money_cents(lo: str, hi: str) -> tuple[int, int, int]:
    """(total, invoice, reservation) cents received in [lo, hi)."""
    inv = db.one(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM payments"
        " WHERE created_at >= ? AND created_at < ?",
        (lo, hi),
    )["c"]
    res = db.one(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM booking_payments"
        " WHERE created_at >= ? AND created_at < ?",
        (lo, hi),
    )["c"]
    return inv + res, inv, res


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _count(sql: str, lo: str, hi: str) -> int:
    return db.one(sql, (lo, hi))["c"]


def build(week_start: dt.date) -> tuple[str, str, int]:
    """(subject, body, audience_count) for the week starting `week_start`."""
    lo, hi = _week_utc(week_start)
    prev_lo, prev_hi = _week_utc(week_start - dt.timedelta(days=7))
    week_end = week_start + dt.timedelta(days=6)
    label = f"{week_start.strftime('%b %-d')} – {week_end.strftime('%b %-d')}"

    total, inv, res = _money_cents(lo, hi)
    prev_total, _, _ = _money_cents(prev_lo, prev_hi)
    money = f"Payments received: {_usd(total)} (week before: {_usd(prev_total)})"
    if inv and res:
        money += f"\n  invoices {_usd(inv)} · reservations {_usd(res)}"

    lines = [f"Week of {label}\n", money, ""]

    booked = db.all_(
        """SELECT e.name AS event_name, COUNT(*) AS c
             FROM bookings b JOIN event_types e ON e.id=b.event_type_id
            WHERE b.created_at >= ? AND b.created_at < ?
              AND b.status IN ('confirmed','pending_payment')
            GROUP BY e.name ORDER BY c DESC""",
        (lo, hi),
    )
    cancelled = _count(
        "SELECT COUNT(*) AS c FROM bookings WHERE cancelled_at >= ? AND cancelled_at < ?", lo, hi
    )
    if booked:
        lines.append(f"Bookings made: {sum(r['c'] for r in booked)}")
        lines += [f"  {r['event_name']}: {r['c']}" for r in booked]
    else:
        lines.append("Bookings made: 0")
    if cancelled:
        lines.append(f"Cancellations: {cancelled}")
    lines.append("")

    inquiries = db.all_(
        """SELECT COALESCE(NULLIF(TRIM(referral_source), ''), 'unattributed') AS src,
                  COUNT(*) AS c
             FROM inquiries WHERE created_at >= ? AND created_at < ?
            GROUP BY src ORDER BY c DESC""",
        (lo, hi),
    )
    n_inq = sum(r["c"] for r in inquiries)
    lines.append(f"New inquiries: {n_inq}")
    if n_inq:
        lines.append("  " + " · ".join(f"{r['src']} {r['c']}" for r in inquiries))
    lines.append("")

    asks = _count(
        "SELECT COUNT(*) AS c FROM galleries"
        " WHERE review_requested_at >= ? AND review_requested_at < ?",
        lo,
        hi,
    )
    nudges = _count(
        "SELECT COUNT(*) AS c FROM clients"
        " WHERE anniversary_nudged_at >= ? AND anniversary_nudged_at < ?",
        lo,
        hi,
    )
    unsubs = _count(
        "SELECT COUNT(*) AS c FROM unsubscribes WHERE created_at >= ? AND created_at < ?", lo, hi
    )
    audience_count = len(announcements.audience())
    prev_row = db.one(
        "SELECT audience_count FROM digest_log WHERE week_start=?",
        ((week_start - dt.timedelta(days=7)).isoformat(),),
    )
    growth = ""
    if prev_row and prev_row["audience_count"] is not None:
        delta = audience_count - prev_row["audience_count"]
        growth = f" ({delta:+d} since last digest)"
    lines += [
        f"Review asks sent: {asks}",
        f"Anniversary nudges: {nudges}",
        f"Email audience: {audience_count}{growth}"
        + (f" · {unsubs} unsubscribed" if unsubs else ""),
        "",
    ]

    ahead_lo, ahead_hi = _week_utc(week_start + dt.timedelta(days=7))
    upcoming = db.all_(
        """SELECT b.name, b.start_utc, e.name AS event_name
             FROM bookings b JOIN event_types e ON e.id=b.event_type_id
            WHERE b.status='confirmed' AND b.start_utc >= ? AND b.start_utc < ?
            ORDER BY b.start_utc""",
        (ahead_lo, ahead_hi),
    )
    lines.append(f"This week: {len(upcoming)} session{'s' if len(upcoming) != 1 else ''}")
    for b in upcoming:
        local = dt.datetime.strptime(b["start_utc"], _FMT).replace(tzinfo=_UTC).astimezone(_tz())
        when = local.strftime("%a %b %-d · %-I:%M %p").replace(" 0", " ")
        lines.append(f"  {when} — {b['event_name']} · {b['name']}")

    dormant = []
    if not features.review_engine_enabled():
        dormant.append("review engine")
    if not features.sms_enabled():
        dormant.append("SMS")
    if not (gcal.configured() and gcal.is_connected()):
        dormant.append("Google Calendar")
    if dormant:
        lines += ["", f"Dormant channels: {', '.join(dormant)}"]

    lines += ["", f"Reports: {config.BASE_URL}/admin/reports"]
    return f"Mise weekly — {label}", "\n".join(lines) + "\n", audience_count


def sweep() -> None:
    """Send the owed weekly digest, if any. Stamp only after a successful send."""
    if not config.WEEKLY_DIGEST or not mailer.configured():
        return
    local = dt.datetime.now(tz=_UTC).astimezone(_tz())
    this_monday = local.date() - dt.timedelta(days=local.weekday())
    if local.date() == this_monday and local.hour < config.DIGEST_HOUR:
        return
    week_start = this_monday - dt.timedelta(days=7)
    if db.one("SELECT 1 AS x FROM digest_log WHERE week_start=?", (week_start.isoformat(),)):
        return
    subject, body, audience_count = build(week_start)
    try:
        mailer.send(config.GMAIL_USER, subject, body, reply_to=config.GMAIL_USER)
    except Exception:
        log.exception("weekly digest send failed")
        return
    db.run(
        "INSERT INTO digest_log (week_start, audience_count) VALUES (?,?)",
        (week_start.isoformat(), audience_count),
    )
    log.info("weekly digest sent for week of %s", week_start)
