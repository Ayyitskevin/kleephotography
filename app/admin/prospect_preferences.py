"""Explicit, audited sales outreach controls; never send or rewrite history."""

import json
import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import audit, db, outreach, security
from ..redirects import redirect_local
from ..render import templates
from . import prospect_followups as followups
from . import studio

router = APIRouter(prefix="/admin/studio/prospects", dependencies=[Depends(security.require_admin)])
TOKEN_AGE = 3600
MODES = {
    "active": "No outreach hold",
    "snoozed": "Pause through a date",
    "stopped": "Stop outbound outreach",
}


def _state(con, prospect_id):
    prospect = con.execute(
        "SELECT id,business FROM prospects WHERE id=?", (prospect_id,)
    ).fetchone()
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    state = outreach.for_prospect(con, prospect_id, studio._today().isoformat())
    own = next((s for s in state["sources"] if s["prospect_id"] == prospect_id), None)
    return dict(prospect), state, own


def _page(request, prospect_id, *, values=None, token="", error=None, status=200):
    with closing(db.connect()) as con:
        con.execute("BEGIN")
        prospect, state, own = _state(con, prospect_id)
    if values is None:
        values = {
            key: (own[key] or "") if own else value for key, value in outreach.DEFAULTS.items()
        }
        # Each change needs its own explanation; don't reuse a previous reason.
        values["reason"] = ""
    if not token and not error:
        token = security.sign(
            json.dumps(
                {
                    "kind": "outreach_preferences",
                    "prospect_id": prospect_id,
                    "version": own["version"] if own else 0,
                    "state": followups._digest(state),
                }
            )
        )
    return templates.TemplateResponse(
        request,
        "admin/prospect_preferences.html",
        {
            **followups._review_context(request),
            "prospect": prospect,
            "prospect_id": prospect_id,
            "outreach": state,
            "values": values,
            "token": token,
            "error": error,
            "modes": MODES,
            "preference_channels": outreach.CHANNELS,
            "today": studio._today().isoformat(),
        },
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _redirect(request, prospect_id):
    if followups._review_context(request):
        return followups._redirect(prospect_id, request)
    return redirect_local(f"/admin/studio/prospects/{prospect_id}/outreach")


@router.get("/{prospect_id}/outreach", response_class=HTMLResponse)
def preferences(request: Request, prospect_id: int):
    return _page(request, prospect_id)


@router.post("/{prospect_id}/outreach")
def save_preferences(
    request: Request,
    prospect_id: int,
    token: str = Form(""),
    mode: str = Form(""),
    snoozed_until: str = Form(""),
    email_blocked: str = Form(""),
    sms_blocked: str = Form(""),
    call_blocked: str = Form(""),
    preferred_channel: str = Form(""),
    reason: str = Form(""),
):
    values = dict(
        mode=mode,
        snoozed_until=snoozed_until,
        email_blocked=email_blocked,
        sms_blocked=sms_blocked,
        call_blocked=call_blocked,
        preferred_channel=preferred_channel,
        reason=reason.strip(),
    )
    try:
        raw = security.unsign(token, max_age=TOKEN_AGE)
        try:
            ticket = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            ticket = None
        if (
            not isinstance(ticket, dict)
            or ticket.get("kind") != "outreach_preferences"
            or ticket.get("prospect_id") != prospect_id
            or type(ticket.get("version")) is not int
        ):
            raise followups.ActivityError(
                "This form changed or expired. Reopen preferences and review again.", 409
            )
        if mode not in MODES or preferred_channel not in {"", *outreach.CHANNELS}:
            raise followups.ActivityError("Choose a listed outreach status and channel.")
        cleaned = dict(values)
        for channel in outreach.CHANNELS:
            field = channel + "_blocked"
            if values[field] not in {"", "1"}:
                raise followups.ActivityError("Choose the listed channel exclusions.")
            cleaned[field] = int(values[field] == "1")
        if preferred_channel and cleaned[preferred_channel + "_blocked"]:
            raise followups.ActivityError(
                "A preferred channel cannot also be excluded on this record."
            )
        cleaned["reason"] = followups._text(reason, "a reason for this change", 1000, required=True)
        if mode == "snoozed":
            followups._date(snoozed_until, "pause end date")
        else:
            cleaned["snoozed_until"] = None
        digest = followups._digest({"ticket": ticket, "values": cleaned})
        with db.tx() as con:
            _, state, own = _state(con, prospect_id)
            version = own["version"] if own else 0
            if own and version == ticket["version"] + 1 and own["last_command_digest"] == digest:
                return _redirect(request, prospect_id)
            if version != ticket["version"] or ticket.get("state") != followups._digest(state):
                raise followups.ActivityError(
                    "Preferences or linked client records changed. Your entries are preserved; reopen to review the current scope.",
                    409,
                )
            if mode == "snoozed" and snoozed_until < studio._today().isoformat():
                raise followups.ActivityError("Pause through today or a future date.")
            con.execute(
                "INSERT INTO prospect_outreach_preferences "
                "(prospect_id,mode,snoozed_until,email_blocked,sms_blocked,call_blocked,preferred_channel,reason,version,last_command_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(prospect_id) DO UPDATE SET "
                "mode=excluded.mode,snoozed_until=excluded.snoozed_until,email_blocked=excluded.email_blocked,"
                "sms_blocked=excluded.sms_blocked,call_blocked=excluded.call_blocked,preferred_channel=excluded.preferred_channel,"
                "reason=excluded.reason,version=excluded.version,last_command_digest=excluded.last_command_digest,updated_at=datetime('now')",
                (
                    prospect_id,
                    cleaned["mode"],
                    cleaned["snoozed_until"],
                    cleaned["email_blocked"],
                    cleaned["sms_blocked"],
                    cleaned["call_blocked"],
                    cleaned["preferred_channel"],
                    cleaned["reason"],
                    version + 1,
                    digest,
                ),
            )
            audit.log(
                con,
                "prospect",
                prospect_id,
                "outreach_preferences",
                diff={
                    "before": {k: own[k] for k in outreach.DEFAULTS} if own else None,
                    "after": cleaned,
                    "version": version + 1,
                    "scope_members": state["members"],
                },
            )
    except followups.ActivityError as exc:
        return _page(
            request, prospect_id, values=values, token=token, error=str(exc), status=exc.status
        )
    except sqlite3.IntegrityError:
        return _page(
            request,
            prospect_id,
            values=values,
            token=token,
            error="The record changed. Reopen preferences and review again.",
            status=409,
        )
    return _redirect(request, prospect_id)
