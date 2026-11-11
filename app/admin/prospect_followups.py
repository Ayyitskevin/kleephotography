"""Dated prospect work and manual contact history. No provider or Inbox effects."""

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import audit, db, outreach, security
from ..redirects import redirect_local
from ..render import templates
from . import studio as studio_mod

router = APIRouter(prefix="/admin/studio/prospects", dependencies=[Depends(security.require_admin)])
PAGE_SIZE = 50
CHANNELS = {
    "email": "Email",
    "sms": "Text message",
    "call": "Phone call",
    "in_person": "In person",
    "other": "Other",
}
DIRECTIONS = {"out": "Outbound", "in": "Inbound"}
OUTCOMES = {
    "attempted": "Attempted contact",
    "no_answer": "No answer",
    "voicemail": "Left voicemail",
    "sent": "Manually sent",
    "conversation": "Had a conversation",
    "reply_received": "Received a reply",
    "not_interested": "Not interested",
    "other": "Other",
}
REPLY_OUTCOMES = {"reply_received", "conversation", "not_interested"}


class ActivityError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _options() -> dict:
    today = studio_mod._today()
    return {
        "today": today.isoformat(),
        "tomorrow": (today + dt.timedelta(days=1)).isoformat(),
        "next_week": (today + dt.timedelta(days=7)).isoformat(),
        "channels": CHANNELS,
        "directions": DIRECTIONS,
        "outcomes": OUTCOMES,
    }


def detail_context(prospect_id: int) -> dict:
    tasks = [
        dict(row)
        for row in db.all_(
            "SELECT * FROM tasks WHERE prospect_id=? ORDER BY id DESC", (prospect_id,)
        )
    ]
    by_id = {task["id"]: task for task in tasks}
    for task in tasks:
        task["changes"] = []
    for row in db.all_(
        "SELECT a.action, a.created_at, a.diff_json, a.entity_id FROM audit_log a "
        "JOIN tasks t ON t.id=a.entity_id "
        "WHERE a.entity_type='prospect_task' AND t.prospect_id=? ORDER BY a.id DESC",
        (prospect_id,),
    ):
        # Another tab may create a task between these read-only projections.
        if row["entity_id"] not in by_id:
            continue
        by_id[row["entity_id"]]["changes"].append(
            {
                "action": row["action"],
                "created_at": row["created_at"],
                "diff": json.loads(row["diff_json"]) if row["diff_json"] else {},
            }
        )
    contacts = [
        dict(row)
        for row in db.all_(
            "SELECT * FROM prospect_contact_events WHERE prospect_id=? "
            "ORDER BY occurred_on DESC, id DESC",
            (prospect_id,),
        )
    ]
    prospect = db.one("SELECT * FROM prospects WHERE id=?", (prospect_id,))
    return {
        **_options(),
        "outreach": outreach.read_index(studio_mod._today().isoformat())["prospects"].get(
            prospect_id
        ),
        "prospect": dict(prospect) if prospect else None,
        "handoff": db.one(
            "SELECT h.*, p.title AS project_title FROM prospect_handoffs h "
            "JOIN projects p ON p.id=h.project_id WHERE h.prospect_id=?",
            (prospect_id,),
        ),
        "pending_task": next((t for t in tasks if not t["done"] and not t["canceled_at"]), None),
        "followup_history": tasks,
        "contact_events": contacts,
        "last_attempt": next((c for c in contacts if c["direction"] == "out"), None),
        "last_reply": next(
            (c for c in contacts if c["direction"] == "in" and c["outcome"] in REPLY_OUTCOMES), None
        ),
        "schedule_operation_key": str(uuid.uuid4()),
        "contact_operation_key": str(uuid.uuid4()),
    }


def _error(request, prospect_id, values, kind, error):
    # Submitted identity/version/key stay untouched; current state is separate.
    return templates.TemplateResponse(
        request,
        "admin/prospect_activity_error.html",
        {
            **detail_context(prospect_id),
            "prospect_id": prospect_id,
            "values": values,
            "form_kind": kind,
            "form_action": request.url.path,
            "error": str(error),
            "conflict": error.status == 409,
        },
        status_code=error.status,
    )


def _digest(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _text(value: str, label: str, limit: int, *, required: bool = False) -> str:
    value = value.strip()
    if "\x00" in value or len(value) > limit or (required and not value):
        raise ActivityError(f"Enter {label} between {1 if required else 0} and {limit} characters.")
    return value


def _date(value: str, label: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        raise ActivityError(f"Choose a real {label} in YYYY-MM-DD format.") from None
    return value


def _positive(value: str, label: str) -> int:
    if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < 1:
        raise ActivityError(f"The {label} is invalid. Reopen this prospect and review the form.")
    return int(value)


def _key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", value):
        raise ActivityError(
            "The operation key is invalid. Reopen this prospect to start a new form."
        )
    return value


def _prospect(con, prospect_id):
    if not con.execute("SELECT 1 FROM prospects WHERE id=?", (prospect_id,)).fetchone():
        raise ActivityError("Prospect not found.", 404)


def _redirect(prospect_id):
    """Back to this prospect. Only a numeric id may be part of the path.

    Anything else, including a scheme or a backslash host, stays on the list.
    ``redirect_local`` still refuses a target that is not a same-site path.
    """
    default = "/admin/studio/prospects"
    if isinstance(prospect_id, bool) or not isinstance(prospect_id, int):
        return redirect_local(default)
    try:
        return redirect_local(f"{default}/{prospect_id}")
    except HTTPException:
        return redirect_local(default)


@router.get("/follow-ups", response_class=HTMLResponse)
def followup_queue(request: Request, filter: str = "due", q: str = "", page: int = 1):
    if filter not in {"due", "upcoming", "unplanned", "held", "all"}:
        filter = "due"
    q = q.strip()[:200]
    today = studio_mod._today().isoformat()
    predicates = {
        "due": "t.id IS NOT NULL AND t.due_date <= ?",
        "upcoming": "t.id IS NOT NULL AND t.due_date > ?",
        "unplanned": "t.id IS NULL AND p.qualification != 'not_fit' AND h.prospect_id IS NULL",
        "all": "1=1",
        "held": "1=1",
    }
    # Every interpolated SQL fragment is selected here, never supplied by a form.
    source = (
        "FROM prospects p LEFT JOIN tasks t ON t.prospect_id=p.id "
        "AND t.done=0 AND t.canceled_at IS NULL "
        "LEFT JOIN prospect_handoffs h ON h.prospect_id=p.id "
    )
    where = (
        "WHERE instr(lower(p.business || ' ' || p.contact_name || ' ' || p.email || ' ' "
        "|| p.phone), lower(?)) > 0 AND " + predicates[filter]
    )
    args = (q, today) if filter in {"due", "upcoming"} else (q,)
    query = (
        "SELECT p.*, t.id AS task_id, t.title, t.due_date, t.version, h.project_id AS linked_project_id, "
        "(SELECT x.cancel_reason FROM tasks x WHERE x.prospect_id=p.id "
        "ORDER BY x.id DESC LIMIT 1) AS last_cancel_reason, "
        "(SELECT MAX(c.occurred_on) FROM prospect_contact_events c "
        "WHERE c.prospect_id=p.id AND c.direction='out') AS last_attempt_on, "
        "(SELECT MAX(c.occurred_on) FROM prospect_contact_events c "
        "WHERE c.prospect_id=p.id AND c.direction='in' "
        "AND c.outcome IN ('reply_received','conversation','not_interested')) AS last_reply_on "
        + source
        + where
        + " ORDER BY (t.due_date IS NULL), t.due_date ASC, p.id ASC"
    )
    with closing(db.connect()) as con:
        con.execute("BEGIN")
        policies = outreach.index(con, today)["prospects"]
        rows = [{**dict(r), "outreach": policies[r["id"]]} for r in con.execute(query, args)]
        if filter != "all":
            rows = [r for r in rows if r["outreach"]["held"] == (filter == "held")]
    # Filter before pagination so held rows never crowd active work off a page.
    total = len(rows)
    page = min(max(1, page), max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    rows = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    return templates.TemplateResponse(
        request,
        "admin/prospect_followups.html",
        {
            **_options(),
            "prospects": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "has_next": page * PAGE_SIZE < total,
            "filter": filter,
            "q": q,
        },
    )


@router.post("/{prospect_id}/follow-ups")
def schedule_followup(
    request: Request,
    prospect_id: int,
    title: str = Form(""),
    due_date: str = Form(""),
    operation_key: str = Form(""),
):
    values = {"title": title.strip(), "due_date": due_date.strip(), "operation_key": operation_key}
    try:
        title = _text(values["title"], "an action title", 200, required=True)
        due_date = _date(values["due_date"], "due date")
        operation_key = _key(operation_key)
        digest = _digest({"prospect_id": prospect_id, "title": title, "due_date": due_date})
        with db.tx() as con:
            _prospect(con, prospect_id)
            previous = con.execute(
                "SELECT * FROM tasks WHERE schedule_key=?", (operation_key,)
            ).fetchone()
            if previous:
                if previous["prospect_id"] != prospect_id or previous["schedule_digest"] != digest:
                    raise ActivityError(
                        "This operation key already belongs to another saved action. Review the current record.",
                        409,
                    )
                return _redirect(prospect_id)
            policy = outreach.for_prospect(con, prospect_id, studio_mod._today().isoformat())
            if reason := outreach.blocked_reason(policy):
                raise ActivityError(reason, 409)
            if con.execute(
                "SELECT 1 FROM tasks WHERE prospect_id=? AND done=0 AND canceled_at IS NULL",
                (prospect_id,),
            ).fetchone():
                raise ActivityError(
                    "This prospect already has a next action. Review or reschedule it first.", 409
                )
            task_id = con.execute(
                "INSERT INTO tasks (title,due_date,prospect_id,schedule_key,schedule_digest) VALUES (?,?,?,?,?)",
                (title, due_date, prospect_id, operation_key, digest),
            ).lastrowid
            audit.log(
                con,
                "prospect_task",
                task_id,
                "schedule",
                diff={"prospect_id": prospect_id, "title": title, "due_date": due_date},
            )
    except ActivityError as error:
        return _error(request, prospect_id, values, "schedule", error)
    except sqlite3.IntegrityError:
        return _error(
            request,
            prospect_id,
            values,
            "schedule",
            ActivityError(
                "This action could not be saved because the record changed. Review the current next action.",
                409,
            ),
        )
    return _redirect(prospect_id)


def _change_task(
    con, prospect_id, task_id, version, action, note, due_date="", *, allow_retry=True
):
    task = con.execute(
        "SELECT * FROM tasks WHERE id=? AND prospect_id=?", (task_id, prospect_id)
    ).fetchone()
    if task is None:
        raise ActivityError("This follow-up does not belong to this prospect.", 404)
    digest = _digest(
        {
            "prospect_id": prospect_id,
            "task_id": task_id,
            "version": version,
            "action": action,
            "note": note,
            "due_date": due_date,
        }
    )
    if allow_retry and task["version"] == version + 1 and task["last_command_digest"] == digest:
        return  # Exact retry of the most recent successful command; no audit/write.
    if task["version"] != version or task["done"] or task["canceled_at"]:
        raise ActivityError(
            "This action changed since the form opened. Your entries are preserved; review the current action before trying again.",
            409,
        )
    if action == "complete":
        con.execute(
            "UPDATE tasks SET done=1, done_at=datetime('now'), completion_note=?, version=version+1, last_command_digest=? WHERE id=?",
            (note, digest, task_id),
        )
    elif action == "cancel":
        con.execute(
            "UPDATE tasks SET canceled_at=datetime('now'), cancel_reason=?, version=version+1, last_command_digest=? WHERE id=?",
            (note, digest, task_id),
        )
    else:
        con.execute(
            "UPDATE tasks SET due_date=?, version=version+1, last_command_digest=? WHERE id=?",
            (due_date, digest, task_id),
        )
    audit.log(
        con,
        "prospect_task",
        task_id,
        action,
        diff={
            "prospect_id": prospect_id,
            "version": [version, version + 1],
            "note": note,
            "due_date": [task["due_date"], due_date]
            if action == "reschedule"
            else task["due_date"],
        },
    )


def _command_values(action, version, note, due_date):
    if action not in {"complete", "reschedule", "cancel"}:
        raise ActivityError("Choose complete, reschedule, or cancel.")
    parsed_version = _positive(version, "action version")
    note = _text(note, "a completion note or change reason", 1000, required=True)
    due_date = _date(due_date, "due date") if action == "reschedule" else ""
    return parsed_version, note, due_date


@router.post("/{prospect_id}/follow-ups/{task_id}")
def change_followup(
    request: Request,
    prospect_id: int,
    task_id: int,
    action: str = Form(""),
    version: str = Form(""),
    note: str = Form(""),
    due_date: str = Form(""),
):
    values = {
        "action": action,
        "version": version,
        "note": note.strip(),
        "due_date": due_date.strip(),
        "task_id": str(task_id),
    }
    try:
        parsed_version, clean_note, clean_date = _command_values(
            action, version, values["note"], values["due_date"]
        )
        with db.tx() as con:
            _prospect(con, prospect_id)
            _change_task(con, prospect_id, task_id, parsed_version, action, clean_note, clean_date)
    except ActivityError as error:
        return _error(request, prospect_id, values, "command", error)
    return _redirect(prospect_id)


@router.post("/{prospect_id}/contacts")
def record_contact(
    request: Request,
    prospect_id: int,
    operation_key: str = Form(""),
    channel: str = Form(""),
    direction: str = Form(""),
    outcome: str = Form(""),
    occurred_on: str = Form(""),
    notes: str = Form(""),
    task_action: str = Form("keep"),
    task_id: str = Form(""),
    version: str = Form(""),
    task_note: str = Form(""),
    due_date: str = Form(""),
):
    values = {
        "operation_key": operation_key,
        "channel": channel,
        "direction": direction,
        "outcome": outcome,
        "occurred_on": occurred_on.strip(),
        "notes": notes.strip(),
        "task_action": task_action,
        "task_id": task_id,
        "version": version,
        "task_note": task_note.strip(),
        "due_date": due_date.strip(),
    }
    try:
        _key(operation_key)
        if channel not in CHANNELS or direction not in DIRECTIONS or outcome not in OUTCOMES:
            raise ActivityError("Choose a listed channel, direction, and outcome.")
        if (direction == "in" and outcome in {"attempted", "no_answer", "voicemail", "sent"}) or (
            direction == "out" and outcome == "reply_received"
        ):
            raise ActivityError("That outcome does not match the contact direction.")
        if outcome in {"no_answer", "voicemail"} and channel != "call":
            raise ActivityError("No answer and left voicemail apply to phone calls.")
        if outcome == "sent" and channel not in {"email", "sms"}:
            raise ActivityError("Manually sent applies to email or text messages.")
        occurred_on = _date(values["occurred_on"], "contact date")
        if occurred_on > studio_mod._today().isoformat():
            raise ActivityError(
                "Contact history cannot be dated in the future. Schedule a next action instead."
            )
        notes = _text(values["notes"], "contact notes", 4000)
        if task_action not in {"keep", "complete", "reschedule"}:
            raise ActivityError("Choose keep, complete, or reschedule for the next action.")
        linked_task, parsed_version, clean_note, clean_date = None, None, "", ""
        if task_action != "keep":
            linked_task = _positive(task_id, "action identity")
            parsed_version, clean_note, clean_date = _command_values(
                task_action, version, values["task_note"], values["due_date"]
            )
        digest = _digest(
            {
                "prospect_id": prospect_id,
                "channel": channel,
                "direction": direction,
                "outcome": outcome,
                "occurred_on": occurred_on,
                "notes": notes,
                "task_action": task_action,
                "task_id": linked_task,
                "version": parsed_version,
                "task_note": clean_note,
                "due_date": clean_date,
            }
        )
        with db.tx() as con:
            _prospect(con, prospect_id)
            previous = con.execute(
                "SELECT * FROM prospect_contact_events WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if previous:
                if previous["prospect_id"] != prospect_id or previous["request_digest"] != digest:
                    raise ActivityError(
                        "This operation key already belongs to a different saved contact. Review the current history.",
                        409,
                    )
                return _redirect(prospect_id)
            if linked_task is not None:
                _change_task(
                    con,
                    prospect_id,
                    linked_task,
                    parsed_version,
                    task_action,
                    clean_note,
                    clean_date,
                    allow_retry=False,
                )
            event_id = con.execute(
                "INSERT INTO prospect_contact_events (prospect_id,operation_key,request_digest,channel,direction,outcome,occurred_on,notes,task_id,task_action) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    prospect_id,
                    operation_key,
                    digest,
                    channel,
                    direction,
                    outcome,
                    occurred_on,
                    notes,
                    linked_task,
                    task_action,
                ),
            ).lastrowid
            audit.log(
                con,
                "prospect_contact",
                event_id,
                "record",
                diff={
                    "prospect_id": prospect_id,
                    "channel": channel,
                    "direction": direction,
                    "outcome": outcome,
                    "occurred_on": occurred_on,
                    "notes": notes,
                    "provenance": "manual",
                    "task_id": linked_task,
                    "task_action": task_action,
                },
            )
    except ActivityError as error:
        return _error(request, prospect_id, values, "contact", error)
    except sqlite3.IntegrityError:
        return _error(
            request,
            prospect_id,
            values,
            "contact",
            ActivityError(
                "This contact could not be saved because the record changed. Review the current history and next action.",
                409,
            ),
        )
    return _redirect(prospect_id)
