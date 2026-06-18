"""Content portal — honest brand-kit + caption-pack overview over REAL data.

Adapts the Admin Content prototype. The prototype fabricates brand colours,
voice tags, typefaces, and a client-side "AI" caption generator — none of which
Mise stores. This page shows only what Mise actually has:

  - brand_kits   — the raster overlay logo + placement params (position, opacity,
                   scale, margin) that get composited into crop JPEGs server-side
  - brand_assets — the per-client file locker (logos, menus, EPS/PDF they shared)
  - crop_presets — global export presets (read-only here; managed under Presets)
  - retainer_captions — caption deliverables on monthly retainer plans, with the
                   honest AI-assist provenance (drafted by Odysseus, never auto-posted)

A ?client=ID switcher scopes the kit/asset/caption sections; crop presets are
global and always shown. Everything is read-only — editing lives on the real
management routes (per-client Studio page, Presets, Recurring), so nothing here
writes and nothing narrates to the Notion Activity Log.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import caption_ai, db, presets, security
from ..render import templates

router = APIRouter(prefix="/admin/content",
                   dependencies=[Depends(security.require_admin)])

# 9-grid position codes -> human labels (matches brand_kits.position)
_POS_LABELS = {
    "tl": "Top left", "tc": "Top centre", "tr": "Top right",
    "ml": "Mid left", "c": "Centre", "mr": "Mid right",
    "bl": "Bottom left", "bc": "Bottom centre", "br": "Bottom right",
}


def _kb(n: int) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _clients_with_content() -> list[dict]:
    """Clients that own a brand kit, a brand asset, or a retainer caption —
    the only clients worth offering in the switcher."""
    return db.all_(
        """SELECT c.id, c.name, c.company
             FROM clients c
            WHERE c.id IN (SELECT client_id FROM brand_kits)
               OR c.id IN (SELECT client_id FROM brand_assets)
               OR c.id IN (SELECT pr.client_id
                             FROM recurring_plans rp
                             JOIN projects pr ON pr.id = rp.project_id
                             JOIN retainer_captions rc ON rc.plan_id = rp.id)
            ORDER BY c.name""")


@router.get("", response_class=HTMLResponse)
async def content(request: Request):
    try:
        sel = int(request.query_params.get("client", ""))
    except (TypeError, ValueError):
        sel = None

    roster = _clients_with_content()
    if sel is not None and sel not in {c["id"] for c in roster}:
        sel = None  # stale/unknown id -> fall back to All

    where = "WHERE k.client_id = ?" if sel else ""
    kit_rows = db.all_(
        f"""SELECT k.*, c.name AS client_name, c.company AS client_company
              FROM brand_kits k JOIN clients c ON c.id = k.client_id
              {where}
             ORDER BY c.name, k.active DESC, k.id DESC""",
        (sel,) if sel else ())
    kits = [{
        "id": k["id"], "client_id": k["client_id"],
        "client": k["client_company"] or k["client_name"],
        "label": k["label"] or "Logo",
        "active": bool(k["active"]),
        "position": _POS_LABELS.get(k["position"], k["position"]),
        "opacity": k["opacity"], "scale": k["scale_pct"],
        "margin": k["margin_pct"], "bytes": _kb(k["bytes"]),
    } for k in kit_rows]

    awhere = "WHERE b.client_id = ?" if sel else ""
    asset_rows = db.all_(
        f"""SELECT b.id, b.client_id, b.filename, b.bytes, b.created_at,
                   c.name AS client_name, c.company AS client_company
              FROM brand_assets b JOIN clients c ON c.id = b.client_id
              {awhere}
             ORDER BY b.created_at DESC""",
        (sel,) if sel else ())
    assets = [{
        "id": a["id"], "client_id": a["client_id"],
        "client": a["client_company"] or a["client_name"],
        "filename": a["filename"], "bytes": _kb(a["bytes"]),
        "date": (a["created_at"] or "")[:10],
    } for a in asset_rows]

    cwhere = "WHERE pr.client_id = ?" if sel else ""
    cap_rows = db.all_(
        f"""SELECT rc.id, rc.period, rc.label, rc.body, rc.status,
                   rc.ai_drafted, rc.ai_model,
                   rp.id AS plan_id, rp.title AS plan_title,
                   c.name AS client_name, c.company AS client_company
              FROM retainer_captions rc
              JOIN recurring_plans rp ON rp.id = rc.plan_id
              JOIN projects pr ON pr.id = rp.project_id
              JOIN clients c ON c.id = pr.client_id
              {cwhere}
             ORDER BY rc.period DESC, rc.created_at DESC, rc.id DESC
             LIMIT 60""",
        (sel,) if sel else ())
    body_clip = lambda b: (b[:180] + "…") if b and len(b) > 180 else (b or "")
    captions = [{
        "id": r["id"], "plan_id": r["plan_id"],
        "client": r["client_company"] or r["client_name"],
        "plan": r["plan_title"], "period": r["period"], "label": r["label"],
        "body": body_clip(r["body"]),
        "approved": r["status"] == "approved",
        "ai": bool(r["ai_drafted"]), "model": r["ai_model"] or "",
    } for r in cap_rows]

    presets_rows = presets.active()
    preset_cards = [{
        "name": p["name"], "ratio": p["ratio_label"],
        "dims": f'{p["width"]}×{p["height"]}',
        "channel": (p["target_channel"] or "").replace("_", " ") or "—",
        "overlay": bool(p["brand_overlay"]),
    } for p in presets_rows]

    n_appr = sum(1 for c in captions if c["approved"])
    cards = [
        {"label": "Brand kits", "value": str(len(kits)), "tone": "dark",
         "sub": "overlay logos with placement"},
        {"label": "Brand assets", "value": str(len(assets)), "tone": "plain",
         "sub": "files clients shared"},
        {"label": "Caption packs", "value": str(len(captions)), "tone": "ok",
         "sub": f"{n_appr} approved · {len(captions) - n_appr} draft"},
        {"label": "Export presets", "value": str(len(preset_cards)), "tone": "warn",
         "sub": "active crop ratios"},
    ]

    sel_client = next((c for c in roster if c["id"] == sel), None) if sel else None
    switch = [{"id": None, "label": "All clients", "on": sel is None}] + [
        {"id": c["id"], "label": c["company"] or c["name"], "on": c["id"] == sel}
        for c in roster]

    return templates.TemplateResponse(request, "admin/content.html", {
        "cards": cards, "kits": kits, "assets": assets, "captions": captions,
        "presets": preset_cards, "switch": switch, "sel": sel,
        "sel_client": sel_client,
        "ai_enabled": caption_ai.is_enabled(),
    })
