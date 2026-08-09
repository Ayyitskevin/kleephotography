"""Downloads — email-gated. Full-gallery ZIP is built async, keyed by content_rev."""

import hashlib
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, db, jobs, security
from ..render import templates
from .gallery import get_live_gallery, is_expired

log = logging.getLogger("mise.public.downloads")
router = APIRouter(prefix="/g")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Server-minted subset-ZIP filenames (favorites / per-section). Only shape this
# regex accepts may be polled through zip_status, and it must also carry the
# caller's own gallery id — see zip_status.
_SUBSET_ZIP = re.compile(r"^g\d+-(?:fav|s\d+)-[0-9a-f]{8}\.zip$")


def _gate(request: Request, slug: str):
    g = get_live_gallery(slug)
    if is_expired(g):
        raise HTTPException(status_code=410)
    visitor = security.require_visitor(request, g["id"])
    return g, visitor


def _email_required(g) -> bool:
    """Galleries email-gate downloads; transfers (drops) don't — a transfer is a
    WeTransfer-style send, so the file grabs straight through."""
    return g["type"] != "drop"


def _store_zip(gallery_id: int, assets, out: Path) -> None:
    jobs.build_zip(out, jobs.zip_entries(gallery_id, assets))


def _queue_or_build(request: Request, g, assets, out: Path, prune: str):
    """Build a favorites/section bundle. Small ones stay inline — one click,
    one file, no wait screen, which is genuinely nicer for a handful of photos.
    Past config.ZIP_INLINE_MAX_* the same copy would stall the event loop for
    the whole gallery, so it goes to the queue and the client gets the SAME
    wait/poll page the full-gallery ZIP uses. Returns that page, or None once
    the file is on disk and the caller can serve it."""
    total_bytes = sum(a["bytes"] or 0 for a in assets)
    # The count ceiling is only a backstop for rows whose size we don't know:
    # with real byte counts the size ceiling already says everything, and a
    # client favouriting 30-60 frames off a delivery gallery is the ordinary
    # case, not an abusive one — sending that to the wait page would trade an
    # instant download for a spinner.
    unmetered = any(a["bytes"] is None for a in assets)
    if total_bytes <= config.ZIP_INLINE_MAX_BYTES and not (
        unmetered and len(assets) > config.ZIP_INLINE_MAX_ASSETS
    ):
        _store_zip(g["id"], assets, out)
        for old in config.ZIP_DIR.glob(prune):
            if old != out:
                old.unlink(missing_ok=True)
        return None
    pending = db.one(
        """SELECT 1 AS x FROM jobs WHERE kind='zip_subset'
                        AND status IN ('queued','running')
                        AND json_extract(payload,'$.name')=?""",
        (out.name,),
    )
    if not pending:
        jobs.enqueue(
            "zip_subset",
            {
                "gallery_id": g["id"],
                "name": out.name,
                "prune": prune,
                "asset_ids": [a["id"] for a in assets],
            },
        )
    return templates.TemplateResponse(
        request,
        "public/zip_wait.html",
        {
            "g": g,
            "status_url": f"/g/{g['slug']}/download/zip/status?name={out.name}",
            "download_url": request.url.path,
        },
    )


def _target(
    slug: str,
    asset_id: int | None,
    fav: int | None,
    section: int | None,
    web: int | None = None,
) -> str:
    if fav:
        return f"/g/{slug}/download/favorites"
    if section is not None:
        return f"/g/{slug}/download/section/{section}"
    if asset_id is not None:
        if web:
            return f"/g/{slug}/download/web/{asset_id}"
        return f"/g/{slug}/download/asset/{asset_id}"
    return f"/g/{slug}/download/zip"


@router.get("/{slug}/download", response_class=HTMLResponse)
async def download_page(
    request: Request,
    slug: str,
    asset_id: int | None = None,
    fav: int | None = None,
    section: int | None = None,
    web: int | None = None,
):
    g, visitor = _gate(request, slug)
    if _email_required(g) and not visitor["email"]:
        return templates.TemplateResponse(
            request,
            "public/email_gate.html",
            {
                "g": g,
                "asset_id": asset_id,
                "fav": fav,
                "section": section,
                "web": web,
                "error": None,
            },
        )
    return RedirectResponse(_target(slug, asset_id, fav, section, web), status_code=303)


@router.post("/{slug}/email", response_class=HTMLResponse)
async def capture_email(
    request: Request,
    slug: str,
    email: str = Form(...),
    asset_id: int | None = Form(None),
    fav: int | None = Form(None),
    section: int | None = Form(None),
    web: int | None = Form(None),
):
    g, visitor = _gate(request, slug)
    email = email.strip().lower()
    if not _EMAIL.match(email):
        return templates.TemplateResponse(
            request,
            "public/email_gate.html",
            {
                "g": g,
                "asset_id": asset_id,
                "fav": fav,
                "section": section,
                "web": web,
                "error": "That doesn't look like an email.",
            },
            status_code=400,
        )
    db.run("UPDATE visitors SET email=? WHERE id=?", (email, visitor["id"]))
    log.info("email captured for gallery %s visitor %s", g["id"], visitor["id"])
    return RedirectResponse(_target(slug, asset_id, fav, section, web), status_code=303)


@router.get("/{slug}/download/asset/{asset_id}")
async def download_asset(request: Request, slug: str, asset_id: int):
    g, visitor = _gate(request, slug)
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download?asset_id={asset_id}", status_code=303)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'", (asset_id, g["id"])
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(g["id"]) / "original" / a["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    db.run(
        "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,?)",
        (g["id"], visitor["id"], asset_id),
    )
    return FileResponse(path, filename=a["filename"], media_type="application/octet-stream")


@router.get("/{slug}/download/web/{asset_id}")
async def download_web_video(request: Request, slug: str, asset_id: int):
    """Web-ready MP4 for a delivered video — the same transcoded H.264 the
    gallery streams, offered as a download so clients get a post-anywhere file
    without pulling the multi-GB camera original."""
    g, visitor = _gate(request, slug)
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download?asset_id={asset_id}&web=1", status_code=303)
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready' AND kind='video'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(g["id"]) / "web" / f"{Path(a['stored']).stem}.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404)
    db.run(
        "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,?)",
        (g["id"], visitor["id"], asset_id),
    )
    return FileResponse(
        path, filename=f"{Path(a['filename']).stem}_web.mp4", media_type="video/mp4"
    )


@router.get("/{slug}/download/rendition/{rendition_id}")
async def download_rendition(request: Request, slug: str, rendition_id: int):
    """A ready social-cut rendition (9:16 / 1:1) as an attachment. Same gates
    as every other download; the tile only links renditions once they're ready,
    so the email-gate redirect just returns the visitor to the gallery flow."""
    g, visitor = _gate(request, slug)
    r = db.one(
        """SELECT r.*, a.filename, a.id AS a_id FROM asset_renditions r
           JOIN assets a ON a.id = r.asset_id
           WHERE r.id=? AND a.gallery_id=? AND r.status='ready'""",
        (rendition_id, g["id"]),
    )
    if not r:
        raise HTTPException(status_code=404)
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download?asset_id={r['a_id']}", status_code=303)
    path = config.MEDIA_DIR / str(g["id"]) / "renditions" / r["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    db.run(
        "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,?)",
        (g["id"], visitor["id"], r["a_id"]),
    )
    return FileResponse(
        path,
        filename=f"{Path(r['filename']).stem}_{r['preset']}.mp4",
        media_type="video/mp4",
    )


@router.get("/{slug}/download/favorites")
async def download_favorites(request: Request, slug: str):
    g, visitor = _gate(request, slug)
    # Match download_asset/download_zip: only email-gate when this gallery type
    # actually requires it. A drop (transfer) skips the gate, and the plain
    # `not email` check here made /download bounce to /download/favorites and
    # back forever (download_page doesn't gate a drop either).
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download?fav=1", status_code=303)
    assets = db.all_(
        """SELECT a.* FROM favorites f JOIN assets a ON a.id=f.asset_id
                        WHERE f.visitor_id=? AND a.gallery_id=? AND a.status='ready'
                        ORDER BY a.id""",
        (visitor["id"], g["id"]),
    )
    if not assets:
        raise HTTPException(status_code=404, detail="no favorites yet")
    # Keyed by the fav-set hash (not the visitor) so identical favorites share
    # one file and stale cleanup bounds the gallery to a single favorites ZIP on
    # disk. Built inline while it's small, queued once it isn't (_queue_or_build).
    key = hashlib.sha256(",".join(str(a["id"]) for a in assets).encode()).hexdigest()[:8]
    out = config.ZIP_DIR / f"g{g['id']}-fav-{key}.zip"
    if not out.is_file():
        # Same pre-write disk floor as the upload handlers — a fresh visitor
        # must not be able to force an unbounded ZIP build on a full disk.
        if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
            raise HTTPException(status_code=507, detail="low disk space — download refused")
        wait = _queue_or_build(request, g, assets, out, f"g{g['id']}-fav-*.zip")
        if wait is not None:
            return wait
    db.run(
        "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,NULL)",
        (g["id"], visitor["id"]),
    )
    base = re.sub(r"[^A-Za-z0-9 _-]", "", g["title"]) or "gallery"
    return FileResponse(out, filename=f"{base}-favorites.zip", media_type="application/zip")


@router.get("/{slug}/download/section/{section_id}")
async def download_section(request: Request, slug: str, section_id: int):
    g, visitor = _gate(request, slug)
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download?section={section_id}", status_code=303)
    s = db.one("SELECT * FROM sections WHERE id=? AND gallery_id=?", (section_id, g["id"]))
    if not s:
        raise HTTPException(status_code=404)
    assets = db.all_(
        """SELECT * FROM assets WHERE gallery_id=? AND section_id=?
                        AND status='ready' ORDER BY position, id""",
        (g["id"], section_id),
    )
    if not assets:
        raise HTTPException(status_code=404, detail="section is empty")
    key = hashlib.sha256(",".join(str(a["id"]) for a in assets).encode()).hexdigest()[:8]
    out = config.ZIP_DIR / f"g{g['id']}-s{section_id}-{key}.zip"
    if not out.is_file():
        wait = _queue_or_build(request, g, assets, out, f"g{g['id']}-s{section_id}-*.zip")
        if wait is not None:
            return wait
    db.run(
        "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,NULL)",
        (g["id"], visitor["id"]),
    )
    base = re.sub(r"[^A-Za-z0-9 _-]", "", f"{g['title']} {s['name']}") or "section"
    return FileResponse(out, filename=f"{base}.zip", media_type="application/zip")


@router.get("/{slug}/download/zip")
async def download_zip(request: Request, slug: str):
    g, visitor = _gate(request, slug)
    if _email_required(g) and not visitor["email"]:
        return RedirectResponse(f"/g/{slug}/download", status_code=303)
    path = jobs.zip_path(g["id"], g["content_rev"])
    if path.is_file():
        db.run(
            "INSERT INTO downloads (gallery_id, visitor_id, asset_id) VALUES (?,?,NULL)",
            (g["id"], visitor["id"]),
        )
        fname = f"{re.sub(r'[^A-Za-z0-9 _-]', '', g['title']) or 'gallery'}.zip"
        return FileResponse(path, filename=fname, media_type="application/zip")
    pending = db.one(
        """SELECT 1 AS x FROM jobs WHERE kind='zip_build'
                        AND status IN ('queued','running')
                        AND json_extract(payload,'$.gallery_id')=?
                        AND json_extract(payload,'$.rev')=?""",
        (g["id"], g["content_rev"]),
    )
    if not pending:
        jobs.enqueue("zip_build", {"gallery_id": g["id"], "rev": g["content_rev"]})
    return templates.TemplateResponse(
        request,
        "public/zip_wait.html",
        {
            "g": g,
            "status_url": f"/g/{slug}/download/zip/status",
            "download_url": f"/g/{slug}/download/zip",
        },
    )


@router.get("/{slug}/download/zip/status")
async def zip_status(request: Request, slug: str, name: str | None = None):
    # Same gates as the sibling download routes — without them this was an
    # unauthenticated status oracle for any known slug.
    g, _ = _gate(request, slug)
    if name is not None:
        # A queued favorites/section bundle (_queue_or_build). The filename is
        # server-minted and namespaced by gallery id, so a visitor who cleared
        # the gate above can only ever poll THIS gallery's bundles — same
        # authorization as the full-gallery branch below, no new surface.
        if not (_SUBSET_ZIP.match(name) and name.startswith(f"g{g['id']}-")):
            raise HTTPException(status_code=404)
        if (config.ZIP_DIR / name).is_file():
            return {"ready": True, "failed": False}
        # Only report a failure that nothing is superseding. A subset bundle is
        # named by a content hash of the favourite/section set, so for a stable
        # set the name never changes and one exhausted build would otherwise
        # paint the error screen on every later attempt, forever — even while a
        # fresh job for the same name is queued and running fine.
        failed = db.one(
            """SELECT 1 AS x FROM jobs WHERE kind='zip_subset' AND status='failed'
                            AND json_extract(payload,'$.name')=?
                            AND NOT EXISTS (
                                SELECT 1 FROM jobs live
                                 WHERE live.kind='zip_subset'
                                   AND live.status IN ('queued','running')
                                   AND json_extract(live.payload,'$.name')=?)""",
            (name, name),
        )
        return {"ready": False, "failed": bool(failed)}
    if jobs.zip_path(g["id"], g["content_rev"]).is_file():
        return {"ready": True, "failed": False}
    # Surface a build that exhausted its retries so the wait page can stop
    # spinning and offer a retry instead of polling forever.
    failed = db.one(
        """SELECT 1 AS x FROM jobs WHERE kind='zip_build' AND status='failed'
                        AND json_extract(payload,'$.gallery_id')=?
                        AND json_extract(payload,'$.rev')=?""",
        (g["id"], g["content_rev"]),
    )
    return {"ready": False, "failed": bool(failed)}
