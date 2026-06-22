"""Crop presets — admin CRUD over the data-driven export/crop table (slice D follow-up).

This is the surface that makes the slice-D overlay engine and any future
delivery-app/print formats reachable without a direct DB edit. Every mutation
writes through db.tx() + audit.log(entity_type="crop_preset"), diff-only, so a
config change to a table the PUBLIC render path reads is observable (R14): a
preset edit silently changes what every client's gallery serves.

Two safety invariants the admin must never break:
- `slug` is the on-disk crop filename key ({stem}_{slug}.jpg) AND a public URL
  token, so it is validated to a safe charset on add and is IMMUTABLE on edit
  (renaming it would orphan every rendered crop and churn live URLs).
- Deactivating a preset is the only "off" switch (no destructive delete): it
  drops out of presets.active(), so portal.crop()/crops_zip stop resolving it —
  a clean 404, never a broken render path.

NOTE: a toggle/edit here records config INTENT. Cached crops are idempotent by
file existence (jobs._h_crops), so existing crops re-render on the next favorite,
not on save — the audit row logs the decision, not a re-cut.
"""

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, db, security
from ..render import templates

log = logging.getLogger("mise.admin.presets")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# slug is a filename key + URL token — lowercase alnum plus _ and -, no path
# separators, dots, or quotes. Must start alnum. Existing seeds (1x1/4x5/9x16) fit.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Fields the edit form may change (NOT slug). Order = diff/display order.
# bleed_px/color_space/dpi are carried by the schema but not honored by the
# render path yet, so they are deliberately not exposed — no inputs that do
# nothing. active/brand_overlay have their own toggle routes.
_EDIT_FIELDS = [
    "name",
    "ratio_label",
    "width",
    "height",
    "centering_x",
    "centering_y",
    "target_channel",
    "sort",
]


def get_preset(preset_id: int) -> "db.sqlite3.Row":
    return db.get_or_404("SELECT * FROM crop_presets WHERE id=?", (preset_id,))


def _parse(form) -> dict:
    """Pull the editable preset fields out of a form into a normalized dict.
    Validates dimensions, centering, and sort; slug is handled separately."""

    def posint(key: str, lo: int, hi: int) -> int:
        try:
            v = int(form.get(key) or "")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad {key}")
        if not (lo <= v <= hi):
            raise HTTPException(status_code=400, detail=f"{key} out of range ({lo}–{hi})")
        return v

    def centering(key: str) -> float:
        try:
            v = float(form.get(key) or "0.5")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad {key}")
        if not (0.0 <= v <= 1.0):
            raise HTTPException(status_code=400, detail=f"{key} must be 0.0–1.0")
        return v

    name = (form.get("name") or "").strip()
    ratio_label = (form.get("ratio_label") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not ratio_label:
        raise HTTPException(status_code=400, detail="ratio_label required")
    try:
        sort = int(form.get("sort") or "0")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad sort")
    return {
        "name": name,
        "ratio_label": ratio_label,
        "width": posint("width", 1, 20000),
        "height": posint("height", 1, 20000),
        "centering_x": centering("centering_x"),
        "centering_y": centering("centering_y"),
        "target_channel": (form.get("target_channel") or "").strip() or None,
        "sort": sort,
    }


@router.get("/presets", response_class=HTMLResponse)
async def presets_list(request: Request):
    rows = db.all_("SELECT * FROM crop_presets ORDER BY active DESC, sort, id")
    trail = db.all_(
        """SELECT entity_id, action, actor, diff_json, created_at FROM audit_log
           WHERE entity_type='crop_preset' ORDER BY id DESC LIMIT 50"""
    )
    slug_by_id = {p["id"]: p["slug"] for p in rows}
    return templates.TemplateResponse(
        request,
        "admin/presets.html",
        {
            "presets": rows,
            "slug_by_id": slug_by_id,
            "trail": [
                {**dict(t), "diff": json.loads(t["diff_json"]) if t["diff_json"] else None}
                for t in trail
            ],
        },
    )


@router.post("/presets")
async def create_preset(request: Request):
    form = await request.form()
    slug = (form.get("slug") or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must be lowercase letters/numbers/_/- (no spaces, dots, slashes)",
        )
    new = _parse(form)
    with db.tx() as con:
        if con.execute("SELECT 1 FROM crop_presets WHERE slug=?", (slug,)).fetchone():
            raise HTTPException(status_code=400, detail=f"slug '{slug}' already exists")
        cur = con.execute(
            """INSERT INTO crop_presets
               (slug, name, ratio_label, width, height, centering_x, centering_y,
                target_channel, sort)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                slug,
                new["name"],
                new["ratio_label"],
                new["width"],
                new["height"],
                new["centering_x"],
                new["centering_y"],
                new["target_channel"],
                new["sort"],
            ),
        )
        pid = cur.lastrowid
        audit.log(con, "crop_preset", pid, "create", diff={"slug": slug, **new})
    log.info("crop_preset %s created (slug=%s)", pid, slug)
    return RedirectResponse("/admin/studio/presets", status_code=303)


@router.post("/presets/{preset_id}")
async def update_preset(request: Request, preset_id: int):
    p = get_preset(preset_id)
    form = await request.form()
    new = _parse(form)
    diff = {f: [p[f], new[f]] for f in _EDIT_FIELDS if p[f] != new[f]}
    if not diff:
        return RedirectResponse("/admin/studio/presets", status_code=303)
    with db.tx() as con:
        con.execute(
            """UPDATE crop_presets SET name=?, ratio_label=?, width=?, height=?,
               centering_x=?, centering_y=?, target_channel=?, sort=? WHERE id=?""",
            (
                new["name"],
                new["ratio_label"],
                new["width"],
                new["height"],
                new["centering_x"],
                new["centering_y"],
                new["target_channel"],
                new["sort"],
                preset_id,
            ),
        )
        audit.log(con, "crop_preset", preset_id, "update", diff=diff)
    log.info("crop_preset %s updated (%d fields)", preset_id, len(diff))
    return RedirectResponse("/admin/studio/presets", status_code=303)


@router.post("/presets/{preset_id}/active")
async def toggle_active(preset_id: int):
    p = get_preset(preset_id)
    new = 0 if p["active"] else 1
    with db.tx() as con:
        con.execute("UPDATE crop_presets SET active=? WHERE id=?", (new, preset_id))
        audit.log(
            con, "crop_preset", preset_id, "active_change", diff={"active": [p["active"], new]}
        )
    log.info("crop_preset %s active %s -> %s", preset_id, p["active"], new)
    return RedirectResponse("/admin/studio/presets", status_code=303)


@router.post("/presets/{preset_id}/overlay")
async def toggle_overlay(preset_id: int):
    p = get_preset(preset_id)
    new = 0 if p["brand_overlay"] else 1
    with db.tx() as con:
        con.execute("UPDATE crop_presets SET brand_overlay=? WHERE id=?", (new, preset_id))
        audit.log(
            con,
            "crop_preset",
            preset_id,
            "overlay_change",
            diff={"brand_overlay": [p["brand_overlay"], new]},
        )
    log.info("crop_preset %s brand_overlay %s -> %s", preset_id, p["brand_overlay"], new)
    return RedirectResponse("/admin/studio/presets", status_code=303)
