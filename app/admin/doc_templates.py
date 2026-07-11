"""Templates gallery — a browsable shelf of the studio's ready-made client
documents (proposals, invoices, contracts).

This is a VIEW over the template definitions that already exist, not a new
store. Cards reference the same PRESETS (proposals) and CONTRACT_LIBRARY
(contracts) the per-project create routes already use; "Use this template"
picks a project and hands off to those exact create handlers, so the
populate -> edit -> send flow is the one that already ships. Nothing here
forks document creation, and no document leaves until its own Send button is
pressed (R16 — the manual gate stays on the proposal/invoice/contract pages).
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, security
from ..render import templates
from .contracts import CONTRACT_LIBRARY, create_contract
from .invoices import create_invoice
from .proposals import PRESETS, create_proposal

log = logging.getLogger("mise.admin.doc_templates")
router = APIRouter(prefix="/admin/templates", dependencies=[Depends(security.require_admin)])

# Curated gallery layout. Keys reference template definitions the create routes
# already understand (proposals.PRESETS / contracts.CONTRACT_LIBRARY); only the
# grouping and blurbs live here. A new preset shows up automatically once it is
# added to a group below — no second source of truth for the content itself.
PROPOSAL_GROUPS = [
    ("Photography", ["photo_starter", "photo_standard", "photo_premium"]),
    ("Videography", ["video_starter", "video_standard", "video_premium"]),
    ("Brand Partner — Monthly", ["retainer_starter", "retainer_standard", "retainer_premium"]),
    ("Portrait Sessions", ["portrait_starter", "portrait_standard", "portrait_premium"]),
    ("Brand Sessions", ["brand_halfday", "brand_full"]),
]

CONTRACT_ORDER = [
    "photography_services",
    "photography_agreement",
    "real_estate_services",
    "portrait_services",
    "videography_services",
    "general_service",
    "model_release",
    "styled_shoot",
    "independent_contractor",
    "event",
    "liability_waiver",
    "nda",
]
CONTRACT_BLURBS = {
    "photography_services": "Master services agreement — scope, usage rights, "
    "payment, reschedule & cancellation.",
    "photography_agreement": "Plain-language alternative — same protections, "
    "client-friendly wording. Draft, attorney review advised.",
    "real_estate_services": "Listing shoots — property access & readiness, "
    "weather/twilight rescheduling, MLS usage license. Draft, attorney review advised.",
    "portrait_services": "Portrait & team sessions — likeness release, usage "
    "rights, portfolio opt-out. Draft, attorney review advised.",
    "videography_services": "For video shoots — deliverables, licensing, and usage terms.",
    "general_service": "All-purpose client service agreement for non-shoot work.",
    "model_release": "Permission to use a subject's likeness in delivered images.",
    "styled_shoot": "Photo release for collaborative / styled shoots.",
    "independent_contractor": "For second shooters or assistants you bring on.",
    "event": "Event-day coverage terms.",
    "liability_waiver": "Liability & release for on-location shoots.",
    "nda": "Mutual confidentiality for sensitive brand work.",
}


def _proposal_groups() -> list[dict]:
    groups = []
    for label, keys in PROPOSAL_GROUPS:
        cards = []
        for k in keys:
            tpl = PRESETS.get(k)
            if not tpl:
                continue
            cards.append(
                {
                    "key": k,
                    "title": tpl["title"],
                    "lines": [i["label"] for i in tpl["items"]],
                    "total_cents": sum(i["qty"] * i["unit_cents"] for i in tpl["items"]),
                }
            )
        if cards:
            groups.append({"label": label, "cards": cards})
    return groups


def _contract_cards() -> list[dict]:
    return [
        {"key": k, "title": CONTRACT_LIBRARY[k], "blurb": CONTRACT_BLURBS.get(k, "")}
        for k in CONTRACT_ORDER
        if k in CONTRACT_LIBRARY
    ]


@router.get("", response_class=HTMLResponse)
async def gallery(request: Request):
    projects = db.all_(
        """SELECT p.id, p.title, c.name AS client_name
           FROM projects p JOIN clients c ON c.id=p.client_id
           WHERE p.status != 'archived'
           ORDER BY p.created_at DESC"""
    )
    return templates.TemplateResponse(
        request,
        "admin/templates_gallery.html",
        {
            "proposal_groups": _proposal_groups(),
            "contracts": _contract_cards(),
            "projects": projects,
        },
    )


@router.post("/use")
async def use_template(project_id: int = Form(...), doc_type: str = Form(...), key: str = Form("")):
    """Dispatch to the existing per-project create handler for the chosen doc
    type. Each handler validates the project (404s if missing), creates the
    populated draft, and returns a 303 to its own editor — so the gallery never
    duplicates creation logic."""
    if doc_type == "proposal":
        return await create_proposal(project_id, preset=key or "blank")
    if doc_type == "invoice":
        return await create_invoice(project_id)
    if doc_type == "contract":
        return await create_contract(project_id, template_key=key or "standard")
    raise HTTPException(status_code=400, detail="bad doc_type")
