"""One-way "arm a deferred reminder" push to Hermes (flow :7020).

Hermes owns the persistent, late-safe, precise-time reminder engine; Mise owns the
events. At an event instant Mise pushes an arm request and Hermes schedules a single
owner-facing Telegram nudge for `when`, dedup'd by `key` so a job retry or a re-scan
can never double-arm. One-way (R-doctrine): Mise never reads back, never syncs.

Dormant unless MISE_HERMES_ARM_URL is set. The POST is synchronous with a short
timeout but NEVER raises — a slow or down Hermes returns False and is logged, so it
can't fail a delivery job or stall the recurring sweep. Callers that need
at-least-once delivery (the post-shoot sweep) gate a DB flag on the True return and
retry on False; best-effort callers (gallery delivery) ignore the result and lean on
Hermes's key dedup to stay one-shot across job retries.
"""

import datetime as dt
import json
import logging
import urllib.request
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("mise.hermes_arm")


def is_enabled() -> bool:
    return bool(config.HERMES_ARM_URL)


def at_9am(days_ahead: int) -> str:
    """ISO8601 (offset-aware) for 09:00 business-tz, `days_ahead` from today — a
    civil hour for an owner nudge instead of whatever instant the event fired at."""
    tz = ZoneInfo(config.TIMEZONE)
    target = (dt.datetime.now(tz) + dt.timedelta(days=days_ahead)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    return target.isoformat()


def arm(key: str, text: str, when: str) -> bool:
    """Push one deferred reminder to Hermes. Returns True on a 2xx, False on any
    failure (including a disabled net). Never raises."""
    if not is_enabled():
        return False
    body = json.dumps({"key": key, "text": text, "when": when}).encode()
    headers = {"Content-Type": "application/json"}
    if config.HERMES_ARM_TOKEN:
        headers["Authorization"] = f"Bearer {config.HERMES_ARM_TOKEN}"
    req = urllib.request.Request(config.HERMES_ARM_URL, data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception as e:
        log.warning("hermes arm failed key=%s: %s", key, e)
        return False
