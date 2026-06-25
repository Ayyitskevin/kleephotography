"""Pull Argus run export into Mise asset rows after vision completes."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from . import config, db

log = logging.getLogger("mise.argus_writeback")

_HERO_LIMIT = 5
_HERO_MIN_SCORE = 0.5


def is_enabled() -> bool:
    return bool(config.ARGUS_URL and config.ARGUS_TOKEN)


def fetch_run_export(run_id: int) -> dict:
    """GET /runs/{id}/export from Argus (bearer auth)."""
    url = f"{config.ARGUS_URL.rstrip('/')}/runs/{int(run_id)}/export"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.ARGUS_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.ARGUS_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError(f"Argus export HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Argus export failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Argus export returned unexpected payload")
    return payload


def _basename_key(name: str) -> str:
    return Path(name).name.lower()


def apply_to_gallery(gallery_id: int, run_id: int) -> dict:
    """Match Argus photos to gallery assets by stored filename; update scores + alt text."""
    if not is_enabled():
        return {"skipped": True, "reason": "argus not configured"}
    export = fetch_run_export(run_id)
    photos = export.get("photos") or []
    assets = db.all_(
        """SELECT id, stored, filename, kind FROM assets
           WHERE gallery_id=? AND kind='photo' AND status='ready'""",
        (gallery_id,),
    )
    by_stored = {_basename_key(a["stored"]): a for a in assets}
    by_filename = {_basename_key(a["filename"]): a for a in assets}

    matched = 0
    hero_rows: list[tuple[float, int]] = []

    for photo in photos:
        basename = _basename_key(str(photo.get("basename") or photo.get("image_path") or ""))
        if not basename:
            continue
        asset = by_stored.get(basename) or by_filename.get(basename)
        if not asset:
            continue

        culling = photo.get("culling") or {}
        keeper = culling.get("keeper_score")
        hero = culling.get("hero_potential")
        keywords = photo.get("keywords") or []
        alt_text = (photo.get("alt_text") or "").strip() or None

        db.run(
            """UPDATE assets SET argus_alt_text=?, argus_keywords=?, argus_keeper_score=?,
                      argus_hero_potential=? WHERE id=?""",
            (
                alt_text,
                json.dumps(keywords) if keywords else None,
                float(keeper) if keeper is not None else None,
                float(hero) if hero is not None else None,
                asset["id"],
            ),
        )
        matched += 1
        if hero is not None and float(hero) >= _HERO_MIN_SCORE:
            hero_rows.append((float(hero), int(asset["id"])))

    hero_rows.sort(key=lambda row: (-row[0], row[1]))
    hero_ids = [asset_id for _, asset_id in hero_rows[:_HERO_LIMIT]]

    db.run(
        """UPDATE galleries SET argus_hero_asset_ids=?, argus_analyzed_count=?
           WHERE id=?""",
        (
            json.dumps(hero_ids) if hero_ids else None,
            matched,
            gallery_id,
        ),
    )
    log.info(
        "argus writeback gallery %s run %s: matched %s/%s photos, heroes=%s",
        gallery_id,
        run_id,
        matched,
        len(photos),
        hero_ids,
    )
    return {
        "gallery_id": gallery_id,
        "run_id": run_id,
        "matched": matched,
        "photo_count": len(photos),
        "hero_asset_ids": hero_ids,
    }


def run_for_gallery(gallery_id: int, run_id: int) -> None:
    """Background job entry — never raises."""
    try:
        apply_to_gallery(gallery_id, run_id)
    except Exception as exc:
        log.warning("argus writeback failed for gallery %s run %s: %s", gallery_id, run_id, exc)