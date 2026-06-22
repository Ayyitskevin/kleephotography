"""Best-effort security alerts to Telegram (direct Bot API sendMessage).

Dormant unless MISE_TELEGRAM_TOKEN + MISE_TELEGRAM_CHAT_ID are set in .env. Sending
is a one-shot HTTP POST — it never calls getUpdates, so it can NEVER conflict with
the single Telegram polling consumer (MickeyBot) elsewhere on the fleet. Fire-and-
forget on a daemon thread: a slow/down Telegram must never block or stall an auth
path, so failures are logged and swallowed. Alerts fire only on ANOMALIES (lockouts
after repeated failures) — never on a normal login or a deploy restart — to avoid
alert fatigue.
"""

import logging
import threading
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("mise.alerts")

# Throttle for crash alerts: a single bad request can fire on a tight retry loop,
# and an outage can crash every request — without a cooldown one bug becomes a
# Telegram flood. Key = error signature (path + exc type); we send at most one
# alert per signature per window, and report how many we swallowed since.
_ERROR_COOLDOWN = 600  # seconds
_error_last: dict[str, float] = {}
_error_suppressed: dict[str, int] = {}
_error_lock = threading.Lock()

# Throttle for operational heartbeat alerts (disk low, stale backup). Unlike a
# crash, these conditions PERSIST across many sweeps, so without a cooldown the
# hourly sweep would re-send the same warning every hour. 12h re-fires it at most
# twice a day while it's still broken — present without nagging.
_OPS_COOLDOWN = 12 * 3600  # seconds
_ops_last: dict[str, float] = {}
_ops_lock = threading.Lock()


def is_enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)


def _send(text: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:3800]}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=5) as r:
            r.read()
    except Exception as e:  # never let a notify failure surface into an auth path
        log.warning("security alert send failed: %s", e)


def security_alert(text: str) -> None:
    if not is_enabled():
        return
    threading.Thread(target=_send, args=(f"\U0001f510 Mise: {text}",),
                     daemon=True).start()


def error_alert(signature: str, text: str) -> None:
    """Fire-and-forget crash alert, throttled per signature (see _ERROR_COOLDOWN).

    `signature` groups identical crashes (e.g. "GET /foo|KeyError"); `text` is the
    human-readable body. Within a cooldown window only the first alert is sent; the
    rest are counted and reported on the next one so a storm collapses to a trickle.
    """
    if not is_enabled():
        return
    now = time.time()
    with _error_lock:
        last = _error_last.get(signature, 0.0)
        if now - last < _ERROR_COOLDOWN:
            _error_suppressed[signature] = _error_suppressed.get(signature, 0) + 1
            return
        suppressed = _error_suppressed.pop(signature, 0)
        _error_last[signature] = now
    if suppressed:
        text += f"\n(+{suppressed} more like this in the last "
        text += f"{_ERROR_COOLDOWN // 60} min, not shown)"
    threading.Thread(target=_send, args=(f"\U0001f4a5 Mise crash: {text}",),
                     daemon=True).start()


def ops_alert(signature: str, text: str) -> None:
    """Throttled outbound nudge for a PERSISTENT operational condition (low disk,
    stale backup). At most one message per signature per _OPS_COOLDOWN while the
    condition holds, so a sweep that keeps finding the same problem doesn't flood.
    """
    if not is_enabled():
        return
    now = time.time()
    with _ops_lock:
        if now - _ops_last.get(signature, 0.0) < _OPS_COOLDOWN:
            return
        _ops_last[signature] = now
    threading.Thread(target=_send, args=(f"\U0001f6df Mise ops: {text}",),
                     daemon=True).start()


def notify(text: str) -> None:
    """Fire-and-forget outbound nudge for a business event the CALLER has already
    de-duplicated (e.g. a one-shot DB flag), so no throttle is applied here. Used
    for internal nudges to Kevin — never a client-facing message."""
    if not is_enabled():
        return
    threading.Thread(target=_send, args=(f"\U0001f514 Mise: {text}",),
                     daemon=True).start()
