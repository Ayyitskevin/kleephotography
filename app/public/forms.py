"""Public custom forms (Phase 4) — renders a built form at /forms/{slug} and
accepts submissions. Lead-kind submissions also create an inquiries row and
email Kevin, mirroring the /contact flow, so the studio Leads pipeline and
Odysseus inquiry_intake pick them up unchanged. Questionnaire-kind submissions
just land in the form's admin inbox for Kevin to match to a project by hand."""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, inquiry_notify, jobs, security
from ..render import templates

log = logging.getLogger("mise.public.forms")
router = APIRouter()


def _load_form(slug: str) -> "db.sqlite3.Row":
    f = db.one("SELECT * FROM forms WHERE slug=?", (slug,))
    if not f or not f["active"]:
        raise HTTPException(status_code=404, detail="form not found")
    return f


def _fields(form_id: int) -> list[dict]:
    out = []
    for fld in db.all_(
        "SELECT * FROM form_fields WHERE form_id=? ORDER BY sort_order, id", (form_id,)
    ):
        d = dict(fld)
        d["opts"] = json.loads(fld["options"]) if fld["options"] else []
        out.append(d)
    return out


@router.get("/forms/{slug}", response_class=HTMLResponse)
async def show_form(request: Request, slug: str):
    f = _load_form(slug)
    return templates.TemplateResponse(
        request,
        "site/form.html",
        {"f": f, "fields": _fields(f["id"]), "sent": False, "error": None, "values": {}},
    )


@router.post("/forms/{slug}", response_class=HTMLResponse)
async def submit_form(request: Request, slug: str):
    f = _load_form(slug)
    fields = _fields(f["id"])
    posted = await request.form()

    # Honeypot: real visitors never see the "website" field — bots fill it.
    if (posted.get("website") or "").strip():
        return templates.TemplateResponse(
            request,
            "site/form.html",
            {"f": f, "fields": fields, "sent": True, "error": None, "values": {}},
        )

    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_FORM):
        log.warning("form %s throttled for ip=%s", f["id"], ip)
        return templates.TemplateResponse(
            request,
            "site/form.html",
            {
                "f": f,
                "fields": fields,
                "sent": False,
                "error": "You've submitted a few times recently — give me a moment "
                "before sending another.",
                "values": {},
            },
            status_code=429,
        )

    name = (posted.get("name") or "").strip()
    email = (posted.get("email") or "").strip()
    values = {}
    for fld in fields:
        key = str(fld["id"])
        if fld["ftype"] == "checkbox":
            values[key] = [v.strip() for v in posted.getlist(f"field_{fld['id']}") if v.strip()]
        else:
            values[key] = (posted.get(f"field_{fld['id']}") or "").strip()

    errors = []
    if not name:
        errors.append("your name")
    if not ("@" in email and "." in email.rsplit("@", 1)[-1]):
        errors.append("a valid email")
    for fld in fields:
        val = values[str(fld["id"])]
        if fld["required"] and not val:
            errors.append(fld["label"])
        elif val and fld["ftype"] == "email" and not ("@" in val and "." in val.rsplit("@", 1)[-1]):
            errors.append(f'a valid email for "{fld["label"]}"')
    if errors:
        return templates.TemplateResponse(
            request,
            "site/form.html",
            {
                "f": f,
                "fields": fields,
                "sent": False,
                "error": "Please fill in: " + ", ".join(errors) + ".",
                "values": {"name": name, "email": email, **values},
            },
            status_code=400,
        )

    security.inquiry_record(ip, security.INQUIRY_BUCKET_FORM)

    label_by_id = {str(fld["id"]): fld["label"] for fld in fields}

    def _fmt(v):
        if isinstance(v, list):
            return ", ".join(v) if v else "—"
        return v or "—"

    answer_lines = "\n".join(f"{label_by_id[k]}: {_fmt(values[k])}" for k in values)

    inquiry_id = None
    if f["kind"] == "lead":
        message = (
            f"Lead form: {f['title']}\n\n{answer_lines}"
            if answer_lines
            else f"Lead form: {f['title']}"
        )
        inquiry_id = db.run(
            "INSERT INTO inquiries (name, email, message) VALUES (?,?,?)", (name, email, message)
        )
        inquiry_notify.enqueue_owner_email(inquiry_id)
        jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})

    sid = db.run(
        "INSERT INTO form_submissions (form_id, name, email, data, inquiry_id) VALUES (?,?,?,?,?)",
        (f["id"], name, email, json.dumps(values), inquiry_id),
    )
    log.info("form %s submission %s (kind=%s, inquiry=%s)", f["id"], sid, f["kind"], inquiry_id)

    return templates.TemplateResponse(
        request,
        "site/form.html",
        {"f": f, "fields": fields, "sent": True, "error": None, "values": {}},
    )
