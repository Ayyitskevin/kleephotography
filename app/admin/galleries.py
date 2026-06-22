import datetime as dt
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import audit, config, db, jobs, mailer, security
from ..public.gallery import _cascade_status, resolve_comment_parent
from ..render import templates

log = logging.getLogger("mise.admin.galleries")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])

# F&B starting points — editable per gallery after creation.
DEFAULT_SECTIONS = ["Hero Dishes", "Menu Items", "Drinks", "Interiors & Ambience",
                    "Team & Process", "Social Crops"]

# Suggested portfolio-tag values for the admin datalist. Kevin can type any
# value; these just speed up the common ones. /portfolio filter chips are
# computed from whatever tags actually exist on starred assets.
PORTFOLIO_TAG_SUGGESTIONS = ["Dishes", "Drinks", "Interiors", "Team",
                             "Plating", "Behind the Scenes", "Detail"]


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _fmt_size(n: int) -> str:
    if n <= 0:
        return "—"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} KB"


# Strict 1:1 with the Admin Galleries prototype: each card carries a single
# derived status (Delivered/Proofing/Expiring/Draft) and one bottom-right date.
# All four are honest projections of real columns — published, expiry, proofing
# progress, asset counts — never fabricated.
_STATUS_STYLE = {
    "Delivered": ("#2f7d57", "#e1f2e9"),
    "Proofing":  ("#9a7a2c", "#f7ecd2"),
    "Draft":     ("#5C6A5E", "#ecefe6"),
    "Expiring":  ("#7C2F38", "#f3e3e5"),
}


def _short_date(stored: str) -> str:
    """'2026-06-18 12:00:00' → 'Jun 18'. Tolerates a bare date or junk."""
    if not stored:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(stored[:19], fmt).strftime("%b %-d")
        except ValueError:
            continue
    return stored


def _gallery_card(g, today_iso: str, soon_iso: str) -> dict:
    exp = g["expires_at"]
    expired = bool(exp and exp < today_iso)
    expiring_soon = bool(exp and not expired and exp <= soon_iso)
    if not g["published"]:
        status = "Draft"
    elif expired or expiring_soon:
        status = "Expiring"
    elif g["n_proof"] and g["n_proof_pending"]:
        status = "Proofing"
    else:
        status = "Delivered"
    color, bg = _STATUS_STYLE[status]
    if status == "Expiring":
        if expired:
            date_label = "expired"
        else:
            days = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(today_iso)).days
            date_label = f"{days} day{'s' if days != 1 else ''}"
        date_color = "#7C2F38"
    else:
        date_label = _short_date(g["created_at"])
        date_color = "#8A9183"
    n = g["n_assets"]
    photos = f"{n} photo{'s' if n != 1 else ''}" if n else "No photos yet"
    return {
        "id": g["id"], "title": g["title"], "client": g["client_name"] or "—",
        "cover_asset_id": g["cover_asset_id"], "pin": g["pin"],
        "status": status, "status_lc": status.lower(),
        "status_color": color, "status_bg": bg,
        "photos": photos, "favs": g["n_fav"],
        "date": date_label, "date_color": date_color,
    }


def get_gallery(gallery_id: int) -> "db.sqlite3.Row":
    g = db.one("SELECT * FROM galleries WHERE id=?", (gallery_id,))
    if not g:
        raise HTTPException(status_code=404)
    return g


@router.get("")
async def admin_root():
    # Home is the studio landing now; the bare /admin keeps working for old
    # bookmarks and redirects there. Galleries moved to /admin/galleries.
    return RedirectResponse("/admin/home", status_code=307)


@router.get("/galleries", response_class=HTMLResponse)
async def dashboard(request: Request):
    gs = db.all_("""SELECT g.*,
                    (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id) AS n_assets,
                    (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id
                      AND a.status='pending') AS n_pending,
                    (SELECT COUNT(*) FROM favorites f JOIN assets a ON a.id=f.asset_id
                      WHERE a.gallery_id=g.id) AS n_fav,
                    (SELECT COUNT(*) FROM downloads d WHERE d.gallery_id=g.id) AS n_dl,
                    (SELECT COUNT(*) FROM visitors v WHERE v.gallery_id=g.id) AS n_visitors,
                    (SELECT COUNT(*) FROM sections s
                      WHERE s.gallery_id=g.id AND s.proof_target IS NOT NULL
                        AND s.proof_target > 0) AS n_proof,
                    (SELECT COUNT(*) FROM sections s
                      WHERE s.gallery_id=g.id AND s.proof_target IS NOT NULL
                        AND s.proof_target > 0
                        AND (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                             JOIN assets a ON a.id=f.asset_id
                             WHERE a.section_id=s.id) < s.proof_target)
                      AS n_proof_pending
                    FROM galleries g WHERE g.type='gallery'
                    ORDER BY g.created_at DESC""")
    today = dt.date.today()
    failed_jobs = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed'")["n"]
    # Unlinked-but-published galleries — usually orphans from a client force-
    # delete (ship #53) or a manual unlink. Worth surfacing because a published
    # gallery without a studio client means Kevin's lost the inquiry/proposal/
    # invoice context. Unpublished + no-client is fine (could be a draft).
    n_unlinked_pub = sum(1 for g in gs if g["client_id"] is None and g["published"])
    # Pre-build the orphan list and client dropdown so the dashboard template
    # can offer an inline "Link to client" picker per orphan (ship #55) —
    # turns the warn into one-click action instead of click-into-each-gallery.
    orphans = [g for g in gs if g["client_id"] is None and g["published"]]
    link_clients = db.all_("SELECT id, name, company FROM clients ORDER BY name") \
                   if orphans else []
    sizes_b = {g["id"]: _dir_size(config.MEDIA_DIR / str(g["id"])) for g in gs}
    sizes = {gid: _fmt_size(b) for gid, b in sizes_b.items()}
    today_iso = today.isoformat()
    # Library roll-up for the summary strip (display-only).
    totals = {
        "n": len(gs),
        "published": sum(1 for g in gs if g["published"]),
        "draft": sum(1 for g in gs if not g["published"]),
        "expired": sum(1 for g in gs if g["expires_at"] and g["expires_at"] < today_iso),
        "assets": sum(g["n_assets"] for g in gs),
        "fav": sum(g["n_fav"] for g in gs),
        "dl": sum(g["n_dl"] for g in gs),
        "size": _fmt_size(sum(sizes_b.values())),
    }
    soon_iso = (today + dt.timedelta(days=7)).isoformat()
    cards = [_gallery_card(g, today_iso, soon_iso) for g in gs]
    card_counts = {"all": len(cards)}
    for k in ("delivered", "proofing", "expiring", "draft"):
        card_counts[k] = sum(1 for c in cards if c["status_lc"] == k)
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    backup_dir = config.DATA_DIR / "backups"
    snaps = sorted(backup_dir.glob("*.db.gz")) if backup_dir.exists() else []
    backup_age_h = ((dt.datetime.now().timestamp() - snaps[-1].stat().st_mtime) / 3600
                    if snaps else None)
    return templates.TemplateResponse(request, "admin/dashboard.html",
                                      {"galleries": gs, "cards": cards,
                                       "card_counts": card_counts,
                                       "base_url": config.BASE_URL,
                                       "today": today.isoformat(),
                                       "soon": soon_iso,
                                       "failed_jobs": failed_jobs, "sizes": sizes,
                                       "free_gb": free_gb,
                                       "min_free_gb": config.MIN_FREE_GB,
                                       "backup_age_h": backup_age_h,
                                       "n_unlinked_pub": n_unlinked_pub,
                                       "orphans": orphans,
                                       "link_clients": link_clients,
                                       "sizes_b": sizes_b,
                                       "totals": totals})


@router.post("/galleries")
async def create_gallery(title: str = Form(...), client_name: str = Form("")):
    gid = db.run("INSERT INTO galleries (slug, title, client_name, pin) VALUES (?,?,?,?)",
                 (security.new_slug(), title.strip(), client_name.strip() or None,
                  security.new_pin()))
    for i, name in enumerate(DEFAULT_SECTIONS):
        db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
               (gid, name, i))
    log.info("gallery %s created", gid)
    return RedirectResponse(f"/admin/galleries/{gid}", status_code=303)


# ── Transfers ──────────────────────────────────────────────────────────────
# WeTransfer-style sends: galleries WHERE type='drop'. They reuse the gallery
# manage page (gallery.html, which hides its gallery-only chrome for drops) and
# the whole upload/derivative/ZIP/download stack; only the list + create live
# here. Created published=1 so the link works the moment files finish, with a
# PIN stored but enforced only when require_pin=1.

# Strict 1:1 with the Admin Transfers prototype: one derived status per card
# (Active / Expiring / Downloaded / Expired) with its own icon + colour pair,
# all honest projections of real columns (expiry, download count, asset count).
_TRANSFER_STYLE = {
    "Active":     ("#2f7d57", "#e1f2e9", "↑"),
    "Expiring":   ("#9a7a2c", "#f7ecd2", "↑"),
    "Downloaded": ("#2f6d8a", "#ddeef0", "✓"),
    "Expired":    ("#8A9183", "#ecefe6", "⤓"),
}


def _transfer_card(g, size: str, today_iso: str, soon_iso: str) -> dict:
    exp = g["expires_at"]
    expired = bool(exp and exp < today_iso)
    expiring = bool(exp and not expired and exp <= soon_iso)
    if expired:
        status = "Expired"
    elif expiring:
        status = "Expiring"
    elif g["n_dl"]:
        status = "Downloaded"
    else:
        status = "Active"
    color, bg, icon = _TRANSFER_STYLE[status]

    if expired:
        when = f"link expired {_short_date(exp)}"
    elif exp:
        days = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(today_iso)).days
        when = f"expires in {days} day{'s' if days != 1 else ''}"
    else:
        when = "no expiry"
    n = g["n_assets"]
    files = f"{n} file{'s' if n != 1 else ''}"
    if g["n_pending"]:
        files += f" ({g['n_pending']} processing)"

    return {"id": g["id"], "slug": g["slug"], "title": g["title"],
            "require_pin": g["require_pin"], "pin": g["pin"],
            "status": status, "status_lc": status.lower(),
            "status_color": color, "status_bg": bg, "icon": icon,
            "meta": f"{files} · {when}",
            "size": size, "n_assets": n,
            "downloads": f"{g['n_dl']} download{'s' if g['n_dl'] != 1 else ''}"}


@router.get("/transfers", response_class=HTMLResponse)
async def transfers(request: Request):
    ds = db.all_("""SELECT g.*,
                    (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id) AS n_assets,
                    (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id
                      AND a.status='pending') AS n_pending,
                    (SELECT COUNT(*) FROM downloads d WHERE d.gallery_id=g.id) AS n_dl
                    FROM galleries g WHERE g.type='drop'
                    ORDER BY g.created_at DESC""")
    today = dt.date.today()
    today_iso = today.isoformat()
    soon_iso = (today + dt.timedelta(days=3)).isoformat()
    sizes_b = {g["id"]: _dir_size(config.MEDIA_DIR / str(g["id"])) for g in ds}
    cards = [_transfer_card(g, _fmt_size(sizes_b[g["id"]]), today_iso, soon_iso)
             for g in ds]
    month_start = today.replace(day=1).isoformat()
    dl_month = db.one("""SELECT COUNT(*) AS n FROM downloads d
                         JOIN galleries g ON g.id=d.gallery_id
                         WHERE g.type='drop' AND d.created_at >= ?""",
                      (month_start,))["n"]
    totals = {
        "n": len(ds),
        "active": sum(1 for c in cards if c["status"] in ("Active", "Downloaded")),
        "live": sum(1 for g in ds
                    if not (g["expires_at"] and g["expires_at"] < today_iso)),
        "expired": sum(1 for c in cards if c["status"] == "Expired"),
        "stored": _fmt_size(sum(sizes_b.values())),
        "dl_month": dl_month,
    }
    return templates.TemplateResponse(request, "admin/transfers.html",
                                      {"transfers": cards, "base_url": config.BASE_URL,
                                       "totals": totals})


@router.post("/transfers")
async def create_transfer(title: str = Form(...), expires_days: str = Form(""),
                          require_pin: bool = Form(False)):
    expires_at = None
    raw = (expires_days or "").strip()
    if raw and raw != "0":
        try:
            expires_at = (dt.date.today() + dt.timedelta(days=int(raw))).isoformat()
        except ValueError:
            raise HTTPException(status_code=400, detail="bad expiry")
    gid = db.run("""INSERT INTO galleries (slug, title, pin, type, require_pin,
                    published, expires_at) VALUES (?,?,?,'drop',?,1,?)""",
                 (security.new_slug(), title.strip(), security.new_pin(),
                  1 if require_pin else 0, expires_at))
    log.info("transfer %s created", gid)
    return RedirectResponse(f"/admin/galleries/{gid}", status_code=303)


@router.get("/galleries/{gallery_id}", response_class=HTMLResponse)
async def gallery_detail(request: Request, gallery_id: int):
    g = get_gallery(gallery_id)
    sections = db.all_("SELECT * FROM sections WHERE gallery_id=? ORDER BY position",
                       (gallery_id,))
    # unique-asset selection counts per section (proofing progress signal for admin)
    section_picks = {r["section_id"]: r["n"] for r in db.all_(
        """SELECT a.section_id, COUNT(DISTINCT f.asset_id) AS n
           FROM favorites f JOIN assets a ON a.id=f.asset_id
           WHERE a.gallery_id=? GROUP BY a.section_id""", (gallery_id,))}
    assets = db.all_("""SELECT a.*,
                        (SELECT COUNT(*) FROM favorites f WHERE f.asset_id=a.id) AS n_fav
                        FROM assets a WHERE a.gallery_id=?
                        ORDER BY a.section_id, a.position, a.id""", (gallery_id,))
    clients = db.all_("SELECT id, name, company FROM clients ORDER BY name")
    projects = db.all_("""SELECT p.id, p.title, c.name AS client_name FROM projects p
                          JOIN clients c ON c.id=p.client_id ORDER BY p.created_at DESC""")
    client = (db.one("SELECT name, email FROM clients WHERE id=?", (g["client_id"],))
              if g["client_id"] else None)
    portal_favs = _portal_fav_count(gallery_id, g["client_id"])
    # Honest delivery stats for the gallery header tiles — real rows only.
    n_views = db.one("SELECT COUNT(*) AS n FROM visitors WHERE gallery_id=?",
                     (gallery_id,))["n"]
    n_downloads = db.one("SELECT COUNT(*) AS n FROM downloads WHERE gallery_id=?",
                         (gallery_id,))["n"]
    # Visible review-comment threads per video asset (flat, nested in the template).
    video_comments = {
        a["id"]: db.all_("""SELECT id, parent_id, timecode, body, author_role, status, created_at
                            FROM video_comments WHERE asset_id=? AND deleted_at IS NULL
                            ORDER BY timecode, created_at, id""", (a["id"],))
        for a in assets if a["kind"] == "video"}
    return templates.TemplateResponse(request, "admin/gallery.html",
                                      {"g": g, "sections": sections, "assets": assets,
                                       "section_picks": section_picks,
                                       "clients": clients, "projects": projects,
                                       "client": client, "base_url": config.BASE_URL,
                                       "tag_suggestions": PORTFOLIO_TAG_SUGGESTIONS,
                                       "portal_favs": portal_favs,
                                       "n_views": n_views, "n_downloads": n_downloads,
                                       "video_comments": video_comments})


@router.post("/galleries/{gallery_id}/link-client")
async def link_client(gallery_id: int, client_id: int = Form(...)):
    """Quick re-link from the /admin dashboard orphan picker. Validates the
    client exists, then UPDATE galleries SET client_id=?. Re-link only; does
    NOT change anything else about the gallery (use the full settings form for
    project/captions/etc)."""
    get_gallery(gallery_id)
    if not db.one("SELECT 1 AS x FROM clients WHERE id=?", (client_id,)):
        raise HTTPException(status_code=400, detail="client does not exist")
    db.run("UPDATE galleries SET client_id=? WHERE id=?", (client_id, gallery_id))
    log.info("gallery %s re-linked to client %s", gallery_id, client_id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/galleries/{gallery_id}/settings")
async def update_gallery(gallery_id: int, title: str = Form(...),
                         client_name: str = Form(""), pin: str = Form(...),
                         expires_at: str = Form(""), published: bool = Form(False),
                         client_id: int | None = Form(None),
                         project_id: int | None = Form(None),
                         captions: str = Form(""),
                         cs_published: bool = Form(False),
                         cs_tagline: str = Form(""), cs_brief: str = Form(""),
                         cs_credits: str = Form(""), cs_location: str = Form("")):
    old = get_gallery(gallery_id)
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(status_code=400, detail="PIN must be 4 digits")
    new_expires = expires_at.strip() or None
    db.run("""UPDATE galleries SET title=?, client_name=?, pin=?, expires_at=?,
              published=?, client_id=?, project_id=?, captions=?,
              cs_published=?, cs_tagline=?, cs_brief=?, cs_credits=?, cs_location=?
              WHERE id=?""",
           (title.strip(), client_name.strip() or None, pin,
            new_expires, 1 if published else 0,
            client_id, project_id,
            captions.strip() or None,
            1 if cs_published else 0,
            cs_tagline.strip() or None, cs_brief.strip() or None,
            cs_credits.strip() or None, cs_location.strip() or None,
            gallery_id))
    # Changing the expiry date re-arms the one-shot expiry reminder so an extended
    # gallery re-reminds near its new date (the flag would otherwise stay set).
    if new_expires != old["expires_at"]:
        db.run("UPDATE galleries SET reminded_expiry=0 WHERE id=?", (gallery_id,))
    if published and project_id and (not old["published"] or old["project_id"] != project_id):
        jobs.enqueue("notion_sync_gallery", {"gallery_id": gallery_id})
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


# Project statuses that pre-date 'delivered' on the studio pipeline. Sending
# the "Final edits delivered" email auto-advances any of these → 'project_closed';
# already-'project_closed' or 'archived' states are left alone (idempotent).
_PRE_DELIVERED_STATUSES = ("inquiry_received", "consultation_call", "proposal_sent",
                           "contract_signed", "retainer_paid", "session_planning")


@router.post("/galleries/{gallery_id}/email")
async def email_gallery(gallery_id: int, to: str = Form(...),
                        subject: str = Form(...), message: str = Form(...),
                        email_kind: str = Form("delivery")):
    g = get_gallery(gallery_id)
    if not g["published"]:
        raise HTTPException(status_code=400,
                            detail="publish the gallery first — the link 404s until then")
    if not mailer.configured():
        raise HTTPException(status_code=503, detail="email is not configured")
    to, subject = to.strip(), subject.strip()
    if not to or not subject:
        raise HTTPException(status_code=400, detail="to and subject required")
    try:
        mailer.send(to, subject, message)
    except Exception:
        log.exception("delivery email failed for gallery %s", gallery_id)
        raise HTTPException(status_code=502, detail="SMTP send failed — check logs")
    db.run("""INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject)
              VALUES (?,?,?,?,?)""",
           (g["project_id"], "other", gallery_id, to, subject))
    log.info("delivery email sent for gallery %s (kind=%s)", gallery_id, email_kind)
    # Auto-advance the linked project on the final hand-off email.
    if email_kind == "final" and g["project_id"]:
        p = db.one("SELECT status FROM projects WHERE id=?", (g["project_id"],))
        if p and p["status"] in _PRE_DELIVERED_STATUSES:
            db.run("UPDATE projects SET status='project_closed', "
                   "stage_changed_at=datetime('now') WHERE id=?",
                   (g["project_id"],))
            log.info("project %s auto-advanced to 'project_closed' via final email "
                     "(was %s)", g["project_id"], p["status"])
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.get("/thumb/{gallery_id}/{asset_id}")
async def admin_thumb(gallery_id: int, asset_id: int):
    a = db.one("SELECT stored FROM assets WHERE id=? AND gallery_id=?",
               (asset_id, gallery_id))
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(gallery_id) / "thumb" / f"{Path(a['stored']).stem}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/galleries/{gallery_id}/sections")
async def add_section(gallery_id: int, name: str = Form(...)):
    get_gallery(gallery_id)
    row = db.one("SELECT COALESCE(MAX(position),-1)+1 AS p FROM sections WHERE gallery_id=?",
                 (gallery_id,))
    db.run("INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
           (gallery_id, name.strip(), row["p"]))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


def get_section(gallery_id: int, section_id: int) -> "db.sqlite3.Row":
    s = db.one("SELECT * FROM sections WHERE id=? AND gallery_id=?",
               (section_id, gallery_id))
    if not s:
        raise HTTPException(status_code=404)
    return s


@router.post("/galleries/{gallery_id}/sections/{section_id}/rename")
async def rename_section(gallery_id: int, section_id: int, name: str = Form(...)):
    get_section(gallery_id, section_id)
    if not name.strip():
        raise HTTPException(status_code=400, detail="name required")
    db.run("UPDATE sections SET name=? WHERE id=?", (name.strip(), section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/proof")
async def set_section_proof(gallery_id: int, section_id: int,
                            proof_target: str = Form("")):
    """Set or clear the proofing target on a section.
    Empty/0/non-numeric → clear (section becomes free-form again)."""
    get_section(gallery_id, section_id)
    raw = proof_target.strip()
    target = None
    if raw:
        try:
            n = int(raw)
            target = n if n > 0 else None
        except ValueError:
            raise HTTPException(status_code=400, detail="proof target must be a number")
    db.run("UPDATE sections SET proof_target=? WHERE id=?", (target, section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/caption")
async def set_section_caption(gallery_id: int, section_id: int,
                              caption: str = Form("")):
    """Set or clear the public-facing caption shown under the section heading."""
    get_section(gallery_id, section_id)
    db.run("UPDATE sections SET caption=? WHERE id=?",
           (caption.strip() or None, section_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/move")
async def reorder_section(gallery_id: int, section_id: int, dir: str = Form(...)):
    if dir not in ("up", "down"):
        raise HTTPException(status_code=400, detail="dir must be up or down")
    get_section(gallery_id, section_id)
    ids = [r["id"] for r in db.all_(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id",
        (gallery_id,))]
    i = ids.index(section_id)
    j = i - 1 if dir == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, sid in enumerate(ids):
            db.run("UPDATE sections SET position=? WHERE id=?", (pos, sid))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/sections/{section_id}/delete")
async def delete_section(gallery_id: int, section_id: int):
    db.run("DELETE FROM sections WHERE id=? AND gallery_id=?", (section_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/section")
async def move_asset(gallery_id: int, asset_id: int, section_id: int | None = Form(None)):
    db.run("UPDATE assets SET section_id=? WHERE id=? AND gallery_id=?",
           (section_id, asset_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/bulk-section")
async def bulk_move_assets(request: Request, gallery_id: int):
    get_gallery(gallery_id)
    form = await request.form()
    raw = form.get("section_id") or ""
    section_id = int(raw) if raw else None
    if section_id and not db.one("SELECT id FROM sections WHERE id=? AND gallery_id=?",
                                 (section_id, gallery_id)):
        raise HTTPException(status_code=400, detail="unknown section")
    for v in form.getlist("asset_ids"):
        db.run("UPDATE assets SET section_id=? WHERE id=? AND gallery_id=?",
               (section_id, int(v), gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/move")
async def reorder_asset(gallery_id: int, asset_id: int, dir: str = Form(...)):
    if dir not in ("left", "right"):
        raise HTTPException(status_code=400, detail="dir must be left or right")
    a = db.one("SELECT section_id FROM assets WHERE id=? AND gallery_id=?",
               (asset_id, gallery_id))
    if not a:
        raise HTTPException(status_code=404)
    siblings = db.all_("""SELECT id FROM assets WHERE gallery_id=? AND section_id IS ?
                          ORDER BY position, id""", (gallery_id, a["section_id"]))
    ids = [s["id"] for s in siblings]
    i = ids.index(asset_id)
    j = i - 1 if dir == "left" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        # renumber the whole section — also normalizes legacy all-zero positions
        for pos, aid in enumerate(ids):
            db.run("UPDATE assets SET position=? WHERE id=?", (pos, aid))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/cover")
async def set_cover(gallery_id: int, asset_id: int):
    g = get_gallery(gallery_id)
    a = db.one("SELECT id FROM assets WHERE id=? AND gallery_id=? AND kind='photo'",
               (asset_id, gallery_id))
    if not a:
        raise HTTPException(status_code=404)
    new = None if g["cover_asset_id"] == asset_id else asset_id
    db.run("UPDATE galleries SET cover_asset_id=? WHERE id=?", (new, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/portfolio")
async def toggle_portfolio(gallery_id: int, asset_id: int):
    db.run("UPDATE assets SET portfolio = 1 - portfolio WHERE id=? AND gallery_id=? "
           "AND kind='photo'", (asset_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/tag")
async def set_portfolio_tag(gallery_id: int, asset_id: int,
                            portfolio_tag: str = Form("")):
    db.run("UPDATE assets SET portfolio_tag=? WHERE id=? AND gallery_id=? "
           "AND kind='photo'",
           (portfolio_tag.strip() or None, asset_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


def _portal_fav_count(gallery_id: int, client_id: int | None) -> int:
    """How many favorites against this gallery's assets exist for a client
    that has a portal? When >0, deleting the gallery silently breaks the
    client's social-crops view on their portal."""
    if not client_id:
        return 0
    if not db.one("SELECT 1 AS x FROM portals WHERE client_id=?", (client_id,)):
        return 0
    return db.one("""SELECT COUNT(DISTINCT f.asset_id) AS n
                     FROM favorites f JOIN assets a ON a.id=f.asset_id
                     WHERE a.gallery_id=?""", (gallery_id,))["n"]


@router.post("/galleries/{gallery_id}/delete")
async def delete_gallery(gallery_id: int, force: bool = Form(False)):
    g = get_gallery(gallery_id)
    if g["published"] and g["type"] != "drop":
        raise HTTPException(status_code=400,
                            detail="unpublish first — deleting a live client link "
                                   "is a two-step on purpose")
    # Portal-favorites safety: deleting cascades through favorites, which the
    # client's portal turned into social crops. Refuse unless `force` is set
    # so a misclick can't silently break their portal.
    if not force:
        n_pf = _portal_fav_count(gallery_id, g["client_id"])
        if n_pf:
            raise HTTPException(
                status_code=400,
                detail=f"{n_pf} portal favorite{'s' if n_pf != 1 else ''} would be "
                       f"lost — the client's social-crops view depends on them. "
                       f"Re-submit with force=1 if you really mean it.")
    db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))  # FKs cascade the rest
    db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (gallery_id,))
    shutil.rmtree(config.MEDIA_DIR / str(gallery_id), ignore_errors=True)
    for z in config.ZIP_DIR.glob(f"g{gallery_id}-r*.zip"):
        z.unlink(missing_ok=True)
    for z in config.ZIP_DIR.glob(f"g{gallery_id}-v*.zip"):
        z.unlink(missing_ok=True)
    for z in config.ZIP_DIR.glob(f"g{gallery_id}-s*.zip"):
        z.unlink(missing_ok=True)
    log.info("gallery %s deleted", gallery_id)
    dest = "/admin/transfers" if g["type"] == "drop" else "/admin"
    return RedirectResponse(dest, status_code=303)


@router.post("/galleries/{gallery_id}/assets/{asset_id}/delete")
async def delete_asset(gallery_id: int, asset_id: int):
    a = db.one("SELECT * FROM assets WHERE id=? AND gallery_id=?", (asset_id, gallery_id))
    if a:
        base = config.MEDIA_DIR / str(gallery_id)
        stem = a["stored"].rsplit(".", 1)[0]
        for sub in ("original", "web", "thumb", "crops"):
            for f in (base / sub).glob(f"{stem}*"):
                f.unlink(missing_ok=True)
        db.run("DELETE FROM assets WHERE id=?", (asset_id,))
        db.run("UPDATE galleries SET content_rev=content_rev+1, "
               "cover_asset_id=CASE WHEN cover_asset_id=? THEN NULL ELSE cover_asset_id END "
               "WHERE id=?", (asset_id, gallery_id))
    return RedirectResponse(f"/admin/galleries/{gallery_id}", status_code=303)


# ── Video review comments (Domain C slice 3) ─────────────────────────────────

@router.post("/galleries/{gallery_id}/comments/{asset_id}")
async def admin_add_comment(gallery_id: int, asset_id: int, body: str = Form(...),
                            timecode: float = Form(0.0), parent_id: str = Form("")):
    """Studio-side author path. Admin comments are author_role='admin',
    visitor_id NULL; a reply inherits its parent's timecode."""
    get_gallery(gallery_id)
    a = db.one("SELECT id FROM assets WHERE id=? AND gallery_id=? AND kind='video'",
               (asset_id, gallery_id))
    if not a:
        raise HTTPException(status_code=404)
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="comment body required")
    parent, inherited = resolve_comment_parent(asset_id, parent_id)
    tc = inherited if parent is not None else max(0.0, timecode)
    db.run("""INSERT INTO video_comments
              (asset_id, gallery_id, parent_id, visitor_id, author_role, timecode, body)
              VALUES (?,?,?,NULL,'admin',?,?)""",
           (asset_id, gallery_id, parent, tc, body))
    return RedirectResponse(f"/admin/galleries/{gallery_id}#asset-{asset_id}",
                            status_code=303)


@router.post("/comments/{comment_id}/hide")
async def admin_hide_comment(comment_id: int):
    """Moderation: soft-delete a comment AND its descendant replies in one
    recursive UPDATE (so no reply dangles under a hidden parent). This is the
    one auditable human act in this slice — logged to audit_log."""
    c = db.one("SELECT id, asset_id, gallery_id, author_role FROM video_comments "
               "WHERE id=?", (comment_id,))
    if not c:
        raise HTTPException(status_code=404)
    with db.tx() as con:
        con.execute(
            """WITH RECURSIVE sub(id) AS (
                 SELECT id FROM video_comments WHERE id=?
                 UNION ALL
                 SELECT vc.id FROM video_comments vc JOIN sub ON vc.parent_id=sub.id)
               UPDATE video_comments SET deleted_at=datetime('now')
               WHERE id IN (SELECT id FROM sub) AND deleted_at IS NULL""",
            (comment_id,))
        audit.log(con, "video_comment", comment_id, "hide",
                  diff={"asset_id": c["asset_id"], "author_role": c["author_role"]})
    return RedirectResponse(f"/admin/galleries/{c['gallery_id']}#asset-{c['asset_id']}",
                            status_code=303)


# ── Review state machine (Domain C slice 4) ──────────────────────────────────
# Two states (open ⇄ resolved), admin-only, both audited. A transition targets a
# thread ROOT (parent_id IS NULL) and cascades status to the whole subtree via the
# same recursive CTE hide uses — replies never carry independent state. hide and
# status are orthogonal: a hidden comment is out of the workflow (404 here).

def _transition_comment(comment_id: int, *, want: str, to: str):
    """Validate + apply one open⇄resolved transition on a thread root, cascading
    to descendants, with a single audit row. `want` = the status the root must
    currently be in for this transition to be legal; `to` = the new status."""
    c = db.one("SELECT id, asset_id, gallery_id, parent_id, status, deleted_at "
               "FROM video_comments WHERE id=?", (comment_id,))
    if not c or c["deleted_at"]:           # missing or hidden = not in the workflow
        raise HTTPException(status_code=404)
    if c["parent_id"] is not None:
        raise HTTPException(status_code=400, detail="resolve operates on a thread root")
    if (c["status"] or "open") != want:    # illegal transition (already in target state)
        raise HTTPException(status_code=409, detail=f"comment is not {want}")
    with db.tx() as con:
        _cascade_status(con, comment_id, to)
        audit.log(con, "video_comment", comment_id, to,
                  diff={"asset_id": c["asset_id"], "from": want, "to": to})
    return RedirectResponse(f"/admin/galleries/{c['gallery_id']}#asset-{c['asset_id']}",
                            status_code=303)


@router.post("/comments/{comment_id}/resolve")
async def admin_resolve_comment(comment_id: int):
    return _transition_comment(comment_id, want="open", to="resolved")


@router.post("/comments/{comment_id}/reopen")
async def admin_reopen_comment(comment_id: int):
    return _transition_comment(comment_id, want="resolved", to="open")
