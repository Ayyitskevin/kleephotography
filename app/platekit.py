"""Read-only client for Platekit/Dionysus content packs.

Mise stays the photography operating system; Platekit owns campaign-pack
generation and approval. This bridge only reads approved/exported packs for a
client-like organization slug and degrades to an empty admin panel when disabled
or unreachable.
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


def signup_url(client) -> str:
    company = client["company"] or client["name"] or ""
    params = urllib.parse.urlencode(
        {
            "company": company,
            "name": client["name"] or "",
            "email": client["email"] or "",
            "audience": "restaurant",
        }
    )
    return f"https://platekit.kleephotography.com/?{params}#signup"


def _empty(*, slug: str, status: str, message: str, enabled: bool | None = None) -> dict:
    return {
        "enabled": is_enabled() if enabled is None else enabled,
        "slug": slug,
        "status": status,
        "message": message,
        "packs": [],
        "signup_url": "",
    }


def packs_for_client(client, *, include_drafts: bool = False) -> dict:
    slug = slug_for_client(client)
    if not is_enabled():
        state = _empty(
            slug=slug,
            status="not_configured",
            message="Platekit bridge is not configured",
            enabled=False,
        )
        state["signup_url"] = signup_url(client)
        return state
    if not slug:
        state = _empty(
            slug=slug, status="missing_slug", message="Client does not have a usable Platekit slug"
        )
        state["signup_url"] = signup_url(client)
        return state

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
            state = _empty(
                slug=slug, status="not_found", message="No matching Platekit organization"
            )
            state["signup_url"] = signup_url(client)
            return state
        log.warning("Platekit returned HTTP %s for slug=%s", e.code, slug)
        state = _empty(slug=slug, status="error", message=f"Platekit returned HTTP {e.code}")
        state["signup_url"] = signup_url(client)
        return state
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Platekit unreachable for slug=%s: %s", slug, e)
        state = _empty(slug=slug, status="error", message="Platekit is unreachable")
        state["signup_url"] = signup_url(client)
        return state
    except (ValueError, json.JSONDecodeError):
        state = _empty(
            slug=slug, status="error", message="Platekit returned an unreadable response"
        )
        state["signup_url"] = signup_url(client)
        return state

    return {
        "enabled": True,
        "slug": slug,
        "status": "ok",
        "message": "",
        "packs": payload.get("packs") or [],
        "signup_url": signup_url(client),
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
        log.warning(
            "platekit argus hook HTTP %s for gallery %s slug=%s%s",
            e.code,
            gallery_id,
            slug,
            f": {detail}" if detail else "",
        )
        return
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("platekit argus hook unreachable for gallery %s: %s", gallery_id, e)
        return
    except (ValueError, json.JSONDecodeError):
        log.warning("platekit argus hook unreadable response for gallery %s", gallery_id)
        return

    pack_id = payload.get("pack_id") if isinstance(payload, dict) else None
    log.info(
        "platekit argus hook gallery %s run %s slug=%s -> pack %s",
        gallery_id,
        run_id,
        slug,
        pack_id,
    )
