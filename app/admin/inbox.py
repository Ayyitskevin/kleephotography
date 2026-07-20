"""Inbox — inbound inquiries as a conversation-style triage view.

Honest adaptation of the Admin Inbox prototype's two-way messenger over Mise's
REAL data. Email send is manual-only (Gmail SMTP, human-in-the-loop); SMS rides
the Quo adapter and is INERT until keys are set (sms.configured()). The 3-pane
layout: thread list · the conversation thread (inbound + outbound bubbles) + a
composer that sends by email OR text · contact details + the real convert actions
(quote / client / dismiss) that already live in studio.py. The thread is the
`messages` table; legacy web-form inquiries with no messages rows synthesize one
inbound bubble from the inquiry's first message. No fabricated data.
"""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config, db, mailer, security, sms
from ..render import templates

log = logging.getLogger("mise.admin.inbox")
router = APIRouter(prefix="/admin/inbox", dependencies=[Depends(security.require_admin)])

_TABS = ["all", "bookings", "archived"]

# tab → (WHERE, ORDER BY). Fixed SQL fragments keyed by an allowlisted tab; the
# dict lookup IS the gate — an unknown tab can't reach the query (falls to "all").
_INBOX_FILTERS = {
    "all": ("converted_at IS NULL AND dismissed_at IS NULL", "ORDER BY created_at DESC"),
    "bookings": (
        "converted_at IS NULL AND dismissed_at IS NULL AND kind='booking'",
        "ORDER BY created_at DESC",
    ),
    "archived": (
        "converted_at IS NOT NULL OR dismissed_at IS NOT NULL",
        "ORDER BY COALESCE(dismissed_at, converted_at) DESC",
    ),
}

# Deterministic avatar tints by inquiry id — same forest/clay/teal family the
# prototype hand-picked, cycled so each thread reads as a distinct contact.
_AVATARS = [
    ("#7C2F38", "#F3F0E2"),
    ("#2f6d8a", "#FFFFFF"),
    ("#2f7d57", "#FFFFFF"),
    ("#9a7a2c", "#FFFFFF"),
    ("#143C2F", "#F3F0E2"),
]


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "#"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _channel(inq) -> dict:
    """How the lead arrived — booking form, text, or general inquiry.

    Dark-panel tints (editorial-dark, Revamp PR-E) — these feed the .ib-chan
    inline style directly, so they can't be reached by CSS; match the same
    honey/ok/clay/neutral status tokens the rest of the admin shell uses.
    """
    if inq["kind"] == "booking":
        return {"ch_label": "Booking", "ch_color": "#d8a857", "ch_bg": "#2b2413"}
    if inq["kind"] == "sms":
        return {"ch_label": "Text", "ch_color": "#9cc178", "ch_bg": "#20271a"}
    if inq["kind"] == "call":
        return {"ch_label": "Call", "ch_color": "#d98a78", "ch_bg": "#2e1a18"}
    return {"ch_label": "Inquiry", "ch_color": "#aba9a3", "ch_bg": "#242424"}


def _stage(inq) -> dict:
    if inq["converted_at"]:
        return {"stage": "Converted", "stage_color": "#9cc178", "stage_bg": "#20271a"}
    if inq["dismissed_at"]:
        return {"stage": "Dismissed", "stage_color": "#aba9a3", "stage_bg": "#242424"}
    if inq["kind"] == "booking":
        return {"stage": "Booking", "stage_color": "#d8a857", "stage_bg": "#2b2413"}
    return {"stage": "Lead", "stage_color": "#d98a78", "stage_bg": "#2e1a18"}


def _thread_row(inq, active_id):
    av = _AVATARS[inq["id"] % len(_AVATARS)]
    msg = (inq["message"] or "").strip().replace("\n", " ")
    service = (inq["service"] or "").strip()
    return {
        "id": inq["id"],
        "name": inq["business"] or inq["name"] or "Unknown",
        "initials": _initials(inq["business"] or inq["name"]),
        "av_bg": av[0],
        "av_color": av[1],
        "time": inq["created_at"],
        "preview": msg or "(no message)",
        "service": service or None,
        "active": inq["id"] == active_id,
        "unread": not inq["emailed"] and not inq["converted_at"] and not inq["dismissed_at"],
        **_channel(inq),
    }


def _reply_subject(inq) -> str:
    kind = "booking request" if inq["kind"] == "booking" else "inquiry"
    return f"Re: your {kind} — {config.SITE_NAME}"


def _detail_rows(inq) -> list[dict]:
    rows = []
    if inq["email"]:
        rows.append({"k": "Email", "v": inq["email"]})
    if inq["phone"]:
        rows.append({"k": "Phone", "v": inq["phone"]})
    if inq["business"]:
        rows.append({"k": "Business", "v": inq["business"]})
    rows.append(
        {
            "k": "Source",
            "v": {"booking": "Booking form", "sms": "Text message", "call": "Phone call"}.get(
                inq["kind"], "Inquiry form"
            ),
        }
    )
    if inq["service"]:
        rows.append({"k": "Specialty", "v": inq["service"]})
    if inq["shoot_date"]:
        rows.append({"k": "Shoot date", "v": inq["shoot_date"]})
    return rows


def _notion_job_for(inquiry_id: int) -> dict | None:
    """Latest notion_sync_inquiry job for this lead, if any (failed first)."""
    rows = db.all_(
        """SELECT id, status, error, payload, updated_at FROM jobs
           WHERE kind='notion_sync_inquiry'
           ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'queued' THEN 1
                    WHEN 'running' THEN 2 ELSE 3 END, id DESC
           LIMIT 80"""
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("inquiry_id") == inquiry_id:
            return dict(row)
    return None


def _integration_health(inq) -> dict:
    """Owner-facing integration signals — email notify, Notion mirror, recovery.

    Computed from real columns/jobs only. Does not invent green checks: dormant
    Notion config and failed mirror jobs surface as explicit guidance with a
    link to the existing Jobs recovery page.
    """
    notion_job = _notion_job_for(inq["id"])
    page_id = None
    try:
        page_id = inq["notion_page_id"]
    except (KeyError, IndexError):
        page_id = None

    if inq["emailed"]:
        email = {
            "label": "Owner email",
            "state": "ok",
            "detail": "Notification sent (or you replied from Inbox).",
        }
    elif not mailer.configured():
        email = {
            "label": "Owner email",
            "state": "warn",
            "detail": "Mailer not configured — lead is stored; reply manually.",
        }
    else:
        email = {
            "label": "Owner email",
            "state": "warn",
            "detail": "Not marked emailed — check SMTP logs or reply here.",
        }

    armed = bool(config.NOTION_TOKEN and config.NOTION_LEADS_DB)
    if page_id:
        notion = {
            "label": "Notion lead",
            "state": "ok",
            "detail": f"Mirrored (page {page_id[:12]}…)"
            if len(page_id) > 12
            else f"Mirrored ({page_id})",
            "jobs_href": None,
            "retry_job_id": None,
        }
    elif notion_job and notion_job["status"] == "failed":
        err = (notion_job.get("error") or "unknown error")[:120]
        notion = {
            "label": "Notion lead",
            "state": "bad",
            "detail": f"Mirror failed — {err}",
            "jobs_href": "/admin/jobs",
            "retry_job_id": notion_job["id"],
        }
    elif notion_job and notion_job["status"] in ("queued", "running"):
        notion = {
            "label": "Notion lead",
            "state": "warn",
            "detail": f"Mirror {notion_job['status']} — will retry automatically.",
            "jobs_href": "/admin/jobs",
            "retry_job_id": None,
        }
    elif not armed:
        notion = {
            "label": "Notion lead",
            "state": "muted",
            "detail": "Not armed (MISE_NOTION_LEADS_DB unset) — lead lives in Mise only.",
            "jobs_href": None,
            "retry_job_id": None,
        }
    else:
        notion = {
            "label": "Notion lead",
            "state": "warn",
            "detail": "Not mirrored yet — check Jobs if this stays empty.",
            "jobs_href": "/admin/jobs",
            "retry_job_id": None,
        }

    if inq["converted_at"]:
        next_action = "Open the converted client/project/proposal, or undo conversion."
    elif inq["dismissed_at"]:
        next_action = "Restore to pipeline if this lead is still live."
    elif notion["state"] == "bad":
        next_action = "Reply to the lead, then retry the failed Notion job on Jobs."
    elif not inq["emailed"] and inq["email"]:
        next_action = "Reply from this thread (or your mail client), then convert or dismiss."
    else:
        next_action = "Convert to quote/client, or dismiss if not a fit."

    return {
        "email": email,
        "notion": notion,
        "next_action": next_action,
        "specialty": (inq["service"] or "").strip() or None,
        "source": {"booking": "Booking form", "sms": "Text message", "call": "Phone call"}.get(
            inq["kind"], "Inquiry form"
        ),
    }


def _thread(inq) -> list[dict]:
    """Chronological conversation bubbles. The `messages` table is the source of
    truth; an inquiry with no rows yet (every legacy web-form lead) synthesizes a
    single inbound bubble from its first message so it still reads as a thread."""
    rows = db.all_(
        """SELECT direction, channel, body, created_at FROM messages
                      WHERE inquiry_id=? ORDER BY created_at, id""",
        (inq["id"],),
    )
    if rows:
        return [
            {
                "mine": r["direction"] == "out",
                "channel": r["channel"],
                "body": r["body"],
                "time": r["created_at"],
            }
            for r in rows
        ]
    return [
        {
            "mine": False,
            "channel": inq["kind"] if inq["kind"] in ("sms", "call") else "email",
            "body": inq["message"],
            "time": inq["created_at"],
        }
    ]


def _active_ctx(inq) -> dict:
    av = _AVATARS[inq["id"] % len(_AVATARS)]
    phone_first = inq["kind"] in ("sms", "call")
    # The quote flow converts a lead straight into a draft proposal — deep-link
    # the "Open converted record" button to it so Kevin lands on the quote he just
    # made, not the parent project. Falls back to project/client for the other
    # convert paths, which spawn no proposal.
    proposal_id = None
    if inq["converted_project_id"]:
        row = db.one(
            "SELECT id FROM proposals WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (inq["converted_project_id"],),
        )
        proposal_id = row["id"] if row else None
    return {
        "id": inq["id"],
        "name": inq["business"] or inq["name"] or "Unknown",
        "contact_name": inq["name"],
        "initials": _initials(inq["business"] or inq["name"]),
        "av_bg": av[0],
        "av_color": av[1],
        "email": inq["email"],
        "phone": inq["phone"],
        "message": inq["message"],
        "created_at": inq["created_at"],
        "messages": _thread(inq),
        "converted_proposal_id": proposal_id,
        "converted_project_id": inq["converted_project_id"],
        "converted_client_id": inq["converted_client_id"],
        "is_converted": bool(inq["converted_at"]),
        "is_dismissed": bool(inq["dismissed_at"]),
        "is_replied": bool(inq["emailed"]),
        "reply_subject": _reply_subject(inq),
        # Composer defaults to whichever channel the lead arrived on, and only
        # offers a channel the contact can actually receive on.
        "default_channel": "sms"
        if (inq["phone"] and (phone_first or not inq["email"]))
        else "email",
        "can_email": bool(inq["email"]),
        "can_sms": bool(inq["phone"]),
        "sub": (inq["email"] or inq["phone"] or "")
        + (" · booking request" if inq["kind"] == "booking" else ""),
        "details": _detail_rows(inq),
        "health": _integration_health(inq),
        **_channel(inq),
        **_stage(inq),
    }


@router.get("", response_class=HTMLResponse)
async def inbox(request: Request, tab: str = "all", sel: int | None = None):
    if tab not in _TABS:
        tab = "all"
    where, order = _INBOX_FILTERS[tab]
    rows = db.all_(f"SELECT * FROM inquiries WHERE {where} {order} LIMIT 100")

    counts = {
        "all": db.one(
            "SELECT COUNT(*) n FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL"
        )["n"],
        "bookings": db.one(
            "SELECT COUNT(*) n FROM inquiries WHERE converted_at IS NULL "
            "AND dismissed_at IS NULL AND kind='booking'"
        )["n"],
        "archived": db.one(
            "SELECT COUNT(*) n FROM inquiries "
            "WHERE converted_at IS NOT NULL OR dismissed_at IS NOT NULL"
        )["n"],
    }

    active = None
    if rows:
        chosen = next((r for r in rows if r["id"] == sel), None)
        if chosen is None and sel is not None:
            chosen = db.one(f"SELECT * FROM inquiries WHERE id=? AND ({where})", (sel,))
            if chosen is not None:
                rows = [chosen, *rows[:99]]
        if chosen is None:
            chosen = rows[0]
        active = _active_ctx(chosen)

    return templates.TemplateResponse(
        request,
        "admin/inbox.html",
        {
            "tab": tab,
            "counts": counts,
            "threads": [_thread_row(r, active["id"] if active else None) for r in rows],
            "active": active,
            "mail_configured": mailer.configured(),
            "sms_configured": sms.configured(),
        },
    )


@router.post("/{inquiry_id}/reply")
async def reply(
    inquiry_id: int,
    tab: str = Form("all"),
    channel: str = Form("email"),
    subject: str = Form(""),
    message: str = Form(...),
):
    """Reply to an inquiry from inside Mise — manual send, logged as an outbound
    bubble. Email goes via Gmail SMTP (subject required, recorded in emails_log);
    text goes via the Quo adapter (subject ignored). Nothing auto-sends — Kevin
    clicks Send. The outbound row lands in `messages` so it shows in the thread."""
    inq = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    if channel not in ("email", "sms"):
        raise HTTPException(status_code=400, detail="unknown channel")

    if channel == "sms":
        if not inq["phone"]:
            raise HTTPException(status_code=400, detail="no phone on file for this inquiry")
        if not sms.configured():
            raise HTTPException(status_code=503, detail="SMS is not configured")
        try:
            msg_id = sms.send(inq["phone"], message)
        except sms.SmsError as e:
            log.warning("inbox sms reply failed for inquiry %s: %s", inquiry_id, e)
            raise HTTPException(status_code=502, detail=f"SMS send failed: {e}")
        db.run(
            """INSERT INTO messages (inquiry_id, direction, channel, body, provider_msg_id)
                  VALUES (?, 'out', 'sms', ?, ?)""",
            (inquiry_id, message, msg_id or None),
        )
        log.info("inbox sms reply sent for inquiry %s", inquiry_id)
    else:
        if not inq["email"]:
            raise HTTPException(status_code=400, detail="no email on file for this inquiry")
        if not mailer.configured():
            raise HTTPException(status_code=503, detail="email is not configured")
        subject = subject.strip()
        if not subject:
            raise HTTPException(status_code=400, detail="subject required")
        try:
            mailer.send(inq["email"], subject, message)
        except Exception:
            log.exception("inbox reply send failed for inquiry %s", inquiry_id)
            raise HTTPException(status_code=502, detail="SMTP send failed — check logs")
        db.run(
            """INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject)
                  VALUES (?, 'other', ?, ?, ?)""",
            (inq["converted_project_id"], inquiry_id, inq["email"], subject),
        )
        db.run(
            """INSERT INTO messages (inquiry_id, direction, channel, body)
                  VALUES (?, 'out', 'email', ?)""",
            (inquiry_id, message),
        )
        db.run("UPDATE inquiries SET emailed=1 WHERE id=?", (inquiry_id,))
        log.info("inbox reply sent for inquiry %s", inquiry_id)

    if tab not in _TABS:
        tab = "all"
    return RedirectResponse(f"/admin/inbox?tab={tab}&sel={inquiry_id}", status_code=303)
