"""Editable, deterministic outreach starters. No provider calls or contact writes."""

import json
import sqlite3
import uuid
from contextlib import closing

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import audit, config, db, outreach, security, specialties
from ..redirects import redirect_local
from ..render import templates
from . import prospect_followups as followups

router = APIRouter(prefix="/admin/studio/prospects", dependencies=[Depends(security.require_admin)])
PURPOSES = {
    "introduction": "Introduction",
    "follow_up": "Follow-up to an attempt",
    "conversation": "After a conversation or reply",
}
NEXT_STEPS = {
    "introduction": "Would you be open to a quick conversation about what you need?",
    "follow_up": "Would it help if I sent a few relevant examples?",
    "conversation": "What would be the most useful next step for you?",
}
TOKEN_AGE = 3600
PAGE_SIZE = 20


def _inputs(
    channel: str = Form("email"),
    purpose: str = Form("introduction"),
    reference_event_id: str = Form(""),
    personalization: str = Form(""),
    next_step: str = Form(""),
    operation_key: str = Form(""),
):
    return dict(
        channel=channel,
        purpose=purpose,
        reference_event_id=reference_event_id,
        personalization=personalization.strip(),
        next_step=next_step.strip(),
        operation_key=operation_key,
    )


def _clean(values):
    v = dict(values)
    if v["channel"] not in {"email", "sms"} or v["purpose"] not in PURPOSES:
        raise followups.ActivityError("Choose a listed channel and message purpose.")
    followups._key(v["operation_key"])
    if v["purpose"] == "introduction":
        if v["reference_event_id"]:
            raise followups.ActivityError("Choose no prior contact for an introduction.")
    else:
        followups._positive(v["reference_event_id"], "reference contact")
    v["personalization"] = followups._text(v["personalization"], "a personal sentence", 500)
    v["next_step"] = followups._text(v["next_step"], "one next step", 250)
    return v


def _context(con, prospect_id, event_id=""):
    row = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Prospect not found")
    event = None
    if event_id:
        row_event = con.execute(
            "SELECT * FROM prospect_contact_events WHERE id=? AND prospect_id=?",
            (int(event_id), prospect_id),
        ).fetchone()
        if not row_event:
            raise followups.ActivityError(
                "Choose a contact record belonging to this prospect.", 409
            )
        event = dict(row_event)

    def one(sql):
        record = con.execute(sql, (prospect_id,)).fetchone()
        return dict(record) if record else None

    state = {
        "prospect": dict(row),
        "event": event,
        "latest_contact": one(
            "SELECT * FROM prospect_contact_events WHERE prospect_id=? ORDER BY id DESC LIMIT 1"
        ),
        "latest_response": one(
            "SELECT * FROM prospect_contact_events WHERE prospect_id=? "
            "AND outcome IN ('conversation','reply_received','not_interested') "
            "ORDER BY occurred_on DESC,id DESC LIMIT 1"
        ),
        "handoff": one(
            "SELECT h.project_id,h.client_id,h.photography_need,h.specialty,h.target_date,"
            "p.title,p.status FROM prospect_handoffs h JOIN projects p ON p.id=h.project_id "
            "WHERE h.prospect_id=?"
        ),
        "pending": one(
            "SELECT id,title,due_date,version FROM tasks "
            "WHERE prospect_id=? AND done=0 AND canceled_at IS NULL"
        ),
    }

    policy = outreach.for_prospect(con, prospect_id, followups.studio_mod._today().isoformat())
    # Unrestricted drafts created before preferences shipped remain usable.
    if policy["sources"]:
        state["outreach"] = policy
    return state


def _usable(state, values):
    if state.get("outreach"):
        reason = outreach.blocked_reason(state["outreach"], values["channel"])
        if reason:
            raise followups.ActivityError(reason, 409)
    if state["prospect"]["qualification"] == "not_fit":
        raise followups.ActivityError("Review this prospect's Not a fit qualification first.", 409)
    if state["latest_response"] and state["latest_response"]["outcome"] == "not_interested":
        raise followups.ActivityError(
            "The latest recorded response is Not interested. Review the contact history first.", 409
        )
    recipient = state["prospect"]["email" if values["channel"] == "email" else "phone"]
    if not recipient.strip():
        raise followups.ActivityError(
            "Add this channel's recipient to the prospect before drafting."
        )
    event = state["event"]
    if values["purpose"] == "follow_up" and (
        not event
        or event["direction"] != "out"
        or event["outcome"] not in {"attempted", "no_answer", "voicemail", "sent", "conversation"}
    ):
        raise followups.ActivityError("Select a recorded outgoing attempt for this follow-up.")
    if values["purpose"] == "conversation" and (
        not event or event["outcome"] not in {"conversation", "reply_received"}
    ):
        raise followups.ActivityError("Select a recorded conversation or received reply.")


def _starter(state, values):
    """Only explicit identity/specialty and operator-approved copy enter the text.

    Private notes, task titles, target dates and research never become claims.
    """
    p = state["prospect"]
    name = " ".join(p["contact_name"].split())
    business = " ".join(p["business"].split())
    studio = " ".join(config.SITE_NAME.split())
    specialty = (state["handoff"] or {}).get("specialty") or p["specialty"]
    craft = specialties.SPECIALTIES.get(specialty, {}).get("craft", "photography")
    if values["purpose"] == "introduction":
        opening = f"I'm reaching out from {studio} about {craft} for {business}."
    elif values["purpose"] == "follow_up":
        opening = f"I'm following up about {craft} for {business}."
    else:
        opening = (
            "Thanks for your reply."
            if state["event"]["outcome"] == "reply_received"
            else "Thanks for the conversation."
        )
    paragraphs = [f"Hi {name}," if name else "Hi,", opening]
    if values["personalization"]:
        paragraphs.append(values["personalization"])
    paragraphs.append(values["next_step"] or NEXT_STEPS[values["purpose"]])
    if values["channel"] == "sms":
        if values["purpose"] != "introduction":
            paragraphs.append(f"— {studio}")
        return {"subject": "", "body": " ".join(paragraphs)}
    paragraphs.append(studio)
    return {"subject": f"Photography for {business}", "body": "\n\n".join(paragraphs)}


def _message(subject, body, channel):
    subject = followups._text(subject, "a subject", 240, required=channel == "email")
    if "\n" in subject or "\r" in subject or (channel == "sms" and subject):
        raise followups.ActivityError(
            "Use a single-line email subject; text drafts have no subject."
        )
    body = followups._text(body, "a message", 1600 if channel == "sms" else 4000, required=True)
    return {"subject": subject, "body": body}


def _ticket(token, expected):
    raw = security.unsign(token, max_age=TOKEN_AGE)
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected.items()):
        raise followups.ActivityError(
            "This draft form changed or expired. Reopen it; your text is preserved below.", 409
        )
    return data


def _row(con, prospect_id, draft_id):
    row = con.execute(
        "SELECT * FROM prospect_message_drafts WHERE id=? AND prospect_id=?",
        (draft_id, prospect_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Draft not found for this prospect")
    return dict(row)


def _fresh(con, row):
    saved = json.loads(row["context_json"])
    state = _context(con, row["prospect_id"], saved["inputs"]["reference_event_id"])
    _usable(state, saved["inputs"])
    if followups._digest(state) != row["context_digest"]:
        raise followups.ActivityError(
            "The recipient or context changed. Keep this text for reference and start a fresh draft.",
            409,
        )


def _page(
    request,
    prospect_id,
    *,
    row=None,
    values=None,
    state=None,
    token="",
    error=None,
    status=200,
    page=1,
):
    with closing(db.connect()) as con:
        con.execute("BEGIN")
        current = _context(con, prospect_id)
        events = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM prospect_contact_events WHERE prospect_id=? ORDER BY occurred_on DESC,id DESC LIMIT 100",
                (prospect_id,),
            )
        ]
        total = con.execute(
            "SELECT COUNT(*) FROM prospect_message_drafts WHERE prospect_id=?", (prospect_id,)
        ).fetchone()[0]
        page = min(max(1, page), max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        drafts = [
            dict(r)
            for r in con.execute(
                "SELECT id,channel,purpose,subject,created_at,updated_at FROM prospect_message_drafts WHERE prospect_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (prospect_id, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            )
        ]
        stale = ""
        if row:
            state = json.loads(row["context_json"])["state"]
            try:
                _fresh(con, row)
            except followups.ActivityError as exc:
                stale = str(exc)
            if values is None:
                values = {"subject": row["subject"], "body": row["body"]}
            # Error responses retain the submitted token, never rebase edits onto
            # a newer version without review.
            if not token and not error:
                token = security.sign(
                    json.dumps(
                        {
                            "kind": "prospect_draft_edit",
                            "prospect_id": prospect_id,
                            "draft_id": row["id"],
                            "operation_key": row["operation_key"],
                            "version": row["version"],
                        }
                    )
                )
        if values is None:
            policy = current.get("outreach", {})
            available = [
                c
                for c, field in (("email", "email"), ("sms", "phone"))
                if current["prospect"][field] and c not in policy.get("blocked", [])
            ]
            preferred = policy.get("preferred_channel")
            values = {
                "channel": preferred if preferred in available else next(iter(available), "email"),
                "purpose": "introduction",
                "reference_event_id": "",
                "personalization": "",
                "next_step": "",
                "operation_key": str(uuid.uuid4()),
            }
    return templates.TemplateResponse(
        request,
        "admin/prospect_drafts.html",
        {
            "prospect_id": prospect_id,
            "current": current,
            "outreach": current.get("outreach"),
            "context": state,
            "values": values,
            "draft": row,
            "token": token,
            "error": error,
            "stale": stale,
            "events": events,
            "drafts": drafts,
            "page": page,
            "has_next": page * PAGE_SIZE < total,
            "purposes": PURPOSES,
            "channels": followups.CHANNELS,
            "outcomes": followups.OUTCOMES,
        },
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{prospect_id}/drafts", response_class=HTMLResponse)
def workspace(request: Request, prospect_id: int, page: int = 1):
    return _page(request, prospect_id, page=page)


@router.post("/{prospect_id}/drafts/preview", response_class=HTMLResponse)
def preview(request: Request, prospect_id: int, values: dict = Depends(_inputs)):
    try:
        values = _clean(values)
        with closing(db.connect()) as con:
            con.execute("BEGIN")
            state = _context(con, prospect_id, values["reference_event_id"])
            _usable(state, values)
        text = _message(**_starter(state, values), channel=values["channel"])
        token = security.sign(
            json.dumps(
                {
                    "kind": "prospect_draft_create",
                    "prospect_id": prospect_id,
                    "inputs": values,
                    "context_digest": followups._digest(state),
                    "state": state,
                }
            )
        )
        return _page(request, prospect_id, values=values | text, state=state, token=token)
    except followups.ActivityError as exc:
        return _page(request, prospect_id, values=values, error=str(exc), status=exc.status)


@router.post("/{prospect_id}/drafts")
def create(
    request: Request,
    prospect_id: int,
    token: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    values = {"subject": subject, "body": body}
    state = None
    try:
        receipt = _ticket(token, {"kind": "prospect_draft_create", "prospect_id": prospect_id})
        inputs = _clean(receipt["inputs"])
        state = receipt["state"]
        values = inputs | values
        values.update(_message(subject, body, inputs["channel"]))
        digest = followups._digest(values | {"context_digest": receipt["context_digest"]})
        with db.tx() as con:
            previous = con.execute(
                "SELECT * FROM prospect_message_drafts WHERE operation_key=?",
                (inputs["operation_key"],),
            ).fetchone()
            if previous:
                if previous["prospect_id"] != prospect_id or previous["request_digest"] != digest:
                    raise followups.ActivityError(
                        "That draft was already saved with different text. Open the saved draft or start another.",
                        409,
                    )
                return redirect_local(
                    f"/admin/studio/prospects/{prospect_id}/drafts/{previous['id']}"
                )
            current = _context(con, prospect_id, inputs["reference_event_id"])
            _usable(current, inputs)
            if followups._digest(current) != receipt["context_digest"]:
                raise followups.ActivityError(
                    "The recipient or context changed after preview. Start a fresh draft; your text is preserved below.",
                    409,
                )
            draft_id = con.execute(
                "INSERT INTO prospect_message_drafts (prospect_id,operation_key,request_digest,channel,purpose,subject,body,context_json,context_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    prospect_id,
                    inputs["operation_key"],
                    digest,
                    inputs["channel"],
                    inputs["purpose"],
                    values["subject"],
                    values["body"],
                    json.dumps({"state": state, "inputs": inputs}, sort_keys=True),
                    receipt["context_digest"],
                ),
            ).lastrowid
            audit.log(
                con,
                "prospect_draft",
                draft_id,
                "create",
                diff={
                    "prospect_id": prospect_id,
                    "channel": inputs["channel"],
                    "purpose": inputs["purpose"],
                },
            )
        return redirect_local(f"/admin/studio/prospects/{prospect_id}/drafts/{draft_id}")
    except (followups.ActivityError, sqlite3.IntegrityError) as exc:
        return _page(
            request,
            prospect_id,
            values=values,
            state=state,
            token=token,
            error=str(exc)
            if isinstance(exc, followups.ActivityError)
            else "A draft record changed. Reopen it before saving.",
            status=exc.status if isinstance(exc, followups.ActivityError) else 409,
        )


@router.get("/{prospect_id}/drafts/{draft_id}", response_class=HTMLResponse)
def detail(request: Request, prospect_id: int, draft_id: int):
    with closing(db.connect()) as con:
        row = _row(con, prospect_id, draft_id)
    return _page(request, prospect_id, row=row)


def _edit_ticket(token, row):
    data = _ticket(
        token,
        {
            "kind": "prospect_draft_edit",
            "prospect_id": row["prospect_id"],
            "draft_id": row["id"],
            "operation_key": row["operation_key"],
        },
    )
    if type(data.get("version")) is not int:
        raise followups.ActivityError("Reopen this draft before editing.", 409)
    return data["version"]


@router.post("/{prospect_id}/drafts/{draft_id}")
def edit(
    request: Request,
    prospect_id: int,
    draft_id: int,
    token: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    values = {"subject": subject, "body": body}
    row = None
    try:
        with db.tx() as con:
            row = _row(con, prospect_id, draft_id)
            version = _edit_ticket(token, row)
            values = _message(subject, body, row["channel"])
            same = all(row[k] == v for k, v in values.items())
            if row["version"] == version + 1 and same:
                return redirect_local(f"/admin/studio/prospects/{prospect_id}/drafts/{draft_id}")
            if row["version"] != version:
                raise followups.ActivityError(
                    "Another tab saved this draft. Your edits are preserved; open the latest version before retrying.",
                    409,
                )
            _fresh(con, row)
            if not same:
                con.execute(
                    "UPDATE prospect_message_drafts SET subject=?,body=?,version=version+1,updated_at=datetime('now') WHERE id=?",
                    (values["subject"], values["body"], draft_id),
                )
                audit.log(
                    con,
                    "prospect_draft",
                    draft_id,
                    "edit",
                    diff={"prospect_id": prospect_id, "version": [version, version + 1]},
                )
        return redirect_local(f"/admin/studio/prospects/{prospect_id}/drafts/{draft_id}")
    except followups.ActivityError as exc:
        return _page(
            request,
            prospect_id,
            row=row,
            values=values,
            token=token,
            error=str(exc),
            status=exc.status,
        )


@router.post("/{prospect_id}/drafts/{draft_id}/copy")
def copy_text(
    prospect_id: int,
    draft_id: int,
    token: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    # Read-only freshness check for the exact editor. Clipboard work is client-side.
    try:
        with closing(db.connect()) as con:
            con.execute("BEGIN")
            row = _row(con, prospect_id, draft_id)
            if _edit_ticket(token, row) != row["version"]:
                raise followups.ActivityError(
                    "Another tab saved this draft. Reopen the latest version before copying.", 409
                )
            text = _message(subject, body, row["channel"])
            _fresh(con, row)
        return JSONResponse(
            {**text, "draft_id": draft_id, "prospect_id": prospect_id},
            headers={"Cache-Control": "no-store"},
        )
    except followups.ActivityError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"}
        )
