"""Reviewed prospect -> client/project handoff; no documents, sends or bookings."""

import json
import sqlite3
import uuid
from contextlib import closing

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, db, outreach, security, specialties
from ..render import templates
from ..sales import commands, rules
from . import studio

router = APIRouter(prefix="/admin/studio/prospects", dependencies=[Depends(security.require_admin)])
REVIEW_MAX_AGE = 3600


def _form_values(
    client_id: str = Form(""),
    project_id: str = Form("new"),
    title: str = Form(""),
    photography_need: str = Form(""),
    specialty: str = Form(""),
    target_date: str = Form(""),
    task_id: str = Form(""),
    task_version: str = Form(""),
    task_disposition: str = Form(""),
    task_note: str = Form(""),
    interest_confirmed: str = Form(""),
    operation_key: str = Form(""),
) -> dict:
    return {
        "client_id": client_id.strip(),
        "project_id": project_id.strip(),
        "title": title.strip(),
        "photography_need": photography_need.strip(),
        "specialty": specialty,
        "target_date": target_date.strip(),
        "task_id": task_id,
        "task_version": task_version,
        "task_disposition": task_disposition,
        "task_note": task_note.strip(),
        "interest_confirmed": interest_confirmed,
        "operation_key": operation_key,
    }


def _clean(values):
    v = dict(values)
    rules.operation_key(v["operation_key"])
    for field in ("client_id", "project_id"):
        if v[field] != "new":
            rules.positive_id(v[field], field.replace("_", " "))
    if v["client_id"] == "new" and v["project_id"] != "new":
        raise rules.ActivityError("A new client needs a new opportunity.")
    v["title"] = rules.clean_text(
        v["title"], "an opportunity title", 200, required=v["project_id"] == "new"
    )
    v["photography_need"] = rules.clean_text(
        v["photography_need"], "the photography need", 4000, required=True
    )
    if v["specialty"] and v["specialty"] not in specialties.SPECIALTIES:
        raise rules.ActivityError("Choose a listed photography specialty.")
    if v["target_date"]:
        rules.date_value(v["target_date"], "target date")
    if v["interest_confirmed"] != "yes":
        raise rules.ActivityError(
            "Confirm that this is a real photography opportunity before continuing."
        )
    if v["task_disposition"] not in {"none", "keep", "complete", "cancel"}:
        raise rules.ActivityError("Choose what happens to the pending follow-up.")
    if v["task_disposition"] != "none":
        rules.positive_id(v["task_id"], "follow-up identity")
        rules.positive_id(v["task_version"], "follow-up version")
    elif v["task_id"] or v["task_version"]:
        raise rules.ActivityError("Review the pending follow-up before continuing.", 409)
    v["task_note"] = rules.clean_text(
        v["task_note"],
        "a follow-up outcome or reason",
        1000,
        required=v["task_disposition"] in {"complete", "cancel"},
    )
    return v


def _client_matches(con, prospect):
    """Recommend reuse, never automatically merge identities or overwrite a client.

    Matching a business is sufficient to require review even with another contact.
    Legacy data is normalized for comparison only; other client writers are unchanged.
    """
    matched = []
    for row in con.execute("SELECT id, name, company, email, phone FROM clients ORDER BY name, id"):
        reasons = []
        if prospect["email"] and rules.text_key(prospect["email"]) == rules.text_key(row["email"]):
            reasons.append("same email")
        if prospect["phone"] and rules.phone_key(prospect["phone"]) == rules.phone_key(
            row["phone"]
        ):
            reasons.append("same phone")
        if rules.text_key(prospect["business"]) == rules.text_key(row["company"]):
            reasons.append("same business")
        if reasons:
            matched.append({**dict(row), "reason": ", ".join(reasons)})
    return matched


def _pending(con, prospect_id):
    row = con.execute(
        "SELECT * FROM tasks WHERE prospect_id=? AND done=0 AND canceled_at IS NULL", (prospect_id,)
    ).fetchone()
    return dict(row) if row else None


def _resolve(con, prospect_id, values):
    """Resolve the exact reviewed entities on one consistent DB snapshot."""
    row = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Prospect not found")
    prospect = dict(row)
    if prospect["qualification"] == "not_fit":
        raise rules.ActivityError(
            "This prospect is marked Not a fit. Review its qualification first."
        )
    client = project = None
    if values["client_id"] == "new":
        if _client_matches(con, prospect):
            raise rules.ActivityError(
                "A matching client exists. Select and review an existing client before continuing.",
                409,
            )
    else:
        row = con.execute(
            "SELECT * FROM clients WHERE id=?", (int(values["client_id"]),)
        ).fetchone()
        if not row:
            raise rules.ActivityError(
                "The selected client is no longer available. Choose a client again.", 409
            )
        client = dict(row)
    if values["project_id"] != "new":
        row = con.execute(
            "SELECT * FROM projects WHERE id=? AND client_id=?",
            (int(values["project_id"]), int(values["client_id"])),
        ).fetchone()
        if not row or row["status"] in {"archived", "project_closed"}:
            raise rules.ActivityError(
                "Choose an open project belonging to the selected client.", 409
            )
        project = dict(row)
    pending = _pending(con, prospect_id)
    if pending:
        if (
            values["task_disposition"] == "none"
            or str(pending["id"]) != values["task_id"]
            or str(pending["version"]) != values["task_version"]
        ):
            raise rules.ActivityError(
                "The pending follow-up changed. Reopen the handoff and review its current action.",
                409,
            )
    elif values["task_disposition"] != "none":
        raise rules.ActivityError(
            "The follow-up is no longer pending. Reopen the handoff to review the current record.",
            409,
        )
    policy = outreach.for_prospect(
        con,
        prospect_id,
        studio._today().isoformat(),
        extra_client_id=client["id"] if client else None,
    )
    return {
        "prospect": prospect,
        "client": client,
        "project": project,
        "pending": pending,
        "outreach": policy,
    }


def _page(
    request,
    prospect_id,
    values=None,
    *,
    error=None,
    status=200,
    review=None,
    token="",
    initial_client="",
):
    with closing(db.connect()) as con:
        con.execute("BEGIN")
        row = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Prospect not found")
        prospect = dict(row)
        pending = _pending(con, prospect_id)
        if values is None:
            values = {
                "operation_key": str(uuid.uuid4()),
                "client_id": initial_client,
                "project_id": "new",
                "title": f"{prospect['business'][:160]} photography",
                "photography_need": "",
                "specialty": prospect["specialty"],
                "target_date": "",
                "task_id": str(pending["id"]) if pending else "",
                "task_version": str(pending["version"]) if pending else "",
                "task_disposition": "" if pending else "none",
                "task_note": "",
                "interest_confirmed": "",
            }
        clients = [
            dict(r)
            for r in con.execute("SELECT id,name,company,email,phone FROM clients ORDER BY name,id")
        ]
        selected = next((c for c in clients if str(c["id"]) == values["client_id"]), None)
        projects = (
            [
                dict(r)
                for r in con.execute(
                    "SELECT id,title,status,shoot_date FROM projects WHERE client_id=? AND status NOT IN ('archived','project_closed') ORDER BY created_at DESC,id DESC",
                    (selected["id"],),
                )
            ]
            if selected
            else []
        )
        handoff = con.execute(
            "SELECT h.*, p.title AS project_title FROM prospect_handoffs h JOIN projects p ON p.id=h.project_id WHERE h.prospect_id=?",
            (prospect_id,),
        ).fetchone()
        matches = _client_matches(con, prospect)
    return templates.TemplateResponse(
        request,
        "admin/prospect_handoff.html",
        {
            "prospect": prospect,
            "prospect_id": prospect_id,
            "values": values,
            "pending_task": pending,
            "clients": clients,
            "selected_client": selected,
            "projects": projects,
            "matches": matches,
            "handoff": handoff,
            "handoff_snapshot": json.loads(handoff["prospect_snapshot"]) if handoff else None,
            "specialties": specialties.SPECIALTIES,
            "review": review,
            "review_token": token,
            "error": error,
            "conflict": status == 409,
        },
        status_code=status,
    )


@router.get("/{prospect_id}/handoff", response_class=HTMLResponse)
def handoff_form(request: Request, prospect_id: int, client_id: str = ""):
    # Client selection is a read-only first step. A subsequent POST reviews the full plan.
    return _page(request, prospect_id, initial_client=client_id)


@router.post("/{prospect_id}/handoff/edit", response_class=HTMLResponse)
def edit_handoff(request: Request, prospect_id: int, values: dict = Depends(_form_values)):
    return _page(request, prospect_id, values)


@router.post("/{prospect_id}/handoff/review", response_class=HTMLResponse)
def review_handoff(request: Request, prospect_id: int, values: dict = Depends(_form_values)):
    try:
        values = _clean(values)
        with closing(db.connect()) as con:
            con.execute("BEGIN")
            if con.execute(
                "SELECT 1 FROM prospect_handoffs WHERE prospect_id=?", (prospect_id,)
            ).fetchone():
                raise rules.ActivityError(
                    "This prospect already has a project handoff. Open the linked project.", 409
                )
            state = _resolve(con, prospect_id, values)
        receipt = {
            "kind": "prospect_handoff",
            "prospect_id": prospect_id,
            "request": rules.command_digest(values),
            "state": rules.command_digest(state),
        }
        token = security.sign(json.dumps(receipt, sort_keys=True))
        return _page(request, prospect_id, values, review=state, token=token)
    except rules.ActivityError as exc:
        return _page(request, prospect_id, values, error=str(exc), status=exc.status)


@router.post("/{prospect_id}/handoff")
def commit_handoff(
    request: Request,
    prospect_id: int,
    values: dict = Depends(_form_values),
    review_token: str = Form(""),
):
    try:
        values = _clean(values)
        digest = rules.command_digest(values)
        raw = security.unsign(review_token, max_age=REVIEW_MAX_AGE)
        try:
            receipt = json.loads(raw) if isinstance(raw, str) else {}
        except (ValueError, TypeError):
            receipt = {}
        if (
            not isinstance(receipt, dict)
            or receipt.get("kind") != "prospect_handoff"
            or receipt.get("prospect_id") != prospect_id
            or receipt.get("request") != digest
        ):
            raise rules.ActivityError(
                "Review this exact handoff before saving. The review may have expired or its entries changed.",
                409,
            )
        with db.tx() as con:
            previous = con.execute(
                "SELECT * FROM prospect_handoffs WHERE prospect_id=? OR operation_key=?",
                (prospect_id, values["operation_key"]),
            ).fetchone()
            if previous:
                if (
                    previous["prospect_id"] == prospect_id
                    and previous["operation_key"] == values["operation_key"]
                    and previous["request_digest"] == digest
                ):
                    return RedirectResponse(
                        f"/admin/studio/projects/{previous['project_id']}", status_code=303
                    )
                raise rules.ActivityError(
                    "This handoff was already saved. Review the linked project instead of creating another.",
                    409,
                )
            state = _resolve(con, prospect_id, values)
            if receipt.get("state") != rules.command_digest(state):
                raise rules.ActivityError(
                    "The prospect, client, project, follow-up or outreach preferences changed after review. Review the handoff again before saving.",
                    409,
                )
            prospect = state["prospect"]
            client_id = (
                state["client"]["id"]
                if state["client"]
                else con.execute(
                    "INSERT INTO clients (name,company,email,phone) VALUES (?,?,?,?)",
                    (
                        prospect["contact_name"] or prospect["business"],
                        prospect["business"],
                        prospect["email"] or None,
                        prospect["phone"] or None,
                    ),
                ).lastrowid
            )
            project_id = (
                state["project"]["id"]
                if state["project"]
                else con.execute(
                    "INSERT INTO projects (client_id,title,status,notes) VALUES (?,?,'inquiry_received',?)",
                    (client_id, values["title"], values["photography_need"]),
                ).lastrowid
            )
            if not state["client"]:
                audit.log(
                    con,
                    "client",
                    client_id,
                    "create_from_prospect",
                    diff={"prospect_id": prospect_id},
                )
            if not state["project"]:
                audit.log(
                    con,
                    "project",
                    project_id,
                    "create_from_prospect",
                    diff={"prospect_id": prospect_id, "status": "inquiry_received"},
                )
            task_id = state["pending"]["id"] if state["pending"] else None
            if values["task_disposition"] in {"complete", "cancel"}:
                commands.apply_task_change(
                    con,
                    prospect_id,
                    task_id,
                    int(values["task_version"]),
                    values["task_disposition"],
                    values["task_note"],
                    allow_retry=False,
                )
            con.execute(
                "INSERT INTO prospect_handoffs (prospect_id,client_id,project_id,operation_key,request_digest,prospect_snapshot,photography_need,specialty,target_date,task_id,task_disposition,task_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    prospect_id,
                    client_id,
                    project_id,
                    values["operation_key"],
                    digest,
                    json.dumps(prospect, sort_keys=True),
                    values["photography_need"],
                    values["specialty"],
                    values["target_date"] or None,
                    task_id,
                    values["task_disposition"],
                    values["task_note"],
                ),
            )
            audit.log(
                con,
                "prospect",
                prospect_id,
                "handoff",
                diff={
                    "client_id": client_id,
                    "project_id": project_id,
                    "created_client": not bool(state["client"]),
                    "created_project": not bool(state["project"]),
                    "photography_need": values["photography_need"],
                    "specialty": values["specialty"],
                    "target_date": values["target_date"] or None,
                    "task_id": task_id,
                    "task_disposition": values["task_disposition"],
                    "interest_confirmed_by": "operator",
                },
            )
        return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)
    except rules.ActivityError as exc:
        return _page(request, prospect_id, values, error=str(exc), status=exc.status)
    except sqlite3.IntegrityError:
        return _page(
            request,
            prospect_id,
            values,
            error="A linked record changed. Review the current handoff before saving.",
            status=409,
        )
