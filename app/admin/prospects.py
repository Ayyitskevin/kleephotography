"""Manual outbound prospect capture. No inquiry, client, project or sender writes."""

import re
from contextlib import closing

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import audit, db, security, specialties
from ..render import templates
from . import prospect_followups

router = APIRouter(prefix="/admin/studio/prospects", dependencies=[Depends(security.require_admin)])
QUALIFICATIONS = {
    "unreviewed": "Needs research",
    "potential_fit": "Potential fit",
    "not_fit": "Not a fit",
}
LIMITS = {
    "business": 200,
    "contact_name": 200,
    "email": 254,
    "phone": 40,
    "source": 200,
    "notes": 4000,
}
IDENTITY_FIELDS = ("business", "contact_name", "email", "phone")
FIELDS = (*IDENTITY_FIELDS, "specialty", "source", "qualification", "notes")
PAGE_SIZE = 50


def _text_key(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _phone_key(value: str | None) -> str:
    # US ten-digit and +1 forms are equivalent. Never guess other country codes.
    # Extensions are not accepted by capture; keep legacy extensions distinct.
    digits = re.sub(r"[^0-9]", "", value or "")
    return "1" + digits if len(digits) == 10 else digits


def _form_values(
    business: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    specialty: str = Form(""),
    source: str = Form(""),
    qualification: str = Form("unreviewed"),
    notes: str = Form(""),
) -> dict:
    return {
        "business": business.strip(),
        "contact_name": contact_name.strip(),
        "email": email.strip(),
        "phone": phone.strip(),
        "specialty": specialty,
        "source": source.strip(),
        "qualification": qualification,
        "notes": notes.strip(),
    }


def _email_ok(value: str) -> bool:
    """One @ and a dotted domain. Same addresses as the old pattern, read once.

    ``[^@\\s]`` also matches ``.``, so ``[^@\\s]+\\.[^@\\s]+`` tried every dot
    on a near-miss. Anything past the field limit is refused before that scan.
    """
    if not value or len(value) > LIMITS["email"] or any(ch.isspace() for ch in value):
        return False
    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return False
    # A dot counts when a character sits on both sides, so a trailing dot
    # (``a@b.c.``) still matches and ``a@b.`` does not.
    return "." in domain[1:-1]


def _validate(values: dict) -> str | None:
    if not values["business"] or not values["source"]:
        return "Enter a business or venue and where you found it."
    for field, limit in LIMITS.items():
        if len(values[field]) > limit:
            return f"{field.replace('_', ' ').capitalize()} must be {limit} characters or fewer."
    if any("\x00" in v for v in values.values()):
        return "Remove the invalid character from the form."
    if values["specialty"] and values["specialty"] not in specialties.SPECIALTIES:
        return "Choose one of the listed photography specialties."
    if values["qualification"] not in QUALIFICATIONS:
        return "Choose one of the listed qualifications."
    if values["email"] and not _email_ok(values["email"]):
        return "Enter a complete email address, or leave it blank."
    phone = values["phone"]
    if phone and (
        not re.fullmatch(r"\+?[0-9 ()\-.]+", phone)
        or not 10 <= len(re.sub(r"[^0-9]", "", phone)) <= 15
    ):
        return "Use a 10-digit US phone or a country-code number; put extensions in notes."
    return None


def _matches(con, values: dict, exclude_id: int | None = None) -> list[dict]:
    """Compare legacy records without rewriting them or assuming client means paid.

    Exact normalized email, phone, or business + contact matches block capture.
    A small solo CRM can scan these columns; imports need an indexed identity model.
    Archived/converted inquiries still count, preserving their conversation history.
    """
    email = _text_key(values["email"])
    phone = _phone_key(values["phone"])
    business = _text_key(values["business"])
    name = _text_key(values["contact_name"])
    sources = (
        (
            "Outbound prospect",
            "SELECT id, business, contact_name, email, phone FROM prospects",
            "/admin/studio/prospects/",
        ),
        (
            "Client record",
            "SELECT id, company AS business, name AS contact_name, email, phone FROM clients",
            "/admin/studio/clients/",
        ),
        (
            "Inbound inquiry",
            "SELECT id, business, name AS contact_name, email, phone, converted_at, dismissed_at FROM inquiries",
            "/admin/inbox?",
        ),
    )
    found = []
    for kind, sql, prefix in sources:
        for row in con.execute(sql):
            if kind == "Outbound prospect" and row["id"] == exclude_id:
                continue
            reasons = []
            if email and email == _text_key(row["email"]):
                reasons.append("same email")
            if phone and phone == _phone_key(row["phone"]):
                reasons.append("same phone")
            if business == _text_key(row["business"]) and name == _text_key(row["contact_name"]):
                reasons.append("same business and contact")
            if reasons:
                href = f"{prefix}{row['id']}"
                if kind == "Inbound inquiry":
                    tab = "archived" if row["converted_at"] or row["dismissed_at"] else "all"
                    href = f"{prefix}tab={tab}&sel={row['id']}"
                found.append(
                    {
                        "kind": kind,
                        "label": row["business"] or row["contact_name"],
                        "contact": row["contact_name"],
                        "href": href,
                        "reason": ", ".join(reasons),
                    }
                )
    return found


def _form(request, values, *, prospect_id=None, error=None, matches=(), reviewed=False, status=200):
    activity = prospect_followups.detail_context(prospect_id) if prospect_id else {}
    return templates.TemplateResponse(
        request,
        "admin/prospect_form.html",
        {
            **activity,
            "values": values,
            "prospect_id": prospect_id,
            "error": error,
            "matches": matches,
            "reviewed": reviewed,
            "qualifications": QUALIFICATIONS,
            "specialties": specialties.SPECIALTIES,
        },
        status_code=status,
    )


@router.get("", response_class=HTMLResponse)
def prospect_list(request: Request, q: str = "", page: int = 1):
    q = q.strip()[:200]
    page = max(1, page)
    where = "instr(lower(business || ' ' || contact_name || ' ' || email || ' ' || phone), lower(?)) > 0"
    total = db.one(f"SELECT COUNT(*) AS n FROM prospects WHERE {where}", (q,))["n"]
    page = min(page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    rows = db.all_(
        f"SELECT * FROM prospects WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (q, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    return templates.TemplateResponse(
        request,
        "admin/prospects.html",
        {
            "prospects": rows,
            "q": q,
            "page": page,
            "total": total,
            "has_next": page * PAGE_SIZE < total,
            "qualifications": QUALIFICATIONS,
            "specialties": specialties.SPECIALTIES,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_prospect(request: Request):
    return _form(request, {"qualification": "unreviewed"})


@router.post("/review", response_class=HTMLResponse)
def review_prospect(request: Request, values: dict = Depends(_form_values)):
    if error := _validate(values):
        return _form(request, values, error=error, status=400)
    with closing(db.connect()) as con:
        matches = _matches(con, values)
    return _form(request, values, matches=matches, reviewed=True)


@router.post("")
def create_prospect(request: Request, values: dict = Depends(_form_values)):
    if error := _validate(values):
        return _form(request, values, error=error, status=400)
    # The review page is advisory; the authoritative check is under the write lock.
    with db.tx() as con:
        matches = _matches(con, values)
        if matches:
            return _form(request, values, matches=matches, reviewed=True, status=409)
        pid = con.execute(
            "INSERT INTO prospects (business, contact_name, email, phone, specialty, source, "
            "qualification, notes, business_key, contact_key, email_key, phone_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *(values[field] for field in FIELDS),
                _text_key(values["business"]),
                _text_key(values["contact_name"]),
                _text_key(values["email"]) or None,
                _phone_key(values["phone"]) or None,
            ),
        ).lastrowid
        audit.log(con, "prospect", pid, "create", diff=values)
    return prospect_followups._redirect(pid)


@router.get("/{prospect_id}", response_class=HTMLResponse)
def prospect_detail(request: Request, prospect_id: int):
    row = db.get_or_404("SELECT * FROM prospects WHERE id=?", (prospect_id,))
    return _form(request, dict(row), prospect_id=prospect_id)


@router.post("/{prospect_id}")
def update_prospect(request: Request, prospect_id: int, values: dict = Depends(_form_values)):
    if error := _validate(values):
        return _form(request, values, prospect_id=prospect_id, error=error, status=400)
    with db.tx() as con:
        old = con.execute("SELECT * FROM prospects WHERE id=?", (prospect_id,)).fetchone()
        if old is None:
            raise HTTPException(status_code=404, detail="Prospect not found")
        # A later inbound inquiry must not prevent updating research/qualification.
        # Changing identity does require checking all records again.
        if any(values[field] != old[field] for field in IDENTITY_FIELDS):
            matches = _matches(con, values, exclude_id=prospect_id)
            if matches:
                return _form(request, values, prospect_id=prospect_id, matches=matches, status=409)
        con.execute(
            "UPDATE prospects SET business=?, contact_name=?, email=?, phone=?, specialty=?, "
            "source=?, qualification=?, notes=?, business_key=?, contact_key=?, email_key=?, "
            "phone_key=?, updated_at=datetime('now') WHERE id=?",
            (
                *(values[field] for field in FIELDS),
                _text_key(values["business"]),
                _text_key(values["contact_name"]),
                _text_key(values["email"]) or None,
                _phone_key(values["phone"]) or None,
                prospect_id,
            ),
        )
        audit.log(
            con,
            "prospect",
            prospect_id,
            "update",
            diff={
                field: [old[field], value] for field, value in values.items() if old[field] != value
            },
        )
    return prospect_followups._redirect(prospect_id)
