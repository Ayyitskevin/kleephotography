"""Reference — Kevin's internal "Playbook & reference" shelf.

NOT client-facing: no public route, no portal surface, no Send action. One
source of truth for the booking script (what to say at each pipeline stage),
the pricing reference (derived from the real proposal presets — quoting-first,
so unset collections read "set rate"), and the shoot-day kit checklist.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import security
from ..render import templates
from .doc_templates import _proposal_groups

log = logging.getLogger("mise.admin.reference")
router = APIRouter(prefix="/admin/reference",
                   dependencies=[Depends(security.require_admin)])

# Booking script keyed to the eight real PROJECT_STATUSES (studio.py). Advisory
# copy for Kevin to edit — the dot colour matches the pipeline stage swatch.
PLAYBOOK_STAGES = [
    {"stage": "Inquiry", "dot": "#5C6A5E",
     "note": "Reply within the hour. Ask their menu size, the goal "
             "(reservations? socials? a refresh?), and their timeline.",
     "voice": "Love that you reached out — what are you hoping these photos "
              "do for you?"},
    {"stage": "Consultation Call", "dot": "#2a5db0",
     "note": "A 20-minute call to scope the shoot. Walk the menu, the room, "
             "and the deadline. Listen for the hero dishes.",
     "voice": "Tell me about the dishes you’re proudest of — those are the "
              "ones we lead with."},
    {"stage": "Proposal Sent", "dot": "#9a7a2c",
     "note": "Recommend a tier, not a menu. Anchor on the outcome and include "
             "the social crops in every option.",
     "voice": "For a menu this size I’d suggest a half-day — here’s exactly "
              "what you’d get."},
    {"stage": "Contract Signed", "dot": "#7C2F38",
     "note": "Send the agreement for a typed-name e-signature. Scope, usage, "
             "and cancellation are all in writing.",
     "voice": "Here’s the agreement — sign at the bottom and you’re on the "
              "calendar."},
    {"stage": "Retainer Paid", "dot": "#2f7d57",
     "note": "Collect the booking retainer before the date is held. No "
             "retainer, no hold — protects against last-minute drops.",
     "voice": "Once the retainer’s in, the date is yours and we start "
              "planning."},
    {"stage": "Session Planning", "dot": "#1f6f6b",
     "note": "Build the shot list together. Hero dishes first, then the room. "
             "Arrive 30 min early and shoot while the kitchen is fresh.",
     "voice": "Send the first four whenever the chef is ready — I’ll stay out "
              "of the pass."},
    {"stage": "Project Closed", "dot": "#143C2F",
     "note": "Gallery within the week. Lead with their favourite. Crops "
             "auto-made. Balance invoice on delivery.",
     "voice": "Here’s your gallery — your favourites are already cropped for "
              "every platform."},
    {"stage": "Archived", "dot": "#8A9183",
     "note": "Books wrapped and files backed up. Note anything to remember "
             "for next season, then archive the project.",
     "voice": "Loved working with you — I’ll reach out before your next menu "
              "change."},
]

SHOOT_KIT = [
    "Two bodies + 35 / 50 / 100mm macro",
    "Tripod + clamp arms",
    "Diffusion + reflector",
    "Steam wand + spray bottle",
    "Tweezers, brushes, finishing oil",
    "Backup cards + drive",
]


def _pricing_rows() -> list[dict]:
    """One row per proposal collection group, priced from its cheapest preset.
    Quoting-first: groups with no figure set read 'set rate', never a fake one."""
    rows = []
    for g in _proposal_groups():
        priced = [c["total_cents"] for c in g["cards"] if c["total_cents"]]
        if priced:
            price = "from $" + f"{round(min(priced) / 100):,}"
        else:
            price = "set rate"
        rows.append({"item": g["label"], "price": price})
    return rows


@router.get("", response_class=HTMLResponse)
async def reference(request: Request):
    return templates.TemplateResponse(
        request, "admin/reference.html",
        {"stages": PLAYBOOK_STAGES, "pricing": _pricing_rows(), "kit": SHOOT_KIT})
