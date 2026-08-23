"""Conditional file responses — the 304 half of caching, which Starlette omits.

`FileResponse` SENDS `ETag` and `Last-Modified` but never reads the conditional
request headers back; that logic lives in `StaticFiles`, which none of Mise's
media routes can use — the private ones because every byte is behind a PIN, the
public ones because the portfolio gate is a database query, not a directory.

So once the freshness window lapsed, a returning client re-downloaded the whole
grid in full, having just presented the ETag proving nothing had changed. That
was fixed for `/media/*` first; this module is the shared seam so `/site/*` gets
the identical behaviour instead of a second copy of it.
"""

import hashlib
import stat
from email.utils import formatdate, parsedate
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

# Client galleries are private, so those bytes cannot go in a shared cache.
# Portfolio frames are public by definition and SHOULD sit in Cloudflare's edge
# cache — same 24h window, opposite sharing rule.
PRIVATE_24H = "private, max-age=86400"
PUBLIC_24H = "public, max-age=86400"


def client_copy_is_current(req_headers, etag: str, last_modified: str) -> bool:
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


def conditional_file(
    request,
    path: Path,
    media_type: str,
    cache_control: str = PRIVATE_24H,
    extra_headers: dict[str, str] | None = None,
    filename: str | None = None,
):
    """FileResponse, or 304 when the client already holds these exact bytes.

    The ETag is derived exactly as FileResponse derives it (mtime + size), so a
    tag minted by an earlier response still matches here.

    `filename` rides through to FileResponse, which owns the Content-Disposition
    grammar (attachment; RFC 5987 `filename*` when the name is not plain ASCII) —
    download routes get the 304 half without re-deriving that header here. The
    304 branch omits the disposition on purpose: there is no body to name.
    """
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(status_code=404)
    # The callers used to guard with `is_file()`; keep that here so a directory
    # (which stats fine) still 404s instead of reaching FileResponse.
    if not stat.S_ISREG(st.st_mode):
        raise HTTPException(status_code=404)
    etag_base = f"{st.st_mtime}-{st.st_size}"
    etag = f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    last_modified = formatdate(st.st_mtime, usegmt=True)
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if extra_headers:
        headers.update(extra_headers)
    if client_copy_is_current(request.headers, etag, last_modified):
        return NotModifiedResponse(Headers(headers))
    # stat_result is handed over so FileResponse does not stat the file again.
    return FileResponse(
        path, media_type=media_type, headers=headers, stat_result=st, filename=filename
    )
