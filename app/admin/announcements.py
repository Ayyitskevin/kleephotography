"""Compose-and-send for list announcements. One page: the audience it will
reach, one subject, one plain-text body, the history of what already went out.
The mini-session flywheel's second half — pay-to-book makes the slots, this
tells the people who already trust the work."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import announcements, db, mailer, security
from ..render import templates

log = logging.getLogger("mise.admin.announcements")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

# Gmail's practical daily send ceiling. The page warns rather than blocks —
# Kevin may be on Workspace (2k/day) — but silence here would look like a bug
# when the tail of a big send starts failing.
_GMAIL_DAILY_WARN = 400


@router.get("/announcements", response_class=HTMLResponse)
def compose(request: Request, sent: int = 0):
    people = announcements.audience()
    history = db.all_("SELECT * FROM announcements ORDER BY id DESC LIMIT 20")
    return templates.TemplateResponse(
        request,
        "admin/announcements.html",
        {
            "audience_n": len(people),
            "history": history,
            "mailer_on": mailer.configured(),
            "gmail_warn": len(people) > _GMAIL_DAILY_WARN,
            "just_sent": sent,
        },
    )


@router.post("/announcements/send")
def send(subject: str = Form(...), body: str = Form(...)):
    subject, body = subject.strip(), body.strip()
    if not subject or not body:
        raise HTTPException(status_code=400, detail="subject and body are both required")
    if len(body) > 10_000:
        raise HTTPException(status_code=400, detail="body too long")
    if not mailer.configured():
        # Fail loud before staging anything: a queue full of jobs that can only
        # fail is noise, not a send.
        raise HTTPException(status_code=503, detail="mailer is not configured")
    aid, n = announcements.enqueue_send(subject, body)
    log.info("announcement %s queued to %d recipients", aid, n)
    return RedirectResponse(f"/admin/announcements?sent={n}", status_code=303)
