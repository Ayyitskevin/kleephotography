"""Email templates — reusable subject/body snippets for manual client sends.

Content library only: Kevin edits these here, then picks one on a doc's send
form (it fills subject + message with merge fields resolved). Nothing auto-sends;
Odysseus owns automation."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, security
from ..render import templates

log = logging.getLogger("mise.admin.email_templates")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

MERGE_FIELDS = [
    "first_name",
    "client_name",
    "company",
    "project_title",
    "doc_title",
    "doc_url",
    "site_name",
]


@router.get("/email-templates", response_class=HTMLResponse)
async def list_templates(request: Request):
    rows = db.all_("SELECT * FROM email_templates WHERE deleted_at IS NULL ORDER BY name")
    return templates.TemplateResponse(
        request,
        "admin/email_templates.html",
        {"rows": rows, "merge_fields": MERGE_FIELDS, "base_url": config.BASE_URL},
    )


@router.post("/email-templates")
async def create_template(name: str = Form(...), subject: str = Form(...), body: str = Form(...)):
    name, subject = name.strip(), subject.strip()
    if not (name and subject and body.strip()):
        raise HTTPException(status_code=400, detail="name, subject and body required")
    tid = db.run(
        "INSERT INTO email_templates (name, subject, body) VALUES (?,?,?)", (name, subject, body)
    )
    log.info("email template %s created", tid)
    return RedirectResponse("/admin/studio/email-templates", status_code=303)


@router.post("/email-templates/{template_id}")
async def update_template(
    template_id: int, name: str = Form(...), subject: str = Form(...), body: str = Form(...)
):
    d = db.one("SELECT id FROM email_templates WHERE id=? AND deleted_at IS NULL", (template_id,))
    if not d:
        raise HTTPException(status_code=404)
    name, subject = name.strip(), subject.strip()
    if not (name and subject and body.strip()):
        raise HTTPException(status_code=400, detail="name, subject and body required")
    db.run(
        "UPDATE email_templates SET name=?, subject=?, body=? WHERE id=?",
        (name, subject, body, template_id),
    )
    return RedirectResponse("/admin/studio/email-templates", status_code=303)


@router.post("/email-templates/{template_id}/delete")
async def delete_template(template_id: int):
    db.run("UPDATE email_templates SET deleted_at=datetime('now') WHERE id=?", (template_id,))
    log.info("email template %s soft-deleted", template_id)
    return RedirectResponse("/admin/studio/email-templates", status_code=303)
