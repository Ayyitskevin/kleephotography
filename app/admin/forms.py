"""Custom form builder (Phase 4) — lead-capture forms and client
questionnaires. Forms get an unguessable public slug served at /forms/{slug}
by app.public.forms. Submissions land in the per-form inbox here; lead-kind
submissions also spawn an inquiries row + email (handled on the public side)
so the studio Leads pipeline and Odysseus inquiry_intake keep working."""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, security
from ..render import templates

log = logging.getLogger("mise.admin.forms")
router = APIRouter(prefix="/admin/forms", dependencies=[Depends(security.require_admin)])

FTYPES = ["short_text", "long_text", "dropdown", "checkbox", "date", "email", "yesno"]
FTYPE_LABELS = {
    "short_text": "Short text",
    "long_text": "Long text",
    "dropdown": "Dropdown",
    "checkbox": "Checkboxes (multi-select)",
    "date": "Date",
    "email": "Email",
    "yesno": "Yes / No",
}
# Field types that carry a choice list (one option per line).
OPTION_FTYPES = ("dropdown", "checkbox")
KINDS = ["lead", "questionnaire"]


def get_form(form_id: int) -> "db.sqlite3.Row":
    return db.get_or_404("SELECT * FROM forms WHERE id=?", (form_id,))


def get_field(field_id: int) -> "db.sqlite3.Row":
    return db.get_or_404("SELECT * FROM form_fields WHERE id=?", (field_id,))


def _parse_options(raw: str) -> str | None:
    """Dropdown/checkbox choices arrive as one-per-line text; store as a JSON
    array. Fields without a choice list carry no options."""
    opts = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return json.dumps(opts) if opts else None


@router.get("", response_class=HTMLResponse)
async def forms_list(request: Request):
    rows = db.all_(
        """SELECT f.*,
                  (SELECT COUNT(*) FROM form_fields ff WHERE ff.form_id=f.id) AS n_fields,
                  (SELECT COUNT(*) FROM form_submissions s WHERE s.form_id=f.id) AS n_subs,
                  (SELECT MAX(s.created_at) FROM form_submissions s WHERE s.form_id=f.id)
                    AS last_sub
           FROM forms f ORDER BY f.created_at DESC"""
    )
    leads = [r for r in rows if r["kind"] == "lead"]
    quests = [r for r in rows if r["kind"] == "questionnaire"]
    last_sub = max((r["last_sub"] for r in rows if r["last_sub"]), default=None)
    summary = {
        "total": len(rows),
        "active": sum(1 for r in rows if r["active"]),
        "subs": sum(r["n_subs"] for r in rows),
        "last_sub": last_sub,
    }
    return templates.TemplateResponse(
        request, "admin/forms_list.html", {"leads": leads, "quests": quests, "summary": summary}
    )


@router.post("")
async def form_create(title: str = Form(...), kind: str = Form("lead")):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="bad kind")
    slug = security.new_slug()
    fid = db.run("INSERT INTO forms (slug, title, kind) VALUES (?,?,?)", (slug, title, kind))
    log.info("form %s created: %s (%s)", fid, title, kind)
    return RedirectResponse(f"/admin/forms/{fid}", status_code=303)


@router.get("/{form_id}", response_class=HTMLResponse)
async def form_builder(request: Request, form_id: int):
    f = get_form(form_id)
    fields = db.all_(
        "SELECT * FROM form_fields WHERE form_id=? ORDER BY sort_order, id", (form_id,)
    )
    subs = db.all_(
        """SELECT id, name, email, created_at FROM form_submissions
           WHERE form_id=? ORDER BY created_at DESC LIMIT 10""",
        (form_id,),
    )
    n_subs = db.one("SELECT COUNT(*) AS n FROM form_submissions WHERE form_id=?", (form_id,))["n"]
    parsed = []
    for fld in fields:
        d = dict(fld)
        d["opts"] = json.loads(fld["options"]) if fld["options"] else []
        parsed.append(d)
    return templates.TemplateResponse(
        request,
        "admin/form_builder.html",
        {
            "f": f,
            "fields": parsed,
            "subs": subs,
            "n_subs": n_subs,
            "ftypes": FTYPES,
            "ftype_labels": FTYPE_LABELS,
        },
    )


@router.post("/{form_id}")
async def form_update(
    form_id: int, title: str = Form(...), intro: str = Form(""), active: bool = Form(False)
):
    get_form(form_id)
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    db.run(
        "UPDATE forms SET title=?, intro=?, active=? WHERE id=?",
        (title, intro.strip() or None, 1 if active else 0, form_id),
    )
    log.info("form %s updated (active=%s)", form_id, active)
    return RedirectResponse(f"/admin/forms/{form_id}", status_code=303)


@router.post("/{form_id}/delete")
async def form_delete(form_id: int):
    get_form(form_id)
    db.run("DELETE FROM forms WHERE id=?", (form_id,))
    log.info("form %s deleted", form_id)
    return RedirectResponse("/admin/forms", status_code=303)


@router.post("/{form_id}/fields")
async def field_add(
    form_id: int,
    label: str = Form(...),
    ftype: str = Form("short_text"),
    required: bool = Form(False),
    options: str = Form(""),
):
    get_form(form_id)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    if ftype not in FTYPES:
        raise HTTPException(status_code=400, detail="bad field type")
    nxt = db.one(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM form_fields WHERE form_id=?", (form_id,)
    )["n"]
    opts = _parse_options(options) if ftype in OPTION_FTYPES else None
    db.run(
        """INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
              VALUES (?,?,?,?,?,?)""",
        (form_id, label, ftype, 1 if required else 0, opts, nxt),
    )
    log.info("form %s + field '%s' (%s)", form_id, label, ftype)
    return RedirectResponse(f"/admin/forms/{form_id}", status_code=303)


@router.post("/fields/{field_id}")
async def field_update(
    field_id: int,
    label: str = Form(...),
    ftype: str = Form("short_text"),
    required: bool = Form(False),
    options: str = Form(""),
):
    fld = get_field(field_id)
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    if ftype not in FTYPES:
        raise HTTPException(status_code=400, detail="bad field type")
    opts = _parse_options(options) if ftype in OPTION_FTYPES else None
    db.run(
        "UPDATE form_fields SET label=?, ftype=?, required=?, options=? WHERE id=?",
        (label, ftype, 1 if required else 0, opts, field_id),
    )
    log.info("field %s updated", field_id)
    return RedirectResponse(f"/admin/forms/{fld['form_id']}", status_code=303)


@router.post("/fields/{field_id}/delete")
async def field_delete(field_id: int):
    fld = get_field(field_id)
    db.run("DELETE FROM form_fields WHERE id=?", (field_id,))
    log.info("field %s deleted", field_id)
    return RedirectResponse(f"/admin/forms/{fld['form_id']}", status_code=303)


@router.post("/fields/{field_id}/move")
async def field_move(field_id: int, dir: str = Form(...)):
    fld = get_field(field_id)
    if dir not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad direction")
    op, order = ("<", "DESC") if dir == "up" else (">", "ASC")
    neighbor = db.one(
        f"""SELECT id, sort_order FROM form_fields
            WHERE form_id=? AND sort_order {op} ?
            ORDER BY sort_order {order} LIMIT 1""",
        (fld["form_id"], fld["sort_order"]),
    )
    if neighbor:
        db.run(
            "UPDATE form_fields SET sort_order=? WHERE id=?", (neighbor["sort_order"], fld["id"])
        )
        db.run(
            "UPDATE form_fields SET sort_order=? WHERE id=?", (fld["sort_order"], neighbor["id"])
        )
    return RedirectResponse(f"/admin/forms/{fld['form_id']}", status_code=303)


@router.get("/{form_id}/submissions", response_class=HTMLResponse)
async def form_submissions(request: Request, form_id: int):
    f = get_form(form_id)
    fields = db.all_(
        "SELECT id, label FROM form_fields WHERE form_id=? ORDER BY sort_order, id", (form_id,)
    )
    rows = db.all_(
        """SELECT * FROM form_submissions WHERE form_id=?
           ORDER BY created_at DESC""",
        (form_id,),
    )
    subs = []
    for r in rows:
        d = dict(r)
        try:
            d["answers"] = json.loads(r["data"]) if r["data"] else {}
        except (ValueError, TypeError):
            d["answers"] = {}
        subs.append(d)
    return templates.TemplateResponse(
        request, "admin/form_submissions.html", {"f": f, "fields": fields, "subs": subs}
    )
