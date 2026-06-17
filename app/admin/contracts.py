"""Contracts — merge-field template, body locked by SHA-256 at send, typed-name e-sign."""

import hashlib
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, security
from ..render import templates
from .studio import get_project

log = logging.getLogger("mise.admin.contracts")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Imported standard-form contracts kept as files (app/contract_templates/*.md) rather
# than inline strings — they are long legal boilerplate that should stay verbatim and
# diff cleanly. These are DRAFTS pending attorney review; the manual "Send" button is
# the human gate (R16). Merge fields resolve at creation into a self-contained body
# snapshot, so later file edits never alter an already-created contract.
CONTRACT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "contract_templates"
CONTRACT_LIBRARY = {
    "nda": "Unilateral NDA",
    "photography_services": "Photography Services Agreement",
    "photography_agreement": "Photography Agreement (plain-language)",
    "general_service": "General Client Service Agreement",
    "model_release": "Model Release",
    "independent_contractor": "Independent Contractor Agreement",
    "event": "Event Contract",
    "styled_shoot": "Styled Shoot Photo Release",
    "videography_services": "Videography Services Agreement",
    "liability_waiver": "Liability Waiver & Release",
}


def load_library_template(key: str) -> str:
    path = CONTRACT_TEMPLATES_DIR / f"{key}.md"
    return path.read_text(encoding="utf-8")


def resolve_merge(body: str, p: "db.sqlite3.Row") -> str:
    """Fill {field} placeholders from the project/client. Unknown braces and blank
    fill-in lines pass through untouched — same simple-replace convention as render.py,
    never str.format (the legal text contains bare brackets/braces that would break it)."""
    phone = db.one("SELECT phone FROM clients WHERE id=?", (p["client_id"],))
    ctx = {
        "site_name": config.SITE_NAME,
        "client_name": p["client_name"] or "",
        "client_email": p["client_email"] or "",
        "client_phone": (phone["phone"] if phone and phone["phone"] else ""),
    }
    for k, v in ctx.items():
        body = body.replace("{" + k + "}", str(v))
    return body

# Merge fields resolve at creation — the stored body is a self-contained snapshot,
# so later edits to this template never change an existing contract.
DEFAULT_TEMPLATE = """\
PHOTOGRAPHY SERVICES AGREEMENT

This agreement is between {site_name} ("Photographer") and {client_name}{company_clause} ("Client"), dated {date}, for the project "{project_title}".

1. SCOPE — Photographer will provide the photography services described in the accepted proposal{total_clause}. Deliverables are edited digital images delivered via private online gallery.

2. PAYMENT — Per the associated invoice. A deposit, when specified, is due to reserve the shoot date and is non-refundable within 7 days of the shoot. Balance is due on delivery of the final gallery.

3. RESCHEDULING & CANCELLATION — Either party may reschedule with at least 7 days' notice at no charge. Client cancellation within 7 days of the shoot forfeits the deposit.

4. USAGE RIGHTS — Client receives a non-exclusive, perpetual license to use delivered images for marketing, menus, websites, and social media. Photographer retains copyright and may use the images for portfolio and self-promotion unless Client opts out in writing.

5. DELIVERY — Final edited images are delivered within 10 business days of the shoot unless otherwise agreed.

6. LIABILITY — Photographer's total liability is limited to the amount paid under this agreement. In the unlikely event of equipment failure or loss of images, the remedy is a reshoot or refund.

7. E-SIGNATURE — Both parties agree that a typed name submitted through this page constitutes a legal signature under the U.S. ESIGN Act.
"""


def get_contract(contract_id: int) -> "db.sqlite3.Row":
    d = db.one("SELECT * FROM contracts WHERE id=?", (contract_id,))
    if not d:
        raise HTTPException(status_code=404)
    return d


def render_template(p: "db.sqlite3.Row") -> str:
    accepted = db.one("""SELECT total_cents FROM proposals
                         WHERE project_id=? AND status='accepted'
                         ORDER BY accepted_at DESC LIMIT 1""", (p["id"],))
    total_clause = (" for a total of $%.2f" % (accepted["total_cents"] / 100)
                    if accepted else "")
    company_clause = f" of {p['company']}" if p["company"] else ""
    return DEFAULT_TEMPLATE.format(
        site_name=config.SITE_NAME, client_name=p["client_name"],
        company_clause=company_clause, date=date.today().isoformat(),
        project_title=p["title"], total_clause=total_clause)


@router.post("/projects/{project_id}/contracts")
async def create_contract(project_id: int, template_key: str = Form("standard")):
    p = get_project(project_id)
    if template_key in CONTRACT_LIBRARY:
        body = resolve_merge(load_library_template(template_key), p)
        title = f"{CONTRACT_LIBRARY[template_key]} — {p['title']}"
    else:
        body = render_template(p)
        title = f"Services Agreement — {p['title']}"
    did = db.run("""INSERT INTO contracts (project_id, slug, title, body)
                    VALUES (?,?,?,?)""",
                 (project_id, security.new_slug(), title, body))
    log.info("contract %s created for project %s (template=%s)",
             did, project_id, template_key)
    return RedirectResponse(f"/admin/studio/contracts/{did}", status_code=303)


@router.get("/contracts/{contract_id}", response_class=HTMLResponse)
async def contract_detail(request: Request, contract_id: int):
    d = get_contract(contract_id)
    p = get_project(d["project_id"])
    return templates.TemplateResponse(request, "admin/contract.html",
                                      {"d": d, "p": p, "base_url": config.BASE_URL})


@router.post("/contracts/{contract_id}")
async def update_contract(contract_id: int, title: str = Form(...), body: str = Form(...)):
    d = get_contract(contract_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="sent contracts are locked")
    if not body.strip():
        raise HTTPException(status_code=400, detail="body required")
    db.run("UPDATE contracts SET title=?, body=? WHERE id=?",
           (title.strip() or d["title"], body, contract_id))
    return RedirectResponse(f"/admin/studio/contracts/{contract_id}", status_code=303)


@router.post("/contracts/{contract_id}/duplicate")
async def duplicate_contract(contract_id: int):
    """Clone a locked contract (sent/viewed/signed) into a fresh editable draft.
    Copies the resolved body snapshot + title under a new slug; the new draft has
    no hash or signature until it is sent and signed in its own right. The original
    is untouched."""
    d = get_contract(contract_id)
    did = db.run("INSERT INTO contracts (project_id, slug, title, body) VALUES (?,?,?,?)",
                 (d["project_id"], security.new_slug(), d["title"], d["body"]))
    log.info("contract %s duplicated → %s (new draft)", contract_id, did)
    return RedirectResponse(f"/admin/studio/contracts/{did}", status_code=303)


@router.post("/contracts/{contract_id}/send")
async def mark_contract_sent(contract_id: int):
    d = get_contract(contract_id)
    if d["status"] != "draft":
        raise HTTPException(status_code=400, detail="already sent")
    sha = hashlib.sha256(d["body"].encode()).hexdigest()
    db.run("""UPDATE contracts SET status='sent', body_sha256=?, sent_at=datetime('now')
              WHERE id=?""", (sha, contract_id))
    log.info("contract %s marked sent (sha256=%s)", contract_id, sha[:12])
    return RedirectResponse(f"/admin/studio/contracts/{contract_id}", status_code=303)
