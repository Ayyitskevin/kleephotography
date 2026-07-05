import datetime as dt
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import audit, config, db, jobs, reopen_notify, security
from ..render import templates

log = logging.getLogger("mise.public.gallery")
router = APIRouter(prefix="/g")


def get_live_gallery(slug: str) -> "db.sqlite3.Row":
    g = db.one("SELECT * FROM galleries WHERE slug=?", (slug,))
    if not g or not g["published"]:
        raise HTTPException(status_code=404)
    return g


def is_expired(g) -> bool:
    return bool(g["expires_at"]) and g["expires_at"] < dt.date.today().isoformat()


@router.get("/{slug}", response_class=HTMLResponse)
async def view(request: Request, slug: str):
    g = get_live_gallery(slug)
    if is_expired(g):
        return templates.TemplateResponse(request, "public/expired.html", {"g": g}, status_code=410)
    if g["type"] == "drop":
        return _view_drop(request, g)
    visitor = security.get_visitor(request, g["id"])
    if not visitor:
        return templates.TemplateResponse(request, "public/pin.html", {"g": g, "error": None})
    sections = db.all_("SELECT * FROM sections WHERE gallery_id=? ORDER BY position", (g["id"],))
    assets = db.all_(
        """SELECT * FROM assets WHERE gallery_id=? AND status='ready'
                        ORDER BY section_id, position, id""",
        (g["id"],),
    )
    favs = {
        r["asset_id"]
        for r in db.all_("SELECT asset_id FROM favorites WHERE visitor_id=?", (visitor["id"],))
    }
    # this visitor's selection count per proofing section, for the progress label
    section_picks = {
        s["id"]: sum(1 for a in assets if a["section_id"] == s["id"] and a["id"] in favs)
        for s in sections
    }
    by_section: dict = {s["id"]: [] for s in sections}
    unsectioned = []
    for a in assets:
        if a["section_id"] in by_section:
            by_section[a["section_id"]].append(a)
        else:
            unsectioned.append(a)
    return templates.TemplateResponse(
        request,
        "public/gallery.html",
        {
            "g": g,
            "sections": sections,
            "by_section": by_section,
            "unsectioned": unsectioned,
            "assets": assets,
            "favs": favs,
            "section_picks": section_picks,
            "visitor": visitor,
        },
    )


def _view_drop(request: Request, g):
    """WeTransfer-style transfer page. When require_pin=0 (link-only) we mint a
    visitor on first view so the existing email-free download + tracking still
    work; when require_pin=1 the normal PIN gate applies."""
    visitor = security.get_visitor(request, g["id"])
    new_cookie = None
    if not visitor:
        if g["require_pin"]:
            return templates.TemplateResponse(request, "public/pin.html", {"g": g, "error": None})
        vid, new_cookie = security.create_visitor(g["id"])
        visitor = {"id": vid}
    assets = db.all_(
        """SELECT * FROM assets WHERE gallery_id=? AND status='ready'
                        ORDER BY position, id""",
        (g["id"],),
    )
    resp = templates.TemplateResponse(
        request, "public/drop.html", {"g": g, "assets": assets, "visitor": visitor}
    )
    if new_cookie:
        security.set_session_cookie(resp, security.visitor_cookie_name(g["id"]), new_cookie)
    return resp


@router.post("/{slug}/pin")
async def check_pin(request: Request, slug: str, pin: str = Form(...)):
    g = get_live_gallery(slug)
    if is_expired(g):
        raise HTTPException(status_code=410)
    ip = security.client_ip(request)
    if security.pin_locked(ip, g["id"]):
        return templates.TemplateResponse(
            request,
            "public/pin.html",
            {"g": g, "error": f"Too many tries — wait {config.PIN_LOCKOUT_MIN} minutes."},
            status_code=429,
        )
    if pin.strip() != g["pin"]:
        security.pin_fail(ip, g["id"])
        return templates.TemplateResponse(
            request, "public/pin.html", {"g": g, "error": "Wrong PIN."}, status_code=401
        )
    security.pin_clear(ip, g["id"])
    _, cookie_val = security.create_visitor(g["id"])
    resp = RedirectResponse(f"/g/{slug}", status_code=303)
    security.set_session_cookie(resp, security.visitor_cookie_name(g["id"]), cookie_val)
    return resp


def _progress_oob(g_id: int, section_id: int | None, visitor_id: int) -> str:
    """OOB-swap fragment for the section heading's progress label, returned
    alongside the heart so it updates inline after every fav/unfav."""
    if section_id is None:
        return ""
    s = db.one("SELECT proof_target FROM sections WHERE id=?", (section_id,))
    if not s or not s["proof_target"]:
        return ""
    picks = db.one(
        """SELECT COUNT(*) AS n FROM favorites f JOIN assets a ON a.id=f.asset_id
                      WHERE f.visitor_id=? AND a.section_id=?""",
        (visitor_id, section_id),
    )["n"]
    cls = "ok" if picks >= s["proof_target"] else "muted"
    return (
        f'<span id="proof-{section_id}" class="proof-progress {cls}" '
        f'hx-swap-oob="outerHTML">{picks} of {s["proof_target"]} picked</span>'
    )


@router.post("/{slug}/fav/{asset_id}", response_class=HTMLResponse)
async def toggle_fav(request: Request, slug: str, asset_id: int):
    g = get_live_gallery(slug)
    # Once a gallery expires it 410s everywhere else; without this a visitor
    # holding a live cookie could keep changing their proofing picks after the
    # selection window closed. The gallery.html htmx:responseError handler turns
    # this 410 into a reload to the expired page.
    if is_expired(g):
        raise HTTPException(status_code=410)
    visitor = security.require_visitor(request, g["id"])
    a = db.one(
        """SELECT a.id, a.kind, a.status, a.section_id, s.proof_target
                  FROM assets a LEFT JOIN sections s ON s.id=a.section_id
                  WHERE a.id=? AND a.gallery_id=?""",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    existing = db.one(
        "SELECT 1 AS x FROM favorites WHERE visitor_id=? AND asset_id=?", (visitor["id"], asset_id)
    )
    if existing:
        db.run("DELETE FROM favorites WHERE visitor_id=? AND asset_id=?", (visitor["id"], asset_id))
        oob = _progress_oob(g["id"], a["section_id"], visitor["id"])
        return Response('<span class="fav-btn">&#9825;</span>' + oob)
    # Proofing cap: refuse the fav if the visitor already hit the section's target.
    if a["proof_target"]:
        picks = db.one(
            """SELECT COUNT(*) AS n FROM favorites f
                          JOIN assets x ON x.id=f.asset_id
                          WHERE f.visitor_id=? AND x.section_id=?""",
            (visitor["id"], a["section_id"]),
        )["n"]
        if picks >= a["proof_target"]:
            return Response(
                '<span class="fav-btn">&#9825;</span>',
                status_code=409,
                headers={"HX-Trigger": f'{{"proof-cap":{{"target":{a["proof_target"]}}}}}'},
            )
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (visitor["id"], asset_id))
    if a["kind"] == "photo" and a["status"] == "ready":
        jobs.enqueue("social_crops", {"asset_id": asset_id})
    oob = _progress_oob(g["id"], a["section_id"], visitor["id"])
    return Response('<span class="fav-btn faved">&#9829;</span>' + oob)


# ── Timecoded review comments on video deliverables (Domain C slice 3) ───────


def video_comment_thread(asset_id: int) -> list[dict]:
    """Visible (non-hidden) thread for one video asset, ordered for display.
    Flat list; the client/admin renderers nest it by parent_id. author_role is
    the only identity surfaced — no visitor email/PII leaks into the thread."""
    rows = db.all_(
        """SELECT id, parent_id, timecode, body, author_role, status, created_at
                      FROM video_comments
                      WHERE asset_id=? AND deleted_at IS NULL
                      ORDER BY timecode, created_at, id""",
        (asset_id,),
    )
    return [dict(r) for r in rows]


def _cascade_status(con, root_id: int, status: str) -> None:
    """Set status on a thread root AND its whole subtree in one recursive UPDATE,
    so a thread never carries mixed status. Runs on the caller's tx connection.
    Shared by the admin open⇄resolved transition and the client-reply auto-reopen."""
    con.execute(
        """WITH RECURSIVE sub(id) AS (
             SELECT id FROM video_comments WHERE id=?
             UNION ALL
             SELECT vc.id FROM video_comments vc JOIN sub ON vc.parent_id=sub.id)
           UPDATE video_comments SET status=?
           WHERE id IN (SELECT id FROM sub)""",
        (root_id, status),
    )


def resolve_comment_parent(asset_id: int, parent_raw) -> tuple[int | None, float]:
    """Validate an optional reply target and return (parent_id, timecode).
    A reply inherits its parent's timecode (denormalized at insert) so the tree
    needs no join to sort. Top-level returns (None, -1.0) → caller uses the
    posted timecode. Raises 400 if the parent is bogus or on another asset."""
    if not parent_raw:
        return None, -1.0
    try:
        pid = int(parent_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad parent_id")
    p = db.one(
        "SELECT timecode FROM video_comments WHERE id=? AND asset_id=? AND deleted_at IS NULL",
        (pid, asset_id),
    )
    if not p:
        raise HTTPException(status_code=400, detail="reply target not found")
    return pid, float(p["timecode"])


def _maybe_reopen_on_reply(con, reply_id: int):
    """A client reply resurfaces a thread the studio already resolved (transitions
    are admin-only, so a reply is the client's only recourse against an early
    resolve). Walk parent links up to the thread root; if that root is visible and
    currently resolved, cascade the whole thread back to open. The reopen is a
    machine-caused status change → a system-attributed audit row records the
    triggering reply, keeping 'every status change is traceable' intact.

    Returns a notify payload dict on an actual reopen (so the caller can push a
    best-effort studio alert AFTER the tx commits), else None when nothing changed."""
    root = con.execute(
        """WITH RECURSIVE up(id, parent_id) AS (
             SELECT id, parent_id FROM video_comments WHERE id=?
             UNION ALL
             SELECT vc.id, vc.parent_id FROM video_comments vc JOIN up ON vc.id=up.parent_id)
           SELECT vc.id, vc.asset_id, vc.status, vc.deleted_at
           FROM video_comments vc JOIN up ON vc.id=up.id
           WHERE up.parent_id IS NULL""",
        (reply_id,),
    ).fetchone()
    if not root or root["deleted_at"] is not None:
        return None
    if (root["status"] or "open") != "resolved":
        return None
    _cascade_status(con, root["id"], "open")
    audit.log(
        con,
        "video_comment",
        root["id"],
        "open",
        diff={
            "asset_id": root["asset_id"],
            "from": "resolved",
            "to": "open",
            "cause_reply_id": reply_id,
        },
        actor="system",
    )
    return {"root_id": root["id"], "asset_id": root["asset_id"], "cause_reply_id": reply_id}


def _live_video_asset(request: Request, slug: str, asset_id: int):
    """Shared gate for the client comment routes: live + unexpired gallery,
    valid visitor cookie, and a ready video asset in that gallery."""
    g = get_live_gallery(slug)
    if is_expired(g):
        raise HTTPException(status_code=410)
    visitor = security.require_visitor(request, g["id"])
    a = db.one(
        "SELECT id FROM assets WHERE id=? AND gallery_id=? AND kind='video' AND status='ready'",
        (asset_id, g["id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    return g, visitor


@router.get("/{slug}/comments/{asset_id}")
async def list_comments(request: Request, slug: str, asset_id: int):
    _live_video_asset(request, slug, asset_id)
    return JSONResponse(video_comment_thread(asset_id))


@router.post("/{slug}/comments/{asset_id}")
async def add_comment(
    request: Request,
    slug: str,
    asset_id: int,
    body: str = Form(...),
    timecode: float = Form(0.0),
    parent_id: str = Form(""),
):
    g, visitor = _live_video_asset(request, slug, asset_id)
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="comment body required")
    parent, inherited = resolve_comment_parent(asset_id, parent_id)
    tc = inherited if parent is not None else max(0.0, timecode)
    reopened = None
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO video_comments
               (asset_id, gallery_id, parent_id, visitor_id, author_role, timecode, body)
               VALUES (?,?,?,?,?,?,?)""",
            (asset_id, g["id"], parent, visitor["id"], "client", tc, body),
        )
        # New activity reopens a resolved thread; a fresh top-level comment is its
        # own new (open) thread, so only replies can resurface anything.
        if parent is not None:
            reopened = _maybe_reopen_on_reply(con, cur.lastrowid)
    # The reopen + its audit row are now durably committed. The studio push rides
    # on top: best-effort, fired AFTER commit so a slow/down Odysseus can never roll
    # back or block the client's comment. notify_reopen never raises, but belt-and-
    # suspenders the call too — nothing here may surface to the client.
    if reopened is not None:
        try:
            reopen_notify.notify_reopen(
                {"gallery_slug": g["slug"], "gallery_title": g["title"], **reopened}
            )
        except Exception:
            log.exception("reopen notify dispatch failed (non-fatal)")
    return JSONResponse(video_comment_thread(asset_id))
