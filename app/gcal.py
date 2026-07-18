"""Google Calendar integration (Phase B) — OAuth + free/busy + event sync.

Single business account, self-hosted: the OAuth web-app creds live in .env, and the
long-lived refresh token (obtained once via the admin Connect flow) is stored in the
google_oauth table. Access tokens are short-lived and minted on demand.

Two jobs:
  * free/busy — hide booking slots that collide with events already on Kevin's
    calendar, so Mise and Google never double-book him;
  * event sync — create/move/delete the matching calendar event as a booking is
    confirmed / rescheduled / cancelled.

Default availability policy is fail-open: if Google is unreachable or not
connected, free/busy returns intervals=None (no slots hidden) and event sync is
a no-op. Set MISE_GCAL_AVAILABILITY_STRICT=true to fail-closed when connected —
API failures then mark the query unavailable so callers hide slots / reject
bookings. Event sync remains best-effort either way. Uses urllib (stdlib),
matching notion_sync — no new runtime dependency. The refresh token is never
logged.
"""

import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import config, db

log = logging.getLogger("mise.gcal")
_UTC = dt.UTC

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
# Least-privilege: read busy intervals + read/write events. No full-calendar scope.
SCOPES = (
    "https://www.googleapis.com/auth/calendar.freebusy "
    "https://www.googleapis.com/auth/calendar.events"
)


class GcalError(Exception):
    """Calendar API/OAuth failure — callers treat per availability policy."""


class FreeBusyQuery(NamedTuple):
    """Result of a free/busy lookup.

    intervals=None means "do not filter on Google" (not provisioned, not
    connected, or fail-open after an error). unavailable=True means the
    calendar is connected, strict mode is on, and the API failed — callers
    must not offer or accept slots until Google answers again.
    """

    intervals: list[tuple[dt.datetime, dt.datetime]] | None
    unavailable: bool = False


# ── small helpers ────────────────────────────────────────────────────────────


def _parse(utc_str: str) -> dt.datetime:
    return dt.datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)


def _rfc3339(d: dt.datetime) -> str:
    return d.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_UTC)


def _expiry_str(expires_in) -> str:
    secs = int(expires_in or 0)
    return (dt.datetime.now(_UTC) + dt.timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")


def _now_plus(skew_secs: int) -> str:
    return (dt.datetime.now(_UTC) + dt.timedelta(seconds=skew_secs)).strftime("%Y-%m-%d %H:%M:%S")


def _cal() -> str:
    return urllib.parse.quote(config.GOOGLE_CALENDAR_ID, safe="")


# ── connection state ─────────────────────────────────────────────────────────


def configured() -> bool:
    """OAuth client creds present in .env — the feature is provisioned."""
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _row():
    return db.one("SELECT * FROM google_oauth WHERE id=1")


def is_connected() -> bool:
    r = _row()
    return bool(r and r["refresh_token"])


def status() -> dict:
    r = _row()
    return {
        "configured": configured(),
        "connected": bool(r and r["refresh_token"]),
        "connected_at": r["connected_at"] if r else None,
        "calendar_id": config.GOOGLE_CALENDAR_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
    }


# ── OAuth flow ───────────────────────────────────────────────────────────────


def auth_url(state: str) -> str:
    """Google consent URL. access_type=offline + prompt=consent guarantees a
    refresh token is issued (even on re-connect)."""
    q = urllib.parse.urlencode(
        {
            "client_id": config.GOOGLE_CLIENT_ID,
            "redirect_uri": config.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{q}"


def _token_call(payload: dict) -> dict:
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Google returns JSON {error, error_description}; never echo secrets.
        raise GcalError(f"token endpoint {e.code}") from None
    except Exception as e:
        raise GcalError(f"token endpoint unreachable: {e}") from None


def exchange_code(code: str) -> None:
    """Trade the auth code for tokens and persist the refresh token."""
    tok = _token_call(
        {
            "code": code,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": config.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    )
    refresh = tok.get("refresh_token")
    if not refresh:
        raise GcalError(
            "no refresh_token returned — revoke Mise's access in your "
            "Google account and reconnect to force re-consent"
        )
    db.run(
        """INSERT INTO google_oauth (id, refresh_token, access_token, access_expiry,
                                     scope, connected_at)
           VALUES (1,?,?,?,?,datetime('now'))
           ON CONFLICT(id) DO UPDATE SET
             refresh_token=excluded.refresh_token,
             access_token=excluded.access_token,
             access_expiry=excluded.access_expiry,
             scope=excluded.scope,
             connected_at=datetime('now')""",
        (
            refresh,
            tok.get("access_token", ""),
            _expiry_str(tok.get("expires_in")),
            tok.get("scope", ""),
        ),
    )
    log.info("google calendar connected")


def disconnect() -> None:
    db.run("DELETE FROM google_oauth WHERE id=1")
    log.info("google calendar disconnected")


def _access_token() -> str:
    r = _row()
    if not r or not r["refresh_token"]:
        raise GcalError("not connected")
    if r["access_token"] and r["access_expiry"] and r["access_expiry"] > _now_plus(60):
        return r["access_token"]
    tok = _token_call(
        {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "refresh_token": r["refresh_token"],
            "grant_type": "refresh_token",
        }
    )
    access = tok.get("access_token")
    if not access:
        raise GcalError("refresh produced no access token")
    db.run(
        "UPDATE google_oauth SET access_token=?, access_expiry=? WHERE id=1",
        (access, _expiry_str(tok.get("expires_in"))),
    )
    return access


def _api(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


# ── free/busy ────────────────────────────────────────────────────────────────


def free_busy(time_min: dt.datetime, time_max: dt.datetime) -> FreeBusyQuery:
    """Busy intervals on the business calendar for [time_min, time_max).

    Not configured / not connected → intervals=None (no Google filtering).
    API success → intervals=list (possibly empty).
    API failure → fail-open (intervals=None) unless GCAL_AVAILABILITY_STRICT,
    which returns unavailable=True so booking hides/rejects slots."""
    if not configured() or not is_connected():
        return FreeBusyQuery(intervals=None, unavailable=False)
    try:
        res = _api(
            "POST",
            "/freeBusy",
            {
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
                "items": [{"id": config.GOOGLE_CALENDAR_ID}],
            },
        )
        busy = res.get("calendars", {}).get(config.GOOGLE_CALENDAR_ID, {}).get("busy", [])
        intervals = [(_parse_rfc3339(b["start"]), _parse_rfc3339(b["end"])) for b in busy]
        return FreeBusyQuery(intervals=intervals, unavailable=False)
    except Exception as e:
        if config.GCAL_AVAILABILITY_STRICT:
            log.warning("freebusy failed under strict policy — blocking slots: %s", e)
            return FreeBusyQuery(intervals=None, unavailable=True)
        log.warning("freebusy failed, treating as no conflicts: %s", e)
        return FreeBusyQuery(intervals=None, unavailable=False)


# ── event sync (booking lifecycle) ───────────────────────────────────────────


def _event_body(b) -> dict:
    parts = [f"Email: {b['email']}", f"Phone: {b['phone'] or '—'}"]
    if b["notes"]:
        parts.append(f"\n{b['notes']}")
    parts.append(f"\nManage: {config.BASE_URL}/booking/{b['token']}")
    return {
        "summary": f"{b['event_name']} · {b['name']}",
        "description": "\n".join(parts),
        "location": b["event_location"] or "",
        "start": {"dateTime": _rfc3339(_parse(b["start_utc"]))},
        "end": {"dateTime": _rfc3339(_parse(b["end_utc"]))},
    }


def _delete_event(booking_id: int) -> None:
    b = db.one("SELECT google_event_id FROM bookings WHERE id=?", (booking_id,))
    if not b or not b["google_event_id"]:
        return
    try:
        _api("DELETE", f"/calendars/{_cal()}/events/{b['google_event_id']}")
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410):  # already gone is fine
            log.warning("gcal delete for booking %s failed: %s", booking_id, e.code)
    except Exception as e:
        log.warning("gcal delete for booking %s failed: %s", booking_id, e)
    db.run("UPDATE bookings SET google_event_id=NULL WHERE id=?", (booking_id,))


def on_booking_confirmed(booking_id: int) -> None:
    """Create (or update) the calendar event for a confirmed booking. If this is a
    reschedule, the superseded booking's event is removed first."""
    if not configured() or not is_connected():
        return
    b = db.one(
        """SELECT b.*, e.name AS event_name, e.location AS event_location
           FROM bookings b JOIN event_types e ON e.id=b.event_type_id
           WHERE b.id=? AND b.status='confirmed'""",
        (booking_id,),
    )
    if not b:
        return
    try:
        if b["reschedule_of"]:
            _delete_event(b["reschedule_of"])
        body = _event_body(b)
        event_id = b["google_event_id"]
        if event_id:
            try:
                _api("PATCH", f"/calendars/{_cal()}/events/{event_id}", body)
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 410):
                    raise
                log.info("gcal event %s vanished; recreating for booking %s", event_id, booking_id)
                db.run("UPDATE bookings SET google_event_id=NULL WHERE id=?", (booking_id,))
                event_id = None
        if not event_id:
            ev = _api("POST", f"/calendars/{_cal()}/events", body)
            if ev.get("id"):
                db.run("UPDATE bookings SET google_event_id=? WHERE id=?", (ev["id"], booking_id))
        log.info("gcal event synced for booking %s", booking_id)
    except Exception as e:
        log.warning("gcal event sync for booking %s failed: %s", booking_id, e)


def on_booking_cancelled(booking_id: int) -> None:
    """Remove the calendar event when a booking is cancelled."""
    if not configured() or not is_connected():
        return
    _delete_event(booking_id)
