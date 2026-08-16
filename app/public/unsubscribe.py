"""One-click(ish) unsubscribe — the CAN-SPAM half of announcements.

GET shows a single-button page rather than unsubscribing on sight: mail
clients (Apple Mail Privacy Protection above all) prefetch every link in a
message, and a GET that mutates would silently unsubscribe people who never
tapped anything. One page, one button, POST records it — the law asks for "a
reply or a visit to a single page", and this is that page.

Idempotent and quiet: a second visit says "you're already off the list" and a
bad or foreign token is a plain 404, never an error that leaks whether an
address exists.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import announcements
from ..render import templates

log = logging.getLogger("mise.public.unsubscribe")
router = APIRouter()


@router.get("/u/{token}", response_class=HTMLResponse)
def confirm(request: Request, token: str):
    email = announcements.email_from_token(token)
    if not email:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "public/unsubscribe.html",
        {"email": email, "token": token, "done": announcements.is_unsubscribed(email)},
    )


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str):
    email = announcements.email_from_token(token)
    if not email:
        raise HTTPException(status_code=404)
    announcements.record_unsubscribe(email)
    log.info("unsubscribed %s", email)
    return templates.TemplateResponse(
        request, "public/unsubscribe.html", {"email": email, "token": token, "done": True}
    )
