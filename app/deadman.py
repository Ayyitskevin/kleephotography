"""Dead-man's switch — the one alarm that does not share the host's fate.

Every other alarm this system has originates on the box being watched:
`alerts.py` needs the process alive to send, `ops_monitor.py` needs the
scheduler thread alive to check. So a powered-off host, a wedged container, a
dropped tunnel, or a process that died at 3am produces no message at all, and
silence reads exactly like health.

This inverts the direction. The host pings an external URL on a schedule and
the EXTERNAL service raises the alarm when the pings stop, so the absence of a
signal becomes the signal (R21). It is deliberately NOT gated on Telegram being
configured: an alarm that depends on the host's own alerting to work is the
thing this exists to compensate for.

Liveness is not correctness. A ping means "the loop ran", not "everything is
fine" — a failing check still routes through ops_monitor to Telegram. Keeping
the two channels separate is what stops a low-disk warning from also tripping
the "monitoring is dead" alarm.

Dormant unless a URL is configured, and unable to break its caller: the ping is
fire-and-forget on a daemon thread, because a slow or down monitoring service
must never stall the loop it is monitoring.
"""

import logging
import threading
import urllib.request

from . import config, features

log = logging.getLogger("mise.deadman")

# Short: this runs on a daemon thread, but a hung socket would still pin a
# thread for the whole window, once per tick, forever.
_TIMEOUT = 10


def is_enabled() -> bool:
    return features.deadman_enabled()


def _send(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:  # noqa: S310 - scheme checked below
            r.read()
    except Exception as e:
        # Log only. The external service is already about to notice the missing
        # ping — that is the design — and raising here would let a monitoring
        # outage take down the thing being monitored.
        log.warning("dead-man ping failed (%s): %s", url, e)


def ping(url: str | None = None) -> None:
    """Fire-and-forget liveness ping. Never raises, never blocks."""
    url = url if url is not None else config.HEARTBEAT_PING_URL
    if not url:
        return
    # urlopen honours file:// and ftp://; this value comes from .env, so the
    # exposure is small, but a monitoring URL is always http(s) and a typo that
    # silently reads a local file is worth refusing outright.
    if not url.startswith(("http://", "https://")):
        log.warning("dead-man ping URL ignored — not http(s): %r", url)
        return
    threading.Thread(target=_send, args=(url,), daemon=True).start()
