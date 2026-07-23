"""Gallery section CRUD + asset move/reorder within sections.

Split from galleries.py so the gallery detail router stays thinner.
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import db, security
from .galleries import _bench_fragment, _tile_fragment, get_gallery

log = logging.getLogger("mise.admin.gallery_sections")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


@router.post("/galleries/{gallery_id}/sections")
async def add_section(gallery_id: int, name: str = Form(...)):
    get_gallery(gallery_id)
    row = db.one(
        "SELECT COALESCE(MAX(position),-1)+1 AS p FROM sections WHERE gallery_id=?", (gallery_id,)
    )
    db.run(
        "INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
        (gallery_id, name.strip(), row["p"]),
    )
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


def get_section(gallery_id: int, section_id: int) -> "db.sqlite3.Row":
    return db.get_or_404(
        "SELECT * FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id)
    )


@router.post("/galleries/{gallery_id}/sections/{section_id}/rename")
async def rename_section(gallery_id: int, section_id: int, name: str = Form(...)):
    get_section(gallery_id, section_id)
    if not name.strip():
        raise HTTPException(status_code=400, detail="name required")
    db.run("UPDATE sections SET name=? WHERE id=?", (name.strip(), section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/proof")
async def set_section_proof(gallery_id: int, section_id: int, proof_target: str = Form("")):
    """Set or clear the proofing target on a section.
    Empty/0/non-numeric → clear (section becomes free-form again)."""
    get_section(gallery_id, section_id)
    raw = proof_target.strip()
    target = None
    if raw:
        try:
            n = int(raw)
            target = n if n > 0 else None
        except ValueError:
            raise HTTPException(status_code=400, detail="proof target must be a number")
    db.run("UPDATE sections SET proof_target=? WHERE id=?", (target, section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/caption")
async def set_section_caption(gallery_id: int, section_id: int, caption: str = Form("")):
    """Set or clear the public-facing caption shown under the section heading."""
    get_section(gallery_id, section_id)
    db.run("UPDATE sections SET caption=? WHERE id=?", (caption.strip() or None, section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/move")
async def reorder_section(gallery_id: int, section_id: int, dir: str = Form(...)):
    if dir not in ("up", "down"):
        raise HTTPException(status_code=400, detail="dir must be up or down")
    get_section(gallery_id, section_id)
    ids = [
        r["id"]
        for r in db.all_(
            "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id", (gallery_id,)
        )
    ]
    i = ids.index(section_id)
    j = i - 1 if dir == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        with db.tx() as con:
            for pos, sid in enumerate(ids):
                con.execute("UPDATE sections SET position=? WHERE id=?", (pos, sid))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/delete")
async def delete_section(gallery_id: int, section_id: int):
    db.run("DELETE FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/section")
async def move_asset(
    request: Request, gallery_id: int, asset_id: int, section_id: int | None = Form(None)
):
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        if (
            section_id is not None
            and not con.execute(
                "SELECT id FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id)
            ).fetchone()
        ):
            raise HTTPException(status_code=400, detail="unknown section")
        con.execute(
            "UPDATE assets SET section_id=? WHERE id=? AND gallery_id=?",
            (section_id, asset_id, gallery_id),
        )
    if request.headers.get("hx-request") == "true":
        return _tile_fragment(request, gallery_id, asset_id)
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/bulk-section")
async def bulk_move_assets(request: Request, gallery_id: int):
    get_gallery(gallery_id)
    form = await request.form()
    raw = form.get("section_id") or ""
    section_id = int(raw) if raw else None
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        if (
            section_id is not None
            and not con.execute(
                "SELECT id FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id)
            ).fetchone()
        ):
            raise HTTPException(status_code=400, detail="unknown section")
        for v in form.getlist("asset_ids"):
            con.execute(
                "UPDATE assets SET section_id=? WHERE id=? AND gallery_id=?",
                (section_id, int(v), gallery_id),
            )
    if request.headers.get("hx-request") == "true":
        return _bench_fragment(request, gallery_id)
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)
