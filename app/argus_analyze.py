"""One-way Argus vision hand-off on gallery publish (Phase 6).

Mise POSTs mise_gallery_id to Argus /analyze-folder; Argus resolves originals via
ARGUS_MISE_MEDIA_ROOT on its host. Failure is EXPECTED on the mesh — every failure
path is swallowed in run_for_gallery so publish and background jobs never crash; the
gallery row records the last status for admin surfacing.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, platekit, plutus_recommend

log = logging.getLogger("mise.argus")


class ArgusAnalyzeError(Exception):
    """Human-readable failure safe for admin UI (no secrets)."""


def is_enabled() -> bool:
    """Armed only when BOTH Argus URL and bearer token are configured."""
    return bool(config.ARGUS_URL and config.ARGUS_TOKEN)


def _callback_url(gallery_id: int) -> str | None:
    """Tailnet callback for queued Argus jobs to update gallery status."""
    if not config.BASE_URL:
        return None
    return f"{config.BASE_URL.rstrip('/')}/api/argus/callback?gallery_id={gallery_id}"


def apply_callback(gallery_id: int, payload: dict) -> None:
    """Best-effort status update from Argus job completion webhook."""
    status = (payload.get("status") or "").strip()
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    run_id = payload.get("run_id") or result.get("run_id")
    job_id = payload.get("job_id")
    error = payload.get("error")

    if status == "done" or run_id:
        _record(gallery_id, status="done", run_id=run_id, job_id=job_id)
        if run_id:
            try:
                platekit.notify_argus_complete(gallery_id, int(run_id))
            except Exception:
                log.exception("platekit argus hook failed for gallery %s", gallery_id)
        if plutus_recommend.is_enabled():
            from . import jobs

            jobs.enqueue("plutus_recommend_gallery", {"gallery_id": gallery_id})
    elif status == "queued":
        _record(gallery_id, status="queued", job_id=job_id)
    elif status in ("dead_letter", "failed"):
        _record(gallery_id, status="error", run_id=run_id, job_id=job_id,
                error=(error or f"Argus job {status}")[:500])
    else:
        log.info("argus callback ignored for gallery %s status=%s", gallery_id, status)


def _record(gallery_id: int, *, status: str, run_id: int | None = None,
            job_id: str | None = None, error: str | None = None) -> None:
    db.run("""UPDATE galleries SET argus_last_run_id=?, argus_last_job_id=?,
              argus_last_status=?, argus_last_error=?, argus_last_at=datetime('now')
              WHERE id=?""",
           (run_id, job_id, status, (error or None)[:500] if error else None, gallery_id))


def trigger_gallery_analyze(gallery_id: int, *, skip_dedup: bool = False) -> dict:
    """POST /analyze-folder for one published gallery. Returns Argus JSON body."""
    if not is_enabled():
        raise ArgusAnalyzeError("Argus is not configured")
    g = db.one("SELECT id, published, type, project_id FROM galleries WHERE id=?",
               (gallery_id,))
    if not g:
        raise ArgusAnalyzeError(f"gallery {gallery_id} not found")
    if not g["published"]:
        raise ArgusAnalyzeError("gallery is not published")
    if g["type"] == "drop":
        raise ArgusAnalyzeError("transfers are not analyzed")

    fields = {
        "mise_gallery_id": gallery_id,
        "limit": config.ARGUS_ANALYZE_LIMIT,
        "source": "mise",
    }
    if skip_dedup:
        fields["skip_dedup"] = "true"
    callback = _callback_url(gallery_id)
    if callback:
        fields["callback_url"] = callback
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{config.ARGUS_URL}/analyze-folder",
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {config.ARGUS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.ARGUS_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        raise ArgusAnalyzeError(f"Argus returned HTTP {e.code}" + (f": {detail}" if detail else ""))
    except (urllib.error.URLError, TimeoutError) as e:
        reason = e.reason if hasattr(e, "reason") else e
        raise ArgusAnalyzeError(f"Argus unreachable: {reason}")
    except (ValueError, json.JSONDecodeError):
        raise ArgusAnalyzeError("Argus returned an unreadable response")

    if not isinstance(payload, dict):
        raise ArgusAnalyzeError("Argus returned an unexpected response")

    run_id = payload.get("run_id")
    job_id = payload.get("job_id")
    if run_id is None and job_id is None:
        raise ArgusAnalyzeError("Argus response missing run_id and job_id")

    mode = payload.get("mode") or ("queued" if job_id else "sync")
    log.info("argus analyze gallery %s -> mode=%s run=%s job=%s",
             gallery_id, mode, run_id, job_id)
    return payload


def run_for_gallery(gallery_id: int, *, skip_dedup: bool = False) -> None:
    """Background job entry — never raises; records status on the gallery row."""
    if not is_enabled():
        log.info("argus analyze skipped for %s (not configured)", gallery_id)
        return
    try:
        result = trigger_gallery_analyze(gallery_id, skip_dedup=skip_dedup)
    except ArgusAnalyzeError as e:
        log.warning("argus analyze failed for gallery %s: %s", gallery_id, e)
        _record(gallery_id, status="error", error=str(e))
        return
    except Exception as e:
        log.exception("argus analyze unexpected failure for gallery %s", gallery_id)
        _record(gallery_id, status="error", error=str(e)[:500])
        return

    run_id = result.get("run_id")
    job_id = result.get("job_id")
    if job_id:
        status = "queued"
    elif run_id:
        status = "done"
    else:
        status = "error"
        _record(gallery_id, status=status, error="missing run_id and job_id")
        return
    _record(gallery_id, status=status, run_id=run_id, job_id=job_id)