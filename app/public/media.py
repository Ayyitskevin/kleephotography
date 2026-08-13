"""Derivative + original serving. FileResponse handles HTTP Range (iOS video)."""

import hashlib
import mimetypes
from email.utils import formatdate, parsedate
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

from .. import config, db, security
from .downloads import _email_required
from .gallery import get_live_gallery, is_expired

router = APIRouter(prefix="/media")

VARIANTS = {"thumb", "web", "original"}

# Client galleries are private, so these bytes cannot go in a shared cache, but a
# returning client should not re-fetch what they already hold.
_CACHE_CONTROL = "private, max-age=86400"


def _conditional_file(request: Request, path: Path, media_type: str):
    """FileResponse, or 304 when the client already holds these exact bytes.

    FileResponse SENDS ETag and Last-Modified but never reads the conditional
    request headers back — that logic lives in StaticFiles, which these routes
    cannot use because every byte here is behind a PIN. So once the 24h freshness
    window lapsed, a returning client re-downloaded the entire grid in full,
    having just presented the very ETag proving it had not changed. On a 60-photo
    gallery that is megabytes over mobile data for nothing.

    The ETag is derived exactly as FileResponse derives it (mtime + size), so a
    tag minted by an earlier response still matches here.
    """
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(status_code=404)
    etag_base = f"{st.st_mtime}-{st.st_size}"
    etag = f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    last_modified = formatdate(st.st_mtime, usegmt=True)
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if _client_copy_is_current(request.headers, etag, last_modified):
        return NotModifiedResponse(Headers(headers))
    # stat_result is handed over so FileResponse does not stat the file again.
    return FileResponse(path, media_type=media_type, headers=headers, stat_result=st)


def _client_copy_is_current(req_headers, etag: str, last_modified: str) -> bool:
    """Mirrors starlette.staticfiles.StaticFiles.is_not_modified.

    If-None-Match wins outright when present, per RFC 9110: it is exact where a
    date comparison is only second-resolution. W/ prefixes are stripped because a
    weak validator still identifies the same bytes for our purposes.
    """
    if if_none_match := req_headers.get("if-none-match"):
        return etag in [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    if_modified_since = parsedate(req_headers.get("if-modified-since", ""))
    parsed_last_modified = parsedate(last_modified)
    return bool(
        if_modified_since is not None
        and parsed_last_modified is not None
        and if_modified_since >= parsed_last_modified
    )


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
    return _conditional_file(request, path, "image/jpeg")


@router.get("/{slug}/{variant}/{asset_id}")
def serve(request: Request, slug: str, variant: str, asset_id: int):
    a, path = _resolve(slug, variant, asset_id, request)
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return _conditional_file(request, path, media_type)
