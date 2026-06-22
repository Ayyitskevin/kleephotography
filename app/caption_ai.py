"""Mesh client for Odysseus's caption-drafting brain (Domain G slice 6b).

Mise does NOT pick models or route — it hands context to Odysseus and takes back
one caption plus the model name Odysseus used. The call crosses the tailnet mesh,
so failure is EXPECTED, not exceptional: every failure mode (feature off, timeout,
connection refused, bad/empty response) raises CaptionDraftError, and the caller
leaves body/status untouched and writes nothing. There are no partial drafts.

The Odysseus-side endpoint (POST returning {"caption","model"}) is a separate,
independently-deployed change to the Odysseus CRM; until it exists this degrades
cleanly (MISE_ODYSSEUS_CAPTION_URL unset -> CaptionDraftError "not configured").
"""

import json
import logging
import urllib.error
import urllib.request

from . import config, features

log = logging.getLogger("mise.caption_ai")


class CaptionDraftError(Exception):
    """Any reason a draft could not be produced. Carries a human-readable message
    safe to surface in the admin UI (no secrets, no stack)."""


def is_enabled() -> bool:
    """AI drafting is armed only when BOTH the endpoint URL and the bearer token are
    configured. Either unset -> the "Draft with AI" button stays cleanly dormant
    (the route greys it out; a direct call raises CaptionDraftError, never crashes)."""
    return features.odysseus_caption_enabled()


def draft_caption(ctx: dict) -> dict:
    """Ask Odysseus to draft ONE caption from `ctx`. Returns {"caption", "model"}.

    Raises CaptionDraftError on every failure path so the route can leave the
    caption untouched. `ctx` is the drafting context (label, note, client, period
    …) — Odysseus shapes the prompt and selects the model."""
    if not is_enabled():
        raise CaptionDraftError("AI drafting is not configured")
    req = urllib.request.Request(
        config.ODYSSEUS_CAPTION_URL,
        method="POST",
        data=json.dumps(ctx).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.ODYSSEUS_CAPTION_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.ODYSSEUS_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise CaptionDraftError(f"Odysseus returned HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise CaptionDraftError(f"Odysseus unreachable: {e.reason if hasattr(e, 'reason') else e}")
    except (ValueError, json.JSONDecodeError):
        raise CaptionDraftError("Odysseus returned an unreadable response")
    caption = (payload.get("caption") or "").strip()
    if not caption:
        raise CaptionDraftError("Odysseus returned an empty draft")
    model = (payload.get("model") or "").strip() or "unknown"
    log.info("caption drafted via Odysseus (model=%s, %d chars)", model, len(caption))
    return {"caption": caption, "model": model}
