import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import config, db, jobs, security
from ..imaging import PHOTO_EXTS, VIDEO_EXTS
from . import common

log = logging.getLogger("mise.admin.uploads")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def _free_gb() -> float:
    return shutil.disk_usage(config.DATA_DIR).free / 1e9


def _cleanup_staged_uploads(originals: list[Path]) -> None:
    for original in originals:
        original.unlink(missing_ok=True)


@router.post("/galleries/{gallery_id}/upload")
async def upload(gallery_id: int, files: list[UploadFile], section_id: int | None = None):
    g = db.one("SELECT id FROM galleries WHERE id=?", (gallery_id,))
    if not g:
        raise HTTPException(status_code=404)
    # Preflight before creating media directories or copying upload bytes. The same
    # ownership check runs again inside the write transaction after all awaited
    # file saves, because a section id can be deleted and reused meanwhile.
    if section_id is not None and not db.one(
        "SELECT id FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id)
    ):
        raise HTTPException(status_code=400, detail="unknown section")
    # Land new uploads in the gallery's first section (its display order) instead
    # of the catch-all "More" bucket that forces manual moving. Stays None when the
    # gallery has no custom sections — the clean default.
    if section_id is None:
        first = db.one(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
            (gallery_id,),
        )
        if first:
            section_id = first["id"]
    if _free_gb() < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")

    base = config.MEDIA_DIR / str(gallery_id)
    for sub in ("original", "web", "thumb"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    rejected: list[str] = []
    originals: list[Path] = []
    staged: list[tuple[str, str, str, int]] = []
    try:
        for f in files:
            name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
            ext = Path(name).suffix.lower()
            if ext in PHOTO_EXTS:
                kind = "photo"
            elif ext in VIDEO_EXTS:
                kind = "video"
            else:
                rejected.append(name)
                continue
            stored = f"{uuid.uuid4().hex}{ext}"
            original = base / "original" / stored
            originals.append(original)
            size = await common.save_upload(f, original)
            staged.append((kind, name, stored, size))
    except Exception:
        _cleanup_staged_uploads(originals)
        raise

    accepted: list[int] = []
    job_ids: list[int] = []
    if staged:
        try:
            with db.tx() as con:
                con.execute("BEGIN IMMEDIATE")
                if (
                    section_id is not None
                    and not con.execute(
                        "SELECT id FROM sections WHERE id=? AND gallery_id=?",
                        (section_id, gallery_id),
                    ).fetchone()
                ):
                    raise HTTPException(status_code=400, detail="unknown section")
                for kind, name, stored, size in staged:
                    asset_id = con.execute(
                        "INSERT INTO assets "
                        "(gallery_id, section_id, kind, filename, stored, bytes) "
                        "VALUES (?,?,?,?,?,?)",
                        (gallery_id, section_id, kind, name, stored, size),
                    ).lastrowid
                    accepted.append(asset_id)
                    job_ids.append(
                        jobs.stage(
                            con,
                            "image_derivatives" if kind == "photo" else "video_transcode",
                            {"asset_id": asset_id},
                        )
                    )
                con.execute(
                    "UPDATE galleries SET content_rev=content_rev+1 WHERE id=?", (gallery_id,)
                )
        except Exception:
            _cleanup_staged_uploads(originals)
            raise
        jobs.dispatch(job_ids)
    log.info("gallery %s: %d accepted, %d rejected", gallery_id, len(accepted), len(rejected))
    return {"accepted": len(accepted), "rejected": rejected}
