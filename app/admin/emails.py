"""Manual email sends for Studio docs — Kevin clicks Send; nothing auto-sends."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from .. import db, mailer, security

log = logging.getLogger("mise.admin.emails")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

KINDS = {"proposals": "proposal", "contracts": "contract", "invoices": "invoice"}


@router.post("/{kind}/{doc_id}/email")
async def email_doc(
    kind: str, doc_id: int, to: str = Form(...), subject: str = Form(...), message: str = Form(...)
):
    if kind not in KINDS:
        raise HTTPException(status_code=404)
    d = db.one(f"SELECT * FROM {db.ident(kind, KINDS)} WHERE id=?", (doc_id,))
    if not d:
        raise HTTPException(status_code=404)
    if d["status"] == "draft":
        raise HTTPException(
            status_code=400,
            detail="mark the document sent first — drafts are invisible at the client link",
        )
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="email is not configured")
    to, subject = to.strip(), subject.strip()
    if not to or not subject:
        raise HTTPException(status_code=400, detail="to and subject required")
    try:
        mailer.send(to, subject, message)
    except Exception:
        log.exception("send failed for %s %s", KINDS[kind], doc_id)
        raise HTTPException(status_code=502, detail="SMTP send failed — check logs")
    db.run(
        """INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject)
              VALUES (?,?,?,?,?)""",
        (d["project_id"], KINDS[kind], doc_id, to, subject),
    )
    log.info("emailed %s %s (doc %s)", KINDS[kind], doc_id, d["slug"])
    return RedirectResponse(f"/admin/studio/{kind}/{doc_id}", status_code=303)
