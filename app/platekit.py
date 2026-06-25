"""Platekit/Dionysus bridge — Mise operator service (no public SaaS signup).

Reads approved packs and triggers argus-pack drafts after Argus vision completes.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

log = logging.getLogger("mise.platekit")

_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def is_enabled() -> bool:
    return bool(config.PLATEKIT_API_BASE and config.PLATEKIT_API_TOKEN)


def normalize_slug(value: str) -> str:
    return _SLUG_CHARS.sub("-", (value or "").strip().lower()).strip("-")


def slug_for_client(client) -> str:
    explicit = (
        normalize_slug(client["platekit_slug"] or "") if "platekit_slug" in client.keys() else ""
    )
    if explicit:
        return explicit
    base = (client["company"] or client["name"] or "").strip()
    return normalize_slug(base)


def _empty(*, slug: str, status: str, message: str, enabled: bool | None = None) -> dict:
    return {
        "enabled": is_enabled() if enabled is None else enabled,
        "slug": slug,
        "status": status,
        "message": message,
        "packs": [],
    }


def _record(
    gallery_id: int,
    *,
    status: str,
    job_id: str | None = None,
    pack_id: int | None = None,
    error: str | None = None,
) -> None:
    db.run(
        """UPDATE galleries SET platekit_last_job_id=?, platekit_last_pack_id=?,
              platekit_last_status=?, platekit_last_error=?, platekit_last_at=datetime('now')
              WHERE id=?""",
        (
            job_id,
            pack_id,
            status,
            (error or None)[:500] if error else None,
            gallery_id,
        ),
    )


def packs_for_client(client, *, include_drafts: bool = False) -> dict:
    slug = slug_for_client(client)
    if not is_enabled():
        return _empty(
            slug=slug,
            status="not_configured",
            message="Platekit service is not configured",
            enabled=False,
        )
    if not slug:
        return _empty(
            slug=slug,
            status="missing_slug",
            message="Set a Platekit slug on this client (e.g. blue-plate)",
        )

    base = config.PLATEKIT_API_BASE.rstrip("/")
    qs = urllib.parse.urlencode({"include_drafts": "true"}) if include_drafts else ""
    url = f"{base}/api/mise/organizations/{urllib.parse.quote(slug)}/packs"
    if qs:
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.PLATEKIT_API_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PLATEKIT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _empty(
                slug=slug,
                status="not_found",
                message=f"No Platekit org '{slug}' — run seed-demo on Dionysus",
            )
        log.warning("Platekit returned HTTP %s for slug=%s", e.code, slug)
        return _empty(slug=slug, status="error", message=f"Platekit returned HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Platekit unreachable for slug=%s: %s", slug, e)
        return _empty(slug=slug, status="error", message="Platekit is unreachable")
    except (ValueError, json.JSONDecodeError):
        return _empty(slug=slug, status="error", message="Platekit returned an unreadable response")

    return {
        "enabled": True,
        "slug": slug,
        "status": "ok",
        "message": "",
        "packs": payload.get("packs") or [],
    }


def notify_argus_complete(gallery_id: int, run_id: int) -> None:
    """Best-effort Platekit draft when Argus finishes analyzing a gallery (never raises)."""
    if not is_enabled() or run_id <= 0:
        return
    row = db.one(
        """SELECT g.id, g.title, g.client_id,
                  c.platekit_slug, c.company, c.name
           FROM galleries g
           LEFT JOIN clients c ON c.id = g.client_id
           WHERE g.id=?""",
        (gallery_id,),
    )
    if not row:
        log.info("platekit hook skipped — gallery %s not found", gallery_id)
        return
    slug = normalize_slug(row["platekit_slug"] or "") if row["client_id"] else ""
    if not slug and row["client_id"]:
        slug = slug_for_client(
            {
                "platekit_slug": row["platekit_slug"],
                "company": row["company"],
                "name": row["name"],
            }
        )
    if not slug:
        _record(
            gallery_id,
            status="skipped",
            error="no client Platekit slug",
        )
        log.info("platekit hook skipped for gallery %s (no client slug)", gallery_id)
        return

    base = config.PLATEKIT_API_BASE.rstrip("/")
    path = f"/api/mise/organizations/{urllib.parse.quote(slug)}/argus-pack"
    body = json.dumps(
        {
            "argus_run_id": run_id,
            "mise_gallery_id": gallery_id,
            "gallery_title": (row["title"] or "").strip() or None,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}{path}",
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {config.PLATEKIT_API_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PLATEKIT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        msg = f"HTTP {e.code}" + (f": {detail}" if detail else "")
        _record(gallery_id, status="error", error=msg)
        log.warning(
            "platekit argus hook HTTP %s for gallery %s slug=%s%s",
            e.code,
            gallery_id,
            slug,
            f": {detail}" if detail else "",
        )
        return
    except (urllib.error.URLError, TimeoutError) as e:
        _record(gallery_id, status="error", error=str(e)[:200])
        log.warning("platekit argus hook unreachable for gallery %s: %s", gallery_id, e)
        return
    except (ValueError, json.JSONDecodeError):
        _record(gallery_id, status="error", error="unreadable response")
        log.warning("platekit argus hook unreadable response for gallery %s", gallery_id)
        return

    if not isinstance(payload, dict):
        _record(gallery_id, status="error", error="unexpected response")
        return

    job_id = payload.get("job_id")
    pack_id = payload.get("pack_id")
    job_status = (payload.get("job_status") or "queued").strip()
    _record(
        gallery_id,
        status="done" if job_status == "done" else "queued",
        job_id=str(job_id) if job_id else None,
        pack_id=int(pack_id) if pack_id is not None else None,
    )
    log.info(
        "platekit argus hook gallery %s run %s slug=%s -> job %s pack %s",
        gallery_id,
        run_id,
        slug,
        job_id,
        pack_id,
    )
