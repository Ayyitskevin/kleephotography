"""iCalendar (.ics) invites + an 'Add to Google Calendar' link.

No Google account or OAuth needed — the .ics is a standards file every calendar
app (Google, Apple, Outlook) imports, and the Google link is just a prefilled
TEMPLATE URL. This is the Phase-A integration; Phase B (the Google Calendar API
that reads free/busy and writes events automatically) is gated on Kevin's OAuth
credentials and lives elsewhere.
"""

import datetime as dt
from urllib.parse import urlencode

from . import config


def _compact(utc_str: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> '20260618T143000Z' iCal basic-format UTC."""
    return dt.datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def uid_for(booking_id: int) -> str:
    return f"mise-booking-{booking_id}@kleephotography.com"


def build(
    *,
    uid: str,
    summary: str,
    description: str,
    location: str,
    start_utc: str,
    end_utc: str,
    organizer_email: str,
    attendee_email: str,
    sequence: int = 0,
    cancelled: bool = False,
) -> str:
    """Return a complete VCALENDAR string. method=CANCEL + STATUS:CANCELLED when
    `cancelled`, so an importing client removes the held slot instead of duplicating
    it. SEQUENCE must increase across updates for clients to honour the change."""
    method = "CANCEL" if cancelled else "REQUEST"
    status = "CANCELLED" if cancelled else "CONFIRMED"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mise//Scheduler//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_compact(start_utc)}",
        f"DTEND:{_compact(end_utc)}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(description)}",
        f"LOCATION:{_esc(location)}",
        f"ORGANIZER;CN={_esc(config.SITE_NAME)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={_esc(attendee_email)};RSVP=TRUE:mailto:{attendee_email}",
        f"STATUS:{status}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def google_link(*, summary: str, details: str, location: str, start_utc: str, end_utc: str) -> str:
    """Prefilled Google Calendar 'add event' URL (no login state required)."""
    q = urlencode(
        {
            "action": "TEMPLATE",
            "text": summary,
            "dates": f"{_compact(start_utc)}/{_compact(end_utc)}",
            "details": details,
            "location": location,
        }
    )
    return f"https://calendar.google.com/calendar/render?{q}"
