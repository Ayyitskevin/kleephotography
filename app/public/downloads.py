"""Downloads — email-gated. Full-gallery ZIP is built async, keyed by content_rev."""

import hashlib
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, db, jobs, security
from ..render import templates
from .gallery import get_live_gallery, is_expired

log = logging.getLogger("mise.public.downloads")
router = APIRouter(prefix="/g")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    src_dir = config.MEDIA_DIR / str(gallery_id) / "original"
    names: set[str] = set()
    entries = []
    for a in assets:
        name = a["filename"]
        if name in names:
            name = f"{Path(name).stem}_{a['id']}{Path(name).suffix}"
        names.add(name)
        entries.append((src_dir / a["stored"], name))
    jobs.build_zip(out, entries)


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
    # small subset of originals — built synchronously, content-keyed per visitor
    key = hashlib.sha256(",".join(str(a["id"]) for a in assets).encode()).hexdigest()[:8]
    out = config.ZIP_DIR / f"g{g['id']}-v{visitor['id']}-{key}.zip"
    if not out.is_file():
        _store_zip(g["id"], assets, out)
        for old in config.ZIP_DIR.glob(f"g{g['id']}-v{visitor['id']}-*.zip"):
            if old != out:
                old.unlink(missing_ok=True)
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
        _store_zip(g["id"], assets, out)
        for old in config.ZIP_DIR.glob(f"g{g['id']}-s{section_id}-*.zip"):
            if old != out:
                old.unlink(missing_ok=True)
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
    return templates.TemplateResponse(request, "public/zip_wait.html", {"g": g})


@router.get("/{slug}/download/zip/status")
async def zip_status(slug: str):
    g = get_live_gallery(slug)
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
