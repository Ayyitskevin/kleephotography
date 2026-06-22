"""Press / published-work tracking (Domain H, slice 1).

The evidenced source of truth for real-world publication — an outlet ran a piece
of Kevin's F&B work. This is what Domain E's hand-flipped licenses.published flag
is currently guessing at. H stores press hits richly enough for E to consume them
LATER as a read-only JOIN (see press_for_license) — H never writes E state.

Every mutation (create / update / soft-delete) writes through db.tx() so the row
change and its audit_log entry (entity_type='press') commit together, exactly
like licenses.py. Soft-delete sets deleted_at; nothing here hard-deletes a row.

Published-log only: there is no status column. publish_date IS NULL = pending /
not-yet-out; a populated publish_date = published — and that is the gate E reads.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, db, security
from ..render import templates
from ..usage_vocab import CHANNELS
from .licenses import effective_coverage

log = logging.getLogger("mise.admin.press")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

# Columns the diff/audit machinery tracks. Order = form/display order.
_FIELDS = [
    "outlet",
    "title",
    "url",
    "publish_date",
    "channel",
    "credit",
    "note",
    "client_id",
    "project_id",
    "gallery_id",
    "asset_id",
    "show_on_site",
]


def _valid_date(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _parse_form(form) -> dict:
    """Pull press fields out of a submitted form into a normalized dict, validating
    the three constrained inputs: outlet must be non-empty; publish_date, when
    given, must be a real ISO date (it's the E gate — a junk date would poison the
    comparison); channel, when given, must be in the shared CHANNELS vocab (so the
    E channel-overlap seam stays meaningful). Linkage FKs are int-or-None."""
    outlet = (form.get("outlet") or "").strip()
    if not outlet:
        raise HTTPException(status_code=400, detail="outlet required")

    publish_date = (form.get("publish_date") or "").strip() or None
    if publish_date and not _valid_date(publish_date):
        raise HTTPException(status_code=400, detail="bad publish_date")

    channel = (form.get("channel") or "").strip() or None
    if channel and channel not in CHANNELS:
        raise HTTPException(status_code=400, detail="bad channel")

    def fk(key: str):
        v = (form.get(key) or "").strip()
        return int(v) if v.isdigit() else None

    return {
        "outlet": outlet,
        "title": (form.get("title") or "").strip() or None,
        "url": (form.get("url") or "").strip() or None,
        "publish_date": publish_date,
        "channel": channel,
        "credit": (form.get("credit") or "").strip() or None,
        "note": (form.get("note") or "").strip() or None,
        "client_id": fk("client_id"),
        "project_id": fk("project_id"),
        "gallery_id": fk("gallery_id"),
        "asset_id": fk("asset_id"),
        # Public-site opt-in checkbox: present (any truthy value) = 1, absent = 0.
        "show_on_site": 1 if (form.get("show_on_site") or "").strip() else 0,
    }


def get_press(press_id: int) -> "db.sqlite3.Row":
    p = db.one("SELECT * FROM press WHERE id=? AND deleted_at IS NULL", (press_id,))
    if not p:
        raise HTTPException(status_code=404)
    return p


def press_for_license(license_row) -> list[dict]:
    """READ-ONLY H->E seam: published press rows whose linkage overlaps this
    license, each annotated with whether its channel is one the license grants.

    Surfaces a DISPLAY CUE only (H slice 3 will render it on the license page).
    It NEVER writes licenses.published or any E state — consumption is a JOIN,
    suggestion only; flipping the published bit stays a human act on the license.

    Gate (the published-log rule): publish_date IS NOT NULL AND publish_date <=
    today — pending (NULL) and future-dated rows are excluded.

    Linkage overlap (NULL linkage never matches — risk #5 in the scope doc): the
    press row shares the license's gallery_id (sharpest), OR its project_id, OR
    its client_id is within the license's effective coverage (the Domain A walk).

    channel_overlap = the press row's channel is in the license's granted channels;
    its inverse is the channel-overage cue E can surface later.
    """
    import json

    gid = license_row["gallery_id"]
    pid = license_row["project_id"]
    covered = effective_coverage(license_row)

    clauses, params = [], []
    if gid is not None:
        clauses.append("(gallery_id IS NOT NULL AND gallery_id = ?)")
        params.append(gid)
    if pid is not None:
        clauses.append("(project_id IS NOT NULL AND project_id = ?)")
        params.append(pid)
    if covered:
        ph = ",".join("?" * len(covered))
        clauses.append(f"(client_id IS NOT NULL AND client_id IN ({ph}))")
        params.extend(covered)
    if not clauses:
        return []

    rows = db.all_(
        f"""SELECT * FROM press
            WHERE deleted_at IS NULL
              AND publish_date IS NOT NULL AND publish_date <= date('now', 'localtime')
              AND ({" OR ".join(clauses)})
            ORDER BY publish_date DESC, id DESC""",
        tuple(params),
    )

    granted = set(json.loads(license_row["channels"] or "[]"))
    return [
        {
            **{k: r[k] for k in r.keys()},
            "channel_overlap": bool(r["channel"] and r["channel"] in granted),
        }
        for r in rows
    ]


@router.get("/press", response_class=HTMLResponse)
async def press_list(request: Request):
    # All-time ordering (Q4): press is inherently dated, not period-scoped. Pending
    # (no publish_date yet) float to the top as the actionable ones, then published
    # newest-first.
    rows = db.all_(
        """SELECT p.*, c.name AS client_name, c.company,
                  g.title AS gallery_title, pr.title AS project_title
           FROM press p
           LEFT JOIN clients c   ON c.id = p.client_id
           LEFT JOIN galleries g ON g.id = p.gallery_id
           LEFT JOIN projects pr ON pr.id = p.project_id
           WHERE p.deleted_at IS NULL
           ORDER BY p.publish_date IS NULL DESC, p.publish_date DESC, p.id DESC"""
    )
    clients = db.clients_for_select()
    projects = db.all_(
        """SELECT pr.id, pr.title, c.name AS client_name FROM projects pr
           JOIN clients c ON c.id = pr.client_id ORDER BY pr.created_at DESC"""
    )
    galleries = db.all_("SELECT id, title FROM galleries ORDER BY created_at DESC")
    return templates.TemplateResponse(
        request,
        "admin/press.html",
        {
            "press": rows,
            "clients": clients,
            "projects": projects,
            "galleries": galleries,
            "channels_vocab": CHANNELS,
        },
    )


@router.post("/press")
async def create_press(request: Request):
    new = _parse_form(await request.form())
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO press (outlet, title, url, publish_date, channel, credit,
                                  note, client_id, project_id, gallery_id, asset_id,
                                  show_on_site)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new["outlet"],
                new["title"],
                new["url"],
                new["publish_date"],
                new["channel"],
                new["credit"],
                new["note"],
                new["client_id"],
                new["project_id"],
                new["gallery_id"],
                new["asset_id"],
                new["show_on_site"],
            ),
        )
        pid = cur.lastrowid
        audit.log(
            con, "press", pid, "create", diff={k: new[k] for k in _FIELDS if new[k] is not None}
        )
    log.info(
        "press %s created (outlet=%r, published=%s)", pid, new["outlet"], bool(new["publish_date"])
    )
    return RedirectResponse("/admin/studio/press", status_code=303)


@router.post("/press/{press_id}")
async def update_press(request: Request, press_id: int):
    d = get_press(press_id)
    new = _parse_form(await request.form())
    diff = {f: [d[f], new[f]] for f in _FIELDS if (d[f] or None) != (new[f] or None)}
    if not diff:
        return RedirectResponse("/admin/studio/press", status_code=303)
    with db.tx() as con:
        con.execute(
            """UPDATE press SET outlet=?, title=?, url=?, publish_date=?, channel=?,
               credit=?, note=?, client_id=?, project_id=?, gallery_id=?, asset_id=?,
               show_on_site=?, updated_at=datetime('now') WHERE id=?""",
            (
                new["outlet"],
                new["title"],
                new["url"],
                new["publish_date"],
                new["channel"],
                new["credit"],
                new["note"],
                new["client_id"],
                new["project_id"],
                new["gallery_id"],
                new["asset_id"],
                new["show_on_site"],
                press_id,
            ),
        )
        audit.log(con, "press", press_id, "update", diff=diff)
    log.info("press %s updated (%d fields)", press_id, len(diff))
    return RedirectResponse("/admin/studio/press", status_code=303)


@router.post("/press/{press_id}/delete")
async def delete_press(press_id: int):
    d = get_press(press_id)
    with db.tx() as con:
        con.execute("UPDATE press SET deleted_at=datetime('now') WHERE id=?", (press_id,))
        audit.log(
            con,
            "press",
            press_id,
            "soft_delete",
            diff={"outlet": d["outlet"], "publish_date": d["publish_date"]},
        )
    log.info("press %s soft-deleted", press_id)
    return RedirectResponse("/admin/studio/press", status_code=303)
