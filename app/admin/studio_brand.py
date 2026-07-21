"""Client brand assets + brand kits (composited overlay logos).

Split from studio.py so the CRM spine stays thinner; same /admin/studio prefix.
"""

import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from .. import config, db, security
from . import common
from .lookups import get_client

log = logging.getLogger("mise.admin.studio_brand")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")
BRAND_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".eps", ".ai", ".zip"}
KIT_EXTS = {".png", ".webp", ".jpg", ".jpeg"}
KIT_POSITIONS = {"tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"}

# ── Brand assets (Phase 2) ─────────────────────────────────────────────────


@router.post("/clients/{client_id}/brand")
async def upload_brand(client_id: int, files: list[UploadFile]):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rejected = []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
        ext = Path(name).suffix.lower()
        if ext not in BRAND_EXTS:
            rejected.append(name)
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        size = await common.save_upload(f, dest_dir / stored)
        db.run(
            "INSERT INTO brand_assets (client_id, filename, stored, bytes) VALUES (?,?,?,?)",
            (client_id, name, stored, size),
        )
    if rejected:
        log.info("client %s brand upload: rejected %s", client_id, rejected)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/brand/{ba_id}")
async def admin_brand_file(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if not b:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / b["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=b["filename"])


@router.post("/clients/{client_id}/brand/{ba_id}/delete")
async def delete_brand(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if b:
        (config.BRAND_DIR / str(client_id) / b["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_assets WHERE id=?", (ba_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Brand kits (Slice 3 — composite overlay) ───────────────────────────────
# A brand_kit is a single raster logo + placement params composited onto social
# crops at render time. Distinct from brand_assets (the general file locker).
# Newest active kit wins (see app/brand_kits.overlay_for_client).


@router.post("/clients/{client_id}/kits")
async def upload_kit(
    client_id: int,
    logo: UploadFile,
    label: str = Form(""),
    position: str = Form("br"),
    opacity: int = Form(100),
    scale_pct: int = Form(22),
    margin_pct: int = Form(4),
):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    name = _SAFE_NAME.sub("_", Path(logo.filename or "logo").name)
    ext = Path(name).suffix.lower()
    if ext not in KIT_EXTS:
        raise HTTPException(
            status_code=415, detail=f"brand-kit logo must be PNG/WebP/JPEG, not {ext}"
        )
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"kit_{uuid.uuid4().hex}{ext}"
    size = await common.save_upload(logo, dest_dir / stored)
    db.run(
        "INSERT INTO brand_kits (client_id, label, stored, bytes, position, "
        "opacity, scale_pct, margin_pct) VALUES (?,?,?,?,?,?,?,?)",
        (
            client_id,
            label.strip() or None,
            stored,
            size,
            position,
            max(0, min(100, opacity)),
            max(1, min(100, scale_pct)),
            max(0, min(50, margin_pct)),
        ),
    )
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/kits/{kit_id}")
async def update_kit(
    client_id: int,
    kit_id: int,
    label: str = Form(""),
    position: str = Form("br"),
    opacity: int = Form(100),
    scale_pct: int = Form(22),
    margin_pct: int = Form(4),
    active: int = Form(0),
):
    k = db.one("SELECT id FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    db.run(
        "UPDATE brand_kits SET label=?, position=?, opacity=?, scale_pct=?, "
        "margin_pct=?, active=? WHERE id=?",
        (
            label.strip() or None,
            position,
            max(0, min(100, opacity)),
            max(1, min(100, scale_pct)),
            max(0, min(50, margin_pct)),
            1 if active else 0,
            kit_id,
        ),
    )
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/kits/{kit_id}/logo")
async def admin_kit_logo(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / k["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.post("/clients/{client_id}/kits/{kit_id}/delete")
async def delete_kit(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if k:
        (config.BRAND_DIR / str(client_id) / k["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_kits WHERE id=?", (kit_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)
