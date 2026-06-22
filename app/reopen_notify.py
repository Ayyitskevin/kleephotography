"""Fence-clean push when a client reply auto-reopens a resolved video-comment
thread (studio-notify-on-reopen).

Mise does NOT own the Telegram delivery — it POSTs a best-effort event to an
Odysseus endpoint (the caption_ai pattern) and Odysseus's existing bot path turns
it into a push. Mise has no hard dependency on Odysseus: both config vars unset ->
dormant, no outbound call; and EVERY failure mode (disabled, timeout, refused, bad
response) is swallowed and logged here, never raised. The reopen + its system audit
row are the durable record; this notification rides on top and is allowed to fail.

The Odysseus-side receiver (POST consuming the payload below, returning anything)
is a separate, independently-deployed change to the Odysseus CRM; until it exists
this stays dormant or logs a delivery failure — the reopen path is unaffected.

Payload contract (what Odysseus subscribes to):
    {"gallery_slug", "gallery_title", "asset_id", "root_id", "cause_reply_id"}
"""

import json
import logging
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("mise.reopen_notify")


def is_enabled() -> bool:
    """Armed only when BOTH the endpoint URL and the bearer token are configured.
    Either unset -> no outbound call is ever made."""
    return bool(config.REOPEN_NOTIFY_URL and config.REOPEN_NOTIFY_TOKEN)


def notify_reopen(payload: dict) -> bool:
    """Best-effort push of a reopen event to Odysseus. Returns True if the POST was
    accepted, False on any failure or when disabled. NEVER raises — a notification
    failure must not break the client comment response or the committed reopen."""
    if not is_enabled():
        return False
    req = urllib.request.Request(
        config.REOPEN_NOTIFY_URL,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.REOPEN_NOTIFY_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.REOPEN_NOTIFY_TIMEOUT) as resp:
            if resp.status >= 400:
                log.warning("reopen notify: Odysseus returned HTTP %s", resp.status)
                return False
    except urllib.error.HTTPError as e:
        log.warning("reopen notify: Odysseus returned HTTP %s", e.code)
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("reopen notify: Odysseus unreachable: %s", getattr(e, "reason", e))
        return False
    except Exception:
        log.exception("reopen notify: unexpected failure (non-fatal)")
        return False
    log.info(
        "reopen notify sent (gallery=%s asset=%s root=%s)",
        payload.get("gallery_slug"),
        payload.get("asset_id"),
        payload.get("root_id"),
    )
    return True
