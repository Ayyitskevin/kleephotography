"""Manual email sends for Studio docs — Kevin clicks Send; nothing auto-sends."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException

from .. import db, mailer, security
from ..redirects import redirect_local

log = logging.getLogger("mise.admin.emails")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

KINDS = {"proposals": "proposal", "contracts": "contract", "invoices": "invoice"}


def _doc_table(kind: str) -> str:
    """Table name for a studio-doc path segment.

    `kind` is the caller's path parameter, and a table name cannot be a bound
    parameter. The name spliced into SQL is one of the literals below, not
    `kind`. ``db.ident`` still rejects a literal that is not a ``KINDS`` key.
    Checking ``kind not in KINDS`` and then interpolating ``ident(kind)`` does
    not: CodeQL's const-compare barrier only sees an inline collection of
    literals, and ``ident`` returns its argument, so the path parameter was
    followed into ``db.one``.
    """
    if kind == "proposals":
        table = "proposals"
    elif kind == "contracts":
        table = "contracts"
    elif kind == "invoices":
        table = "invoices"
    else:
        raise HTTPException(status_code=404)
    return db.ident(table, KINDS)


@router.post("/{kind}/{doc_id}/email")
def email_doc(
    kind: str, doc_id: int, to: str = Form(...), subject: str = Form(...), message: str = Form(...)
):
    table = _doc_table(kind)
    d = db.get_or_404(f"SELECT * FROM {table} WHERE id=?", (doc_id,))
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
    return redirect_local(f"/admin/studio/{kind}/{doc_id}")
