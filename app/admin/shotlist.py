"""Shot lists per project (Domain F, slice 1) — Mise-local, studio-only.

The menu-driven shot list Kevin builds before an F&B shoot: what we're shooting,
grouped by category and triaged by priority. LOCAL ONLY for now (Kevin: "Mise
owns, local for now") — there is no Notion sync in this slice. Odysseus'
preshoot_pack reads its own Notion shotlist DS; pushing these rows up to Notion
is a deferred LATER slice, so nothing here touches Notion or Odysseus state.

Every mutation (create / update / soft-delete) writes through db.tx() so the row
change and its audit_log entry (entity_type='shot_list') commit together, exactly
like press.py / licenses.py. Soft-delete sets deleted_at; nothing here hard-deletes.

Routes hang off /admin/studio and all redirect back to the owning project page —
the shot list has no standalone index; it lives inside project_detail.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import audit, db, security
from ..usage_vocab import SHOT_CATEGORIES, SHOT_PRIORITIES
from .lookups import get_project

log = logging.getLogger("mise.admin.shotlist")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Columns the diff/audit machinery tracks. Order = form/display order.
_FIELDS = ["title", "category", "priority", "sort_order", "note"]


def _parse_form(form) -> dict:
    """Normalize a submitted shot form, validating the two constrained inputs:
    title must be non-empty; category, when given, must be in SHOT_CATEGORIES;
    priority must be in SHOT_PRIORITIES (defaults to 'want' when blank). sort_order
    is an int (non-numeric → 0)."""
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")

    category = (form.get("category") or "").strip() or None
    if category and category not in SHOT_CATEGORIES:
        raise HTTPException(status_code=400, detail="bad category")

    priority = (form.get("priority") or "").strip() or "want"
    if priority not in SHOT_PRIORITIES:
        raise HTTPException(status_code=400, detail="bad priority")

    so = (form.get("sort_order") or "").strip()
    sort_order = int(so) if so.lstrip("-").isdigit() else 0

    return {
        "title": title,
        "category": category,
        "priority": priority,
        "sort_order": sort_order,
        "note": (form.get("note") or "").strip() or None,
    }


def _get_shot(shot_id: int) -> "db.sqlite3.Row":
    s = db.one("SELECT * FROM shot_list WHERE id=? AND deleted_at IS NULL", (shot_id,))
    if not s:
        raise HTTPException(status_code=404)
    return s


@router.post("/projects/{project_id}/shots")
async def create_shot(request: Request, project_id: int):
    get_project(project_id)  # 404 if the project doesn't exist
    new = _parse_form(await request.form())
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO shot_list (project_id, title, category, priority,
                                      sort_order, note)
               VALUES (?,?,?,?,?,?)""",
            (
                project_id,
                new["title"],
                new["category"],
                new["priority"],
                new["sort_order"],
                new["note"],
            ),
        )
        sid = cur.lastrowid
        audit.log(
            con,
            "shot_list",
            sid,
            "create",
            diff={**{k: new[k] for k in _FIELDS if new[k] is not None}, "project_id": project_id},
        )
    log.info("shot %s created on project %s (priority=%s)", sid, project_id, new["priority"])
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/shots/{shot_id}")
async def update_shot(request: Request, shot_id: int):
    d = _get_shot(shot_id)
    new = _parse_form(await request.form())
    diff = {f: [d[f], new[f]] for f in _FIELDS if (d[f] or None) != (new[f] or None)}
    if not diff:
        return RedirectResponse(f"/admin/studio/projects/{d['project_id']}", status_code=303)
    with db.tx() as con:
        con.execute(
            """UPDATE shot_list SET title=?, category=?, priority=?, sort_order=?,
               note=?, updated_at=datetime('now') WHERE id=?""",
            (
                new["title"],
                new["category"],
                new["priority"],
                new["sort_order"],
                new["note"],
                shot_id,
            ),
        )
        audit.log(con, "shot_list", shot_id, "update", diff=diff)
    log.info("shot %s updated (%d fields)", shot_id, len(diff))
    return RedirectResponse(f"/admin/studio/projects/{d['project_id']}", status_code=303)


@router.post("/shots/{shot_id}/delete")
async def delete_shot(shot_id: int):
    d = _get_shot(shot_id)
    with db.tx() as con:
        con.execute("UPDATE shot_list SET deleted_at=datetime('now') WHERE id=?", (shot_id,))
        audit.log(
            con,
            "shot_list",
            shot_id,
            "soft_delete",
            diff={"title": d["title"], "priority": d["priority"]},
        )
    log.info("shot %s soft-deleted", shot_id)
    return RedirectResponse(f"/admin/studio/projects/{d['project_id']}", status_code=303)
