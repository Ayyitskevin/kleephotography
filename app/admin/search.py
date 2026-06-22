"""Global admin search — one box across clients, projects, proposals,
invoices, and galleries. Read-only; links straight to each record."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import db, security
from ..render import templates

log = logging.getLogger("mise.admin.search")
router = APIRouter(prefix="/admin/search", dependencies=[Depends(security.require_admin)])

_PER = 10  # cap per category — this is a jump-to box, not a report


def _like(q: str) -> str:
    """Escape LIKE wildcards in user input so a literal % or _ matches itself."""
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


@router.get("", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    q = q.strip()
    groups: list[dict] = []
    if q:
        like = _like(q)
        clients = db.all_(
            """SELECT id, name, company, email FROM clients
               WHERE name LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\'
                  OR email LIKE ? ESCAPE '\\'
               ORDER BY name LIMIT ?""",
            (like, like, like, _PER),
        )
        groups.append(
            {
                "label": "Clients",
                "rows": [
                    {
                        "title": c["company"] or c["name"],
                        "meta": (c["name"] if c["company"] else None) or c["email"] or "",
                        "url": f"/admin/studio/clients/{c['id']}",
                    }
                    for c in clients
                ],
            }
        )

        projects = db.all_(
            """SELECT p.id, p.title, p.status, c.name AS client_name, c.company
               FROM projects p JOIN clients c ON c.id=p.client_id
               WHERE p.title LIKE ? ESCAPE '\\'
               ORDER BY p.created_at DESC LIMIT ?""",
            (like, _PER),
        )
        groups.append(
            {
                "label": "Projects",
                "rows": [
                    {
                        "title": p["title"],
                        "meta": f"{p['company'] or p['client_name']} · {p['status']}",
                        "url": f"/admin/studio/projects/{p['id']}",
                    }
                    for p in projects
                ],
            }
        )

        proposals = db.all_(
            """SELECT pr.id, pr.title, pr.status, p.title AS project_title
               FROM proposals pr JOIN projects p ON p.id=pr.project_id
               WHERE pr.title LIKE ? ESCAPE '\\'
               ORDER BY pr.id DESC LIMIT ?""",
            (like, _PER),
        )
        groups.append(
            {
                "label": "Proposals",
                "rows": [
                    {
                        "title": pr["title"],
                        "meta": f"{pr['project_title']} · {pr['status']}",
                        "url": f"/admin/studio/proposals/{pr['id']}",
                    }
                    for pr in proposals
                ],
            }
        )

        invoices = db.all_(
            """SELECT i.id, i.title, i.status, i.total_cents, p.title AS project_title
               FROM invoices i JOIN projects p ON p.id=i.project_id
               WHERE i.title LIKE ? ESCAPE '\\'
               ORDER BY i.id DESC LIMIT ?""",
            (like, _PER),
        )
        groups.append(
            {
                "label": "Invoices",
                "rows": [
                    {
                        "title": i["title"],
                        "meta": f"{i['project_title']} · {i['status']} · "
                        f"${'%.2f' % (i['total_cents'] / 100)}",
                        "url": f"/admin/studio/invoices/{i['id']}",
                    }
                    for i in invoices
                ],
            }
        )

        galleries = db.all_(
            """SELECT id, title, client_name FROM galleries
               WHERE title LIKE ? ESCAPE '\\' OR client_name LIKE ? ESCAPE '\\'
               ORDER BY created_at DESC LIMIT ?""",
            (like, like, _PER),
        )
        groups.append(
            {
                "label": "Galleries",
                "rows": [
                    {
                        "title": g["title"],
                        "meta": g["client_name"] or "",
                        "url": f"/admin/galleries/{g['id']}",
                    }
                    for g in galleries
                ],
            }
        )

    total = sum(len(g["rows"]) for g in groups)
    return templates.TemplateResponse(
        request, "admin/search.html", {"q": q, "groups": groups, "total": total}
    )
