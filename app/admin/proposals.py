"""Proposals — composer with F&B package presets. Drafts are editable; send locks them."""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, security
from ..render import templates
from .contracts import render_template
from .studio import get_project

log = logging.getLogger("mise.admin.proposals")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Starting points only — Kevin edits line items per client. Three categories
# (Photography / Videography / Brand Partner retainer) × three tiers (Starter /
# Standard / Premium). Asheville/Western-NC-comp; retainer tiers stay ~35–40%
# below the equivalent ad-hoc monthly bundle so the deal reads "deal."
#
# IMPORTANT: Odysseus Products catalog on mickey :7010 was backfilled at the
# previous prices (half-day $1,000 floor, Brand Partner Monthly $2,200). Update
# the catalog separately or proposal_engine will draft against stale numbers.

# Default proposal cover note — the "Our Story" brand voice, seeded on every
# preset proposal (editable per client; blank proposals start with no intro).
OUR_STORY_INTRO = (
    "At Kevin Lee Photography, we're not just photographers — we're storytellers. "
    "Every dish, every space, every detail is a chance to make your brand look as "
    "good as it tastes. Here's what I'm proposing for your shoot — let's make "
    "something worth sharing."
)

PRESETS = {
    "blank": {"title": "Proposal", "items": []},

    # ── Photography ─────────────────────────────────────────────────────────
    "photo_starter": {"title": "Photography — Starter", "items": [
        {"label": "Half-day photography session (up to 4 hrs)", "qty": 1, "unit_cents": 90000},
        {"label": "Up to 20 edited, web-ready images", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + standard usage rights", "qty": 1, "unit_cents": 0},
    ]},
    "photo_standard": {"title": "Photography — Standard", "items": [
        {"label": "Full-day photography session (up to 8 hrs)", "qty": 1, "unit_cents": 180000},
        {"label": "Up to 50 edited, web-ready images", "qty": 1, "unit_cents": 0},
        {"label": "Social crops (1:1, 4:5, 9:16) for hero selects", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + standard usage rights", "qty": 1, "unit_cents": 0},
    ]},
    "photo_premium": {"title": "Photography — Premium", "items": [
        {"label": "Extended-day photography session (up to 10 hrs)", "qty": 1, "unit_cents": 320000},
        {"label": "Up to 75 edited, web-ready images", "qty": 1, "unit_cents": 0},
        {"label": "Social crops (1:1, 4:5, 9:16) for every select", "qty": 1, "unit_cents": 0},
        {"label": "Rush turnaround (5 business days)", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + extended usage rights", "qty": 1, "unit_cents": 0},
    ]},

    # ── Videography ─────────────────────────────────────────────────────────
    "video_starter": {"title": "Videography — Starter", "items": [
        {"label": "Half-day video shoot (up to 4 hrs)", "qty": 1, "unit_cents": 180000},
        {"label": "3 short-form vertical reels (15–30s each, edited)", "qty": 1, "unit_cents": 0},
        {"label": "Licensed music + color grade", "qty": 1, "unit_cents": 0},
        {"label": "Delivery via gallery + standard usage rights", "qty": 1, "unit_cents": 0},
    ]},
    "video_standard": {"title": "Videography — Standard", "items": [
        {"label": "Full-day video shoot (up to 8 hrs)", "qty": 1, "unit_cents": 320000},
        {"label": "6 short-form vertical reels (15–60s each, edited)", "qty": 1, "unit_cents": 0},
        {"label": "B-roll package + licensed music + color grade", "qty": 1, "unit_cents": 0},
        {"label": "Delivery via gallery + standard usage rights", "qty": 1, "unit_cents": 0},
    ]},
    "video_premium": {"title": "Videography — Premium", "items": [
        {"label": "Two video shoot days (up to 16 hrs total)", "qty": 1, "unit_cents": 580000},
        {"label": "10 short-form reels + 1 hero brand video (60–90s)", "qty": 1, "unit_cents": 0},
        {"label": "B-roll package, color grade, licensed music", "qty": 1, "unit_cents": 0},
        {"label": "Rush turnaround available", "qty": 1, "unit_cents": 0},
        {"label": "Delivery via gallery + extended usage rights", "qty": 1, "unit_cents": 0},
    ]},

    # ── Brand Partner (Monthly Retainer) ────────────────────────────────────
    "retainer_starter": {"title": "Brand Partner — Starter (Monthly)", "items": [
        {"label": "Monthly photo content day (~20 edited images)", "qty": 1, "unit_cents": 140000},
        {"label": "Social crop pack (1:1, 4:5, 9:16) for hero selects", "qty": 1, "unit_cents": 0},
        {"label": "Standing client portal", "qty": 1, "unit_cents": 0},
        {"label": "Priority scheduling (24-hr response)", "qty": 1, "unit_cents": 0},
    ]},
    "retainer_standard": {"title": "Brand Partner — Standard (Monthly)", "items": [
        {"label": "Monthly photo + short-form video content day", "qty": 1, "unit_cents": 220000},
        {"label": "~30 edited images + 3 short-form reels", "qty": 1, "unit_cents": 0},
        {"label": "Social crop pack (1:1, 4:5, 9:16) for every select", "qty": 1, "unit_cents": 0},
        {"label": "Standing portal + priority scheduling", "qty": 1, "unit_cents": 0},
    ]},
    "retainer_premium": {"title": "Brand Partner — Premium (Monthly)", "items": [
        {"label": "Two content days/month (photo + video)", "qty": 1, "unit_cents": 380000},
        {"label": "~50 edited images + 6 short-form reels", "qty": 1, "unit_cents": 0},
        {"label": "Quarterly hero brand video (60–90s)", "qty": 1, "unit_cents": 0},
        {"label": "Full social crop pack + extended usage rights", "qty": 1, "unit_cents": 0},
        {"label": "Standing portal + concierge scheduling", "qty": 1, "unit_cents": 0},
    ]},

    # ── Portrait Sessions ───────────────────────────────────────────────────
    "portrait_starter": {"title": "Portrait Session — Tier I", "items": [
        {"label": "Portrait session (~1 hr, one look)", "qty": 1, "unit_cents": 35000},
        {"label": "Up to 10 edited, web-ready portraits", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + personal-use rights", "qty": 1, "unit_cents": 0},
    ]},
    "portrait_standard": {"title": "Portrait Session — Tier II", "items": [
        {"label": "Portrait session (~2 hrs, two looks)", "qty": 1, "unit_cents": 60000},
        {"label": "Up to 25 edited, web-ready portraits", "qty": 1, "unit_cents": 0},
        {"label": "Wardrobe change + location guidance", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + personal-use rights", "qty": 1, "unit_cents": 0},
    ]},
    "portrait_premium": {"title": "Portrait Session — Tier III", "items": [
        {"label": "Extended portrait session (~3 hrs, multiple looks)", "qty": 1, "unit_cents": 85000},
        {"label": "Up to 40 edited, web-ready portraits", "qty": 1, "unit_cents": 0},
        {"label": "Multiple looks + on-location options", "qty": 1, "unit_cents": 0},
        {"label": "Rush turnaround available", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + extended personal-use rights", "qty": 1, "unit_cents": 0},
    ]},

    # ── Brand Sessions ──────────────────────────────────────────────────────
    "brand_halfday": {"title": "Brand Session — Half-Day", "items": [
        {"label": "Half-day brand session (up to 4 hrs)", "qty": 1, "unit_cents": 85000},
        {"label": "Personal-brand + headshot mix (~25 edited images)", "qty": 1, "unit_cents": 0},
        {"label": "Social crops (1:1, 4:5, 9:16) for hero selects", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + commercial usage rights", "qty": 1, "unit_cents": 0},
    ]},
    "brand_full": {"title": "Brand Session — Full Package", "items": [
        {"label": "Full-day brand session (up to 8 hrs)", "qty": 1, "unit_cents": 150000},
        {"label": "Headshots, lifestyle, product & workspace (~50 edited images)", "qty": 1, "unit_cents": 0},
        {"label": "Full social crop pack (1:1, 4:5, 9:16) for every select", "qty": 1, "unit_cents": 0},
        {"label": "Brand-story direction + shot planning", "qty": 1, "unit_cents": 0},
        {"label": "Online gallery delivery + extended commercial usage rights", "qty": 1, "unit_cents": 0},
    ]},
}

MAX_ITEM_ROWS = 12


def get_proposal(proposal_id: int) -> "db.sqlite3.Row":
    d = db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
    if not d:
        raise HTTPException(status_code=404)
    return d


def parse_items(form) -> tuple[str, int]:
    """Collect item_label_N / item_qty_N / item_price_N rows → (json, total_cents)."""
    items, total = [], 0
    for i in range(MAX_ITEM_ROWS):
        label = (form.get(f"item_label_{i}") or "").strip()
        if not label:
            continue
        try:
            qty = max(1, int(form.get(f"item_qty_{i}") or "1"))
            unit_cents = round(float(form.get(f"item_price_{i}") or "0") * 100)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad numbers on row {i + 1}")
        items.append({"label": label, "qty": qty, "unit_cents": unit_cents})
        total += qty * unit_cents
    return json.dumps(items), total


@router.post("/projects/{project_id}/proposals")
async def create_proposal(project_id: int, preset: str = Form("blank")):
    p = get_project(project_id)
    tpl = PRESETS.get(preset, PRESETS["blank"])
    intro = None if preset == "blank" else OUR_STORY_INTRO
    did = db.run("""INSERT INTO proposals (project_id, slug, title, intro, line_items, total_cents)
                    VALUES (?,?,?,?,?,?)""",
                 (project_id, security.new_slug(), f"{tpl['title']} — {p['title']}",
                  intro,
                  json.dumps(tpl["items"]),
                  sum(i["qty"] * i["unit_cents"] for i in tpl["items"])))
    log.info("proposal %s created for project %s (preset=%s)", did, project_id, preset)
    return RedirectResponse(f"/admin/studio/proposals/{did}", status_code=303)


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_detail(request: Request, proposal_id: int):
    d = get_proposal(proposal_id)
    p = get_project(d["project_id"])
    items = json.loads(d["line_items"])
    rows = items + [{} for _ in range(max(0, MAX_ITEM_ROWS - len(items)))]
    return templates.TemplateResponse(request, "admin/proposal.html",
                                      {"d": d, "p": p, "rows": rows,
                                       "base_url": config.BASE_URL})


@router.post("/proposals/{proposal_id}")
async def update_proposal(request: Request, proposal_id: int):
    d = get_proposal(proposal_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent proposals are locked")
    form = await request.form()
    items_json, total = parse_items(form)
    db.run("UPDATE proposals SET title=?, intro=?, line_items=?, total_cents=? WHERE id=?",
           ((form.get("title") or "").strip() or d["title"],
            (form.get("intro") or "").strip() or None, items_json, total, proposal_id))
    return RedirectResponse(f"/admin/studio/proposals/{proposal_id}", status_code=303)


@router.post("/proposals/{proposal_id}/convert")
async def convert_proposal(proposal_id: int):
    """Once a client accepts, spawn the matching draft contract + draft invoice in
    one click instead of rebuilding both by hand. Both are DRAFTS — Kevin still
    reviews and hits Send/Issue (R16); nothing is sent or charged here. The invoice
    copies this proposal's line items/total verbatim; the contract is the standard
    body snapshot with the accepted total merged in (same logic as create_contract)."""
    d = get_proposal(proposal_id)
    if d["status"] != "accepted":
        raise HTTPException(status_code=400,
                            detail="only an accepted proposal can be converted")
    p = get_project(d["project_id"])
    cid = db.run("INSERT INTO contracts (project_id, slug, title, body) VALUES (?,?,?,?)",
                 (p["id"], security.new_slug(),
                  f"Services Agreement — {p['title']}", render_template(p)))
    iid = db.run("""INSERT INTO invoices (project_id, slug, title, line_items, total_cents)
                    VALUES (?,?,?,?,?)""",
                 (p["id"], security.new_slug(), f"Invoice — {p['title']}",
                  d["line_items"], d["total_cents"]))
    log.info("proposal %s converted → contract %s + invoice %s", proposal_id, cid, iid)
    return RedirectResponse(f"/admin/studio/projects/{p['id']}", status_code=303)


@router.post("/proposals/{proposal_id}/duplicate")
async def duplicate_proposal(proposal_id: int):
    """Clone a locked proposal (sent/viewed/accepted/declined) into a fresh
    editable draft — the revise-and-re-send path. Copies title/intro/line items
    into a new proposal with its own slug; the original is untouched. Useful when
    a client declines and wants changes, or to reuse a package for someone new."""
    d = get_proposal(proposal_id)
    did = db.run("""INSERT INTO proposals (project_id, slug, title, intro, line_items,
                    total_cents) VALUES (?,?,?,?,?,?)""",
                 (d["project_id"], security.new_slug(), d["title"], d["intro"],
                  d["line_items"], d["total_cents"]))
    log.info("proposal %s duplicated → %s (new draft)", proposal_id, did)
    return RedirectResponse(f"/admin/studio/proposals/{did}", status_code=303)


@router.post("/proposals/{proposal_id}/send")
async def mark_proposal_sent(proposal_id: int):
    d = get_proposal(proposal_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    db.run("UPDATE proposals SET status='sent', sent_at=datetime('now') WHERE id=?",
           (proposal_id,))
    db.run("UPDATE projects SET status='proposal_sent', "
           "stage_changed_at=datetime('now') WHERE id=? "
           "AND status IN ('inquiry_received','consultation_call')",
           (d["project_id"],))
    log.info("proposal %s marked sent", proposal_id)
    return RedirectResponse(f"/admin/studio/proposals/{proposal_id}", status_code=303)
