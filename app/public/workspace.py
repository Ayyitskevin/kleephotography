"""Client project workspace (#1) — one PIN-gated hub per project.

Aggregates the client's SENT documents (proposal/contract/invoice) and the
delivered gallery into a single page, and links out to the canonical action
pages (/p /c /i). Read-only: it never accepts, signs, charges, or creates an
obligation here — those stay on the doc pages. Reuses the gallery/portal PIN
machinery; lockout rows are namespaced with a large positive offset so project
ids can't collide with gallery ids (which share the positive range)."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, security
from ..render import templates
from . import gallery as public_gallery

log = logging.getLogger("mise.public.workspace")
router = APIRouter(prefix="/w")

# Disjoint pin_attempts namespace: galleries use positive ids, portals/inquiry
# buckets use negatives. Offset project ids well past any real gallery id.
PIN_OFFSET = 2_000_000


def get_live_workspace(slug: str) -> "db.sqlite3.Row":
    p = db.one(
        """SELECT pr.*, c.name AS client_name, c.company, c.email AS client_email
                  FROM projects pr JOIN clients c ON c.id=pr.client_id
                  WHERE pr.workspace_slug=?""",
        (slug,),
    )
    if not p or not p["workspace_published"]:
        raise HTTPException(status_code=404)
    return p


def _cookie_name(project_id: int) -> str:
    return f"mise_w{project_id}"


def _has_access(request: Request, project_id: int) -> bool:
    raw = request.cookies.get(_cookie_name(project_id))
    return bool(raw) and security.unsign(raw) == f"workspace:{project_id}"


@router.get("/{slug}", response_class=HTMLResponse)
async def view(request: Request, slug: str):
    p = get_live_workspace(slug)
    if not _has_access(request, p["id"]):
        return templates.TemplateResponse(
            request, "public/workspace_pin.html", {"p": p, "error": None}
        )
    # Clients only ever see docs that have left draft (drafts are private WIP).
    proposals = db.all_(
        """SELECT slug, title, status, total_cents FROM proposals
                           WHERE project_id=? AND status!='draft'
                           ORDER BY created_at DESC""",
        (p["id"],),
    )
    contracts = db.all_(
        """SELECT slug, title, status FROM contracts
                           WHERE project_id=? AND status!='draft'
                           ORDER BY created_at DESC""",
        (p["id"],),
    )
    invoices = db.all_(
        """SELECT slug, title, status, total_cents, deposit_cents, due_date
                          FROM invoices WHERE project_id=? AND status!='draft'
                          ORDER BY created_at DESC""",
        (p["id"],),
    )
    gallery = None
    gallery_expired = False
    if p["gallery_id"]:
        gallery = db.one(
            "SELECT slug, title, expires_at FROM galleries WHERE id=? AND published=1",
            (p["gallery_id"],),
        )
        # An expired gallery 410s at /g/{slug}; render its card unlinked so the
        # workspace never sends the client to a dead end.
        if gallery:
            gallery_expired = public_gallery.is_expired(gallery)
    biz = p["company"] or p["client_name"]
    share_subject = quote(f"Your project workspace — {biz}")
    share_body = quote(
        f"Here's the workspace for {p['title']}:\n\n"
        f"{config.BASE_URL}/w/{p['workspace_slug']}\n"
        f"PIN: {p['workspace_pin']}\n\n"
        f"It has your proposal, agreement, invoice, and gallery in one place.\n"
    )
    share_href = f"mailto:?subject={share_subject}&body={share_body}"
    return templates.TemplateResponse(
        request,
        "public/workspace.html",
        {
            "p": p,
            "proposals": proposals,
            "contracts": contracts,
            "invoices": invoices,
            "gallery": gallery,
            "gallery_expired": gallery_expired,
            "share_href": share_href,
        },
    )


@router.post("/{slug}/pin")
async def check_pin(request: Request, slug: str, pin: str = Form(...)):
    p = get_live_workspace(slug)
    ip = security.client_ip(request)
    key = PIN_OFFSET + p["id"]
    if security.pin_locked(ip, key):
        return templates.TemplateResponse(
            request,
            "public/workspace_pin.html",
            {"p": p, "error": f"Too many tries — wait {config.PIN_LOCKOUT_MIN} minutes."},
            status_code=429,
        )
    if pin.strip() != p["workspace_pin"]:
        security.pin_fail(ip, key)
        return templates.TemplateResponse(
            request, "public/workspace_pin.html", {"p": p, "error": "Wrong PIN."}, status_code=401
        )
    security.pin_clear(ip, key)
    resp = RedirectResponse(f"/w/{slug}", status_code=303)
    security.set_signed_session_cookie(resp, _cookie_name(p["id"]), f"workspace:{p['id']}")
    return resp
