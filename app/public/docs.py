"""Client-facing Studio documents — proposals /p/{slug}, contracts /c/{slug}."""

import hashlib
import json
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, security
from ..render import templates

log = logging.getLogger("mise.public.docs")
router = APIRouter()


def _proposal_or_404(slug: str) -> "db.sqlite3.Row":
    d = db.one(
        """SELECT pr.*, p.title AS project_title, c.name AS client_name, c.company
                  FROM proposals pr
                  JOIN projects p ON p.id=pr.project_id
                  JOIN clients c ON c.id=p.client_id
                  WHERE pr.slug=?""",
        (slug,),
    )
    if not d or d["status"] == "draft":
        raise HTTPException(status_code=404)
    return d


@router.get("/p/{slug}", response_class=HTMLResponse)
async def view_proposal(request: Request, slug: str):
    d = _proposal_or_404(slug)
    if d["status"] == "sent":
        db.run(
            "UPDATE proposals SET status='viewed', viewed_at=datetime('now') WHERE id=?", (d["id"],)
        )
        log.info("proposal %s viewed from %s", d["id"], security.client_ip(request))
    return templates.TemplateResponse(
        request, "public/proposal.html", {"d": d, "items": json.loads(d["line_items"])}
    )


@router.post("/p/{slug}/accept")
async def accept_proposal(request: Request, slug: str):
    d = _proposal_or_404(slug)
    if d["status"] not in ("sent", "viewed"):
        raise HTTPException(status_code=400, detail="proposal is not open for acceptance")
    db.run(
        "UPDATE proposals SET status='accepted', accepted_at=datetime('now') WHERE id=?", (d["id"],)
    )
    log.info("proposal %s ACCEPTED from %s", d["id"], security.client_ip(request))
    return RedirectResponse(f"/p/{slug}", status_code=303)


@router.post("/p/{slug}/decline")
async def decline_proposal(request: Request, slug: str):
    d = _proposal_or_404(slug)
    if d["status"] not in ("sent", "viewed"):
        raise HTTPException(status_code=400, detail="proposal is not open")
    db.run("UPDATE proposals SET status='declined' WHERE id=?", (d["id"],))
    log.info("proposal %s declined from %s", d["id"], security.client_ip(request))
    return RedirectResponse(f"/p/{slug}", status_code=303)


def _contract_or_404(slug: str) -> "db.sqlite3.Row":
    d = db.one(
        """SELECT ct.*, p.title AS project_title, c.name AS client_name, c.company
                  FROM contracts ct
                  JOIN projects p ON p.id=ct.project_id
                  JOIN clients c ON c.id=p.client_id
                  WHERE ct.slug=?""",
        (slug,),
    )
    if not d or d["status"] == "draft":
        raise HTTPException(status_code=404)
    return d


@router.get("/c/{slug}", response_class=HTMLResponse)
async def view_contract(request: Request, slug: str):
    d = _contract_or_404(slug)
    if d["status"] == "sent":
        db.run(
            "UPDATE contracts SET status='viewed', viewed_at=datetime('now') WHERE id=?", (d["id"],)
        )
        log.info("contract %s viewed from %s", d["id"], security.client_ip(request))
    return templates.TemplateResponse(request, "public/contract.html", {"d": d})


@router.post("/c/{slug}/sign")
async def sign_contract(
    request: Request, slug: str, signer_name: str = Form(...), agree: str = Form(...)
):
    d = _contract_or_404(slug)
    if d["status"] not in ("sent", "viewed"):
        raise HTTPException(status_code=400, detail="contract is not open for signing")
    if not signer_name.strip():
        raise HTTPException(status_code=400, detail="typed name required")
    # ESIGN integrity: refuse if the body no longer matches the hash locked at send
    if hashlib.sha256(d["body"].encode()).hexdigest() != d["body_sha256"]:
        log.error("contract %s body hash mismatch — refusing signature", d["id"])
        raise HTTPException(status_code=409, detail="contract integrity check failed")
    db.run(
        """UPDATE contracts SET status='signed', signer_name=?, signer_ip=?,
              signed_at=datetime('now') WHERE id=?""",
        (signer_name.strip(), security.client_ip(request), d["id"]),
    )
    db.run(
        """UPDATE projects SET status='contract_signed',
              stage_changed_at=datetime('now') WHERE id=?
              AND status IN ('inquiry_received','consultation_call','proposal_sent')""",
        (d["project_id"],),
    )
    log.info("contract %s SIGNED from %s", d["id"], security.client_ip(request))
    return RedirectResponse(f"/c/{slug}", status_code=303)


# ── Testimonial capture — client writes their own at /t/{slug} ───────────────


def _testimonial_request_or_404(slug: str) -> "db.sqlite3.Row":
    r = db.one(
        """SELECT tr.*, c.name AS client_name, c.company
                  FROM testimonial_requests tr
                  JOIN clients c ON c.id=tr.client_id
                  WHERE tr.slug=?""",
        (slug,),
    )
    if not r:
        raise HTTPException(status_code=404)
    return r


@router.get("/t/{slug}", response_class=HTMLResponse)
async def view_testimonial_form(request: Request, slug: str):
    r = _testimonial_request_or_404(slug)
    return templates.TemplateResponse(
        request, "public/testimonial.html", {"r": r, "submitted": bool(r["submitted_at"])}
    )


@router.post("/t/{slug}", response_class=HTMLResponse)
async def submit_testimonial(
    request: Request,
    slug: str,
    quote: str = Form(...),
    attribution_name: str = Form(...),
    business: str = Form(""),
):
    r = _testimonial_request_or_404(slug)
    if r["submitted_at"]:  # one submission per link; idempotent thank-you
        return RedirectResponse(f"/t/{slug}", status_code=303)
    if not (quote.strip() and attribution_name.strip()):
        raise HTTPException(status_code=400, detail="quote and name required")
    # Lands unpublished — the existing /admin/studio/testimonials moderation
    # decides if/when it goes live on the marketing site.
    tid = db.run(
        """INSERT INTO testimonials (quote, attribution_name, business,
                                              gallery_id, published)
                    VALUES (?,?,?,?,0)""",
        (quote.strip(), attribution_name.strip(), business.strip() or None, r["gallery_id"]),
    )
    db.run(
        """UPDATE testimonial_requests
              SET submitted_at=datetime('now'), testimonial_id=? WHERE id=?""",
        (tid, r["id"]),
    )
    log.info("testimonial submitted via request %s -> testimonial %s", r["id"], tid)
    return RedirectResponse(f"/t/{slug}", status_code=303)
