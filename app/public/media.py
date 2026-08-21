"""Derivative + original serving. FileResponse handles HTTP Range (iOS video).

The conditional-request half (ETag / If-None-Match -> 304) lives in
app/http_cache.py, shared with the public /site/* portfolio routes.
"""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .. import config, db, security
from ..http_cache import PRIVATE_24H, conditional_file
from .downloads import _email_required
from .gallery import get_live_gallery, is_expired

router = APIRouter(prefix="/media")

VARIANTS = {"thumb", "web", "original"}


def _resolve(slug: str, variant: str, asset_id: int, request: Request):
    if variant not in VARIANTS:
        raise HTTPException(status_code=404)
    g = get_live_gallery(slug)
    if is_expired(g):
        raise HTTPException(status_code=410)
    visitor = security.require_visitor(request, g["id"])
    # Originals carry the email-capture control the download routes enforce —
    # serving them here ungated would let a no-email visitor (e.g. a drop-link
    # holder) bulk-pull masters. thumb/web stay open; drops don't gate at all.
    if variant == "original" and _email_required(g) and not visitor["email"]:
        raise HTTPException(status_code=403, detail="email required for originals")
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
def poster(request: Request, slug: str, asset_id: int):
    g = get_live_gallery(slug)
    # Mirror _resolve: an expired gallery is 410 everywhere, and only ready
    # assets serve — the poster route skipped both of these gates.
    if is_expired(g):
        raise HTTPException(status_code=410)
    security.require_visitor(request, g["id"])
    a = db.one(
        "SELECT * FROM assets WHERE id=? AND gallery_id=? AND kind='video' AND status='ready'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    stem = Path(a["stored"]).stem
    path = config.MEDIA_DIR / str(g["id"]) / "web" / f"{stem}_poster.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return conditional_file(request, path, "image/jpeg", PRIVATE_24H)


@router.get("/{slug}/{variant}/{asset_id}")
def serve(request: Request, slug: str, variant: str, asset_id: int):
    a, path = _resolve(slug, variant, asset_id, request)
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return conditional_file(request, path, media_type, PRIVATE_24H)
