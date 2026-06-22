"""Derivative + original serving. FileResponse handles HTTP Range (iOS video)."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import config, db, security
from .gallery import get_live_gallery, is_expired

router = APIRouter(prefix="/media")

VARIANTS = {"thumb", "web", "original"}


def _resolve(slug: str, variant: str, asset_id: int, request: Request):
    if variant not in VARIANTS:
        raise HTTPException(status_code=404)
    g = get_live_gallery(slug)
    if is_expired(g):
        raise HTTPException(status_code=410)
    security.require_visitor(request, g["id"])
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND status='ready'", (asset_id, g["id"])
    )
    if not a:
        raise HTTPException(status_code=404)
    base = config.MEDIA_DIR / str(g["id"])
    stem = Path(a["stored"]).stem
    if variant == "original":
        path = base / "original" / a["stored"]
    elif variant == "thumb":
        path = base / "thumb" / f"{stem}.jpg"
    else:  # web
        path = base / "web" / (f"{stem}.mp4" if a["kind"] == "video" else f"{stem}.jpg")
    if not path.is_file():
        raise HTTPException(status_code=404)
    return a, path


# Registered before the generic /{variant}/ route below — otherwise "poster" binds
# to {variant} (not in VARIANTS) and 404s, leaving video <video> posters broken.
@router.get("/{slug}/poster/{asset_id}")
async def poster(request: Request, slug: str, asset_id: int):
    g = get_live_gallery(slug)
    security.require_visitor(request, g["id"])
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND kind='video'", (asset_id, g["id"])
    )
    if not a:
        raise HTTPException(status_code=404)
    stem = Path(a["stored"]).stem
    path = config.MEDIA_DIR / str(g["id"]) / "web" / f"{stem}_poster.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"}
    )


@router.get("/{slug}/{variant}/{asset_id}")
async def serve(request: Request, slug: str, variant: str, asset_id: int):
    a, path = _resolve(slug, variant, asset_id, request)
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(
        path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"}
    )
