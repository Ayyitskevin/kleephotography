"""One-way Plutus upsell hand-off after gallery analyze (Phase 1).

Mise POSTs mise_gallery_id to Plutus /recommend/mise-gallery. Failure is swallowed
in run_for_gallery so jobs never crash; the gallery row records last status for admin.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

log = logging.getLogger("mise.plutus")


class PlutusRecommendError(Exception):
    """Human-readable failure safe for admin UI."""


def is_enabled() -> bool:
    return bool(config.PLUTUS_URL and config.PLUTUS_TOKEN)


def _record(
    gallery_id: int,
    *,
    status: str,
    run_id: int | None = None,
    error: str | None = None,
) -> None:
    db.run(
        """UPDATE galleries SET plutus_last_run_id=?, plutus_last_status=?,
              plutus_last_error=?, plutus_last_at=datetime('now')
              WHERE id=?""",
        (run_id, status, (error or None)[:500] if error else None, gallery_id),
    )


def trigger_gallery_recommend(gallery_id: int) -> dict:
    if not is_enabled():
        raise PlutusRecommendError("Plutus is not configured")
    g = db.one(
        "SELECT id, published, type, argus_last_run_id FROM galleries WHERE id=?",
        (gallery_id,),
    )
    if not g:
        raise PlutusRecommendError(f"gallery {gallery_id} not found")
    if not g["published"]:
        raise PlutusRecommendError("gallery is not published")
    if g["type"] == "drop":
        raise PlutusRecommendError("transfers are not analyzed")

    fields: dict[str, str] = {"mise_gallery_id": str(gallery_id)}
    if g["argus_last_run_id"]:
        fields["argus_run_id"] = str(g["argus_last_run_id"])
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{config.PLUTUS_URL}/recommend/mise-gallery",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {config.PLUTUS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PLUTUS_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise PlutusRecommendError(
            f"Plutus returned HTTP {e.code}" + (f": {detail}" if detail else "")
        )
    except (urllib.error.URLError, TimeoutError) as e:
        reason = e.reason if hasattr(e, "reason") else e
        raise PlutusRecommendError(f"Plutus unreachable: {reason}")
    except (ValueError, json.JSONDecodeError):
        raise PlutusRecommendError("Plutus returned an unreadable response")

    if not isinstance(payload, dict) or not payload.get("run_id"):
        raise PlutusRecommendError("Plutus response missing run_id")

    log.info(
        "plutus recommend gallery %s -> run=%s bundles=%s",
        gallery_id,
        payload.get("run_id"),
        len(payload.get("bundles") or []),
    )
    return payload


def apply_callback(gallery_id: int, payload: dict) -> None:
    """Record Plutus hand-off result (from Mise job worker or Argus callback)."""
    status = (payload.get("status") or "done").strip()
    run_id = payload.get("run_id")
    error = payload.get("error")
    if status == "done" and run_id is not None:
        _record(gallery_id, status="done", run_id=int(run_id))
        return
    _record(
        gallery_id,
        status="error" if status != "done" else "done",
        run_id=int(run_id) if run_id is not None else None,
        error=str(error) if error else None,
    )


def run_for_gallery(gallery_id: int) -> None:
    if not is_enabled():
        log.info("plutus recommend skipped for %s (not configured)", gallery_id)
        return
    try:
        result = trigger_gallery_recommend(gallery_id)
    except PlutusRecommendError as e:
        log.warning("plutus recommend failed for gallery %s: %s", gallery_id, e)
        _record(gallery_id, status="error", error=str(e))
        return
    except Exception as e:
        log.exception("plutus recommend unexpected failure for gallery %s", gallery_id)
        _record(gallery_id, status="error", error=str(e)[:500])
        return

    _record(gallery_id, status="done", run_id=int(result["run_id"]))