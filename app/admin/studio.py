"""Studio — clients & projects (the CRM spine; proposals/contracts/invoices hang off projects)."""

import datetime as dt
import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import clients, config, db, jobs, platekit, pricing, security, specialties, usage_vocab
from ..render import templates
from . import common, studio_context
from .lookups import PROJECT_STATUSES, get_client, get_project
from .studio_context import _studio_context

# Re-exported for tests that import the helper from this module.
_spark_series = studio_context._spark_series


def _today() -> dt.date:
    """Single source for the studio's wall-clock 'today' (localtime, the canonical
    studio clock). Financial date boundaries build their comparison from this and
    pass it as a bound param, so SQLite never derives its own UTC 'now' for a
    judgement that must follow the operator's wall clock. Monkeypatchable so the
    overdue financial boundary can be pinned deterministically in tests."""
    return dt.date.today()


log = logging.getLogger("mise.admin.studio")
router = APIRouter(prefix="/admin/studio", dependencies=[Depends(security.require_admin)])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")
BRAND_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".eps", ".ai", ".zip"}
# Brand-KIT logos are composited server-side onto crops, so only raster formats
# Pillow can open + alpha-composite are allowed (PNG/WebP carry transparency;
# vector EPS/PDF/AI and archives can't be pasted onto a JPEG).
KIT_EXTS = {".png", ".webp", ".jpg", ".jpeg"}
KIT_POSITIONS = {"tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"}


@router.get("/playbook", response_class=HTMLResponse)
async def studio_playbook(request: Request):
    return templates.TemplateResponse(request, "admin/studio_playbook.html", {})


@router.get("/clients", response_class=HTMLResponse)
async def studio_clients(request: Request):
    clients, client_portal_hints = common._clients_with_hints()
    return templates.TemplateResponse(
        request,
        "admin/studio_clients.html",
        {"clients": clients, "client_portal_hints": client_portal_hints},
    )


@router.get("", response_class=HTMLResponse)
async def studio_home(request: Request):
    return templates.TemplateResponse(request, "admin/studio.html", _studio_context(request))


@router.get("/activity", response_class=HTMLResponse)
async def studio_activity(request: Request):
    return templates.TemplateResponse(
        request, "admin/studio_activity.html", _studio_context(request)
    )


@router.post("/clients")
async def create_client(
    name: str = Form(...), company: str = Form(""), email: str = Form(""), phone: str = Form("")
):
    cid = db.run(
        "INSERT INTO clients (name, company, email, phone) VALUES (?,?,?,?)",
        (name.strip(), company.strip() or None, email.strip() or None, phone.strip() or None),
    )
    log.info("client %s created", cid)
    return RedirectResponse(f"/admin/studio/clients/{cid}", status_code=303)


def _redirect(return_to: str, default: str) -> RedirectResponse:
    """Honor a caller-supplied return_to (e.g. the Inbox passes its own URL so a
    triage action keeps Kevin in place) when it's a safe local admin path;
    otherwise fall back to the action's own destination. Same-origin only —
    rejects anything that isn't a plain /admin/… path."""
    safe = return_to.startswith("/admin/") and "//" not in return_to[1:]
    return RedirectResponse(return_to if safe else default, status_code=303)


@router.post("/inquiries/{inquiry_id}/unconvert")
async def inquiry_unconvert(inquiry_id: int, return_to: str = Form("")):
    """Clear the conversion stamps on an inquiry so it shows up as actionable
    again. INTENTIONALLY does NOT delete the spawned client/project — by the
    time Kevin clicks undo, those may already carry edits, brand assets, or
    proposals. This is a misclick fix, not a cascade delete."""
    inq = db.one("SELECT id FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    db.run(
        """UPDATE inquiries SET converted_at=NULL,
              converted_client_id=NULL, converted_project_id=NULL
              WHERE id=?""",
        (inquiry_id,),
    )
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})
    log.info("inquiry %s unconverted (spawned client/project untouched)", inquiry_id)
    return _redirect(return_to, "/admin/studio")


@router.post("/inquiries/{inquiry_id}/dismiss")
async def inquiry_dismiss(inquiry_id: int, return_to: str = Form("")):
    """Archive an unconverted inquiry — spam, test, or dead leads. Reversible:
    the row is kept and stamped dismissed_at, so it drops out of the active
    leads list and the home 'new inquiries' count but stays in the Inquiries
    table with an undo. Refuses once converted (that anchors a real
    client/project history)."""
    inq = db.one("SELECT id, converted_at FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    if inq["converted_at"]:
        raise HTTPException(status_code=400, detail="converted inquiries cannot be dismissed")
    db.run("UPDATE inquiries SET dismissed_at=datetime('now') WHERE id=?", (inquiry_id,))
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})
    log.info("inquiry %s dismissed (archived)", inquiry_id)
    return _redirect(return_to, "/admin/studio")


@router.post("/inquiries/{inquiry_id}/undismiss")
async def inquiry_undismiss(inquiry_id: int, return_to: str = Form("")):
    """Undo a dismiss — clears dismissed_at so the lead returns to the active
    pipeline."""
    inq = db.one("SELECT id FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    db.run("UPDATE inquiries SET dismissed_at=NULL WHERE id=?", (inquiry_id,))
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})
    log.info("inquiry %s undismissed (restored)", inquiry_id)
    return _redirect(return_to, "/admin/studio")


def _inquiry_and_client(inquiry_id: int) -> "tuple[db.sqlite3.Row, int]":
    """Load an inquiry (404 if gone) and find-or-create its client by email.
    Shared verbatim by the inquiry→client and inquiry→quote convert routes —
    one copy so the client-seeding fields can't drift between them."""
    inq = db.one("SELECT * FROM inquiries WHERE id=?", (inquiry_id,))
    if not inq:
        raise HTTPException(status_code=404)
    existing = db.one("SELECT id FROM clients WHERE email=?", (inq["email"],))
    if existing:
        return inq, existing["id"]
    cid = db.run(
        "INSERT INTO clients (name, company, email, notes) VALUES (?,?,?,?)",
        (
            inq["name"],
            inq["business"],
            inq["email"],
            f"From inquiry {inq['created_at'][:10]}:\n{inq['message']}",
        ),
    )
    log.info("client %s created from inquiry %s", cid, inquiry_id)
    return inq, cid


@router.post("/inquiries/{inquiry_id}/client")
async def inquiry_to_client(inquiry_id: int, return_to: str = Form("")):
    inq, cid = _inquiry_and_client(inquiry_id)
    pid = None
    # Bookings carry a date + service → lift straight into an 'inquiry_received' project so
    # Kevin can spawn a proposal without re-typing the date.
    if inq["kind"] == "booking" and inq["shoot_date"]:
        title = f"{inq['service'] or 'Shoot'} — {inq['shoot_date']}"
        pid = db.run(
            """INSERT INTO projects (client_id, title, shoot_date)
                        VALUES (?,?,?)""",
            (cid, title, inq["shoot_date"]),
        )
        log.info("project %s spawned from booking %s", pid, inquiry_id)
    # Stamp the inquiry as converted so the studio list can fade it out.
    db.run(
        """UPDATE inquiries SET converted_at=datetime('now'),
              converted_client_id=?, converted_project_id=? WHERE id=?""",
        (cid, pid, inquiry_id),
    )
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})
    if pid:
        return _redirect(return_to, f"/admin/studio/projects/{pid}")
    return _redirect(return_to, f"/admin/studio/clients/{cid}")


@router.post("/inquiries/{inquiry_id}/quote")
async def inquiry_to_quote(inquiry_id: int, return_to: str = Form("")):
    """One click from a lead to an editable draft quote: find/create the client,
    spawn an 'inquiry_received' project, and open a blank draft proposal seeded
    with the inquiry brief as the intro. Quoting-first flow — Kevin fills the
    line items (no auto-pricing; the catalog floor numbers live in proposals
    PRESETS and are applied by hand per client)."""
    inq, cid = _inquiry_and_client(inquiry_id)
    title = f"{inq['service'] or 'Shoot'}"
    if inq["shoot_date"]:
        title += f" — {inq['shoot_date']}"
    pid = db.run(
        "INSERT INTO projects (client_id, title, shoot_date) VALUES (?,?,?)",
        (cid, title, inq["shoot_date"]),
    )
    intro = f"Quote prepared from inquiry received {inq['created_at'][:10]}.\n\n{inq['message']}"
    prop_id = db.run(
        """INSERT INTO proposals (project_id, slug, title, intro)
                        VALUES (?,?,?,?)""",
        (pid, security.new_slug(), f"Quote — {title}", intro),
    )
    db.run(
        """UPDATE inquiries SET converted_at=datetime('now'),
              converted_client_id=?, converted_project_id=? WHERE id=?""",
        (cid, pid, inquiry_id),
    )
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": inquiry_id})
    log.info("inquiry %s → project %s + draft proposal %s", inquiry_id, pid, prop_id)
    return _redirect(return_to, f"/admin/studio/proposals/{prop_id}")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: int):
    c = get_client(client_id)
    projects = db.all_(
        "SELECT * FROM projects WHERE client_id=? ORDER BY created_at DESC", (client_id,)
    )
    portal = db.one("SELECT * FROM portals WHERE client_id=?", (client_id,))
    galleries = db.all_(
        """SELECT g.id, g.title, g.published, g.slug,
                  (SELECT COUNT(*) FROM assets a WHERE a.gallery_id=g.id
                     AND a.status='ready') AS n_assets,
                  (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                     JOIN assets a ON a.id=f.asset_id
                     WHERE a.gallery_id=g.id) AS n_favs
           FROM galleries g WHERE g.client_id=? ORDER BY g.created_at DESC""",
        (client_id,),
    )
    brand = db.all_(
        "SELECT * FROM brand_assets WHERE client_id=? ORDER BY created_at DESC", (client_id,)
    )
    brand_kits = db.all_(
        "SELECT * FROM brand_kits WHERE client_id=? ORDER BY id DESC", (client_id,)
    )
    parent = (
        db.one("SELECT id, name FROM clients WHERE id=?", (c["parent_id"],))
        if c["parent_id"]
        else None
    )
    # Candidate parents: every other client except this one's descendants
    # (picking a descendant would be a cycle — the route rejects it anyway).
    descendants = clients.descendant_ids(client_id)
    blocked = {client_id, *descendants}
    parent_choices = [
        r for r in db.all_("SELECT id, name FROM clients ORDER BY name") if r["id"] not in blocked
    ]
    # Read-only roster of the venues/regions under this client (group->venue
    # direction; the parent selector above covers venue->group). Top-down order
    # preserved from the descendant_ids helper.
    children = []
    if descendants:
        ph = ",".join("?" * len(descendants))
        rows = {
            r["id"]: r
            for r in db.all_(
                f"SELECT id, name, company FROM clients WHERE id IN ({ph})", descendants
            )
        }
        children = [rows[i] for i in descendants if i in rows]
    licenses = db.all_(
        """SELECT id, title, usage_tier, exclusivity, status, published, fee_cents,
                  starts_on, ends_on, perpetual,
                  CAST(julianday(ends_on) - julianday(date('now', 'localtime')) AS INTEGER) AS days_left
           FROM licenses WHERE holder_client_id=? AND deleted_at IS NULL
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                    ends_on IS NULL, ends_on""",
        (client_id,),
    )
    # Reverse lookup: licenses that reach this client WITHOUT it holding them — a
    # group (holder_and_descendants) grant on an ancestor, or an explicit
    # 'specific' grant elsewhere that lists this client. Local import: licenses
    # imports studio.get_client at module load, so a top-level import here cycles.
    from . import licenses as licenses_mod

    covering = licenses_mod.licenses_covering(client_id)
    # Portal audit aggregates — totals across published galleries the client
    # can actually see + the on-disk crop cache (the social-crop ZIP source).
    totals = {
        "assets": sum(g["n_assets"] for g in galleries),
        "favs": sum(g["n_favs"] for g in galleries),
        "galleries_published": sum(1 for g in galleries if g["published"]),
        "brand_bytes": sum(b["bytes"] for b in brand),
    }
    crop_bytes = 0
    for g in galleries:
        crops_dir = config.MEDIA_DIR / str(g["id"]) / "crops"
        if crops_dir.exists():
            crop_bytes += sum(f.stat().st_size for f in crops_dir.rglob("*") if f.is_file())
    totals["crop_bytes"] = crop_bytes
    # Lifetime money rollup — invoiced (issued only, drafts excluded), paid is
    # the ground truth from actual payment events (R21), shoots delivered counts
    # projects that reached delivery.
    inv = db.one(
        """SELECT COALESCE(SUM(CASE WHEN status != 'draft' THEN total_cents END), 0) AS invoiced_cents,
                  COUNT(CASE WHEN status != 'draft' THEN 1 END) AS n_invoices
           FROM invoices
           WHERE project_id IN (SELECT id FROM projects WHERE client_id=?)""",
        (client_id,),
    )
    paid = db.one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS paid_cents
           FROM payments
           WHERE invoice_id IN (
             SELECT i.id FROM invoices i JOIN projects p ON p.id=i.project_id
             WHERE p.client_id=?)""",
        (client_id,),
    )
    n_delivered = db.one(
        """SELECT COUNT(*) AS n FROM projects
           WHERE client_id=? AND status IN ('project_closed','archived')""",
        (client_id,),
    )["n"]
    money = {
        "invoiced_cents": inv["invoiced_cents"],
        "paid_cents": paid["paid_cents"],
        "outstanding_cents": max(inv["invoiced_cents"] - paid["paid_cents"], 0),
        "n_invoices": inv["n_invoices"],
        "n_delivered": n_delivered,
    }
    platekit_packs = platekit.packs_for_client(c)
    # Cross-session activity feed — the same per-doc events the project page
    # shows, but spanning every session this client has, newest first. Pure-read
    # narration of state already stored (reuses _build_timeline); no new state,
    # so nothing writes to the Notion Activity Log.
    proj_ids = [p["id"] for p in projects]
    timeline = []
    if proj_ids:
        ph = ",".join("?" * len(proj_ids))
        c_proposals = db.all_(f"SELECT * FROM proposals WHERE project_id IN ({ph})", proj_ids)
        c_contracts = db.all_(f"SELECT * FROM contracts WHERE project_id IN ({ph})", proj_ids)
        c_invoices = db.all_(f"SELECT * FROM invoices WHERE project_id IN ({ph})", proj_ids)
        c_emails = db.all_(f"SELECT * FROM emails_log WHERE project_id IN ({ph})", proj_ids)
        c_payments = db.all_(
            "SELECT pm.* FROM payments pm JOIN invoices i ON i.id=pm.invoice_id "
            f"WHERE i.project_id IN ({ph})",
            proj_ids,
        )
        timeline = _build_timeline(c_proposals, c_contracts, c_invoices, c_payments, c_emails)[:40]
    return templates.TemplateResponse(
        request,
        "admin/client.html",
        {
            "c": c,
            "projects": projects,
            "portal": portal,
            "timeline": timeline,
            "galleries": galleries,
            "brand": brand,
            "brand_kits": brand_kits,
            "parent": parent,
            "parent_choices": parent_choices,
            "children": children,
            "licenses": licenses,
            "covering": covering,
            "totals": totals,
            "money": money,
            "platekit": platekit_packs,
            "blockers": _client_blockers(client_id),
            "markets": pricing.MARKETS,
            "base_url": config.BASE_URL,
        },
    )


def _client_blockers(client_id: int) -> list[str]:
    """Reasons NOT to silently delete a client — surface as friendly copy so
    Kevin can choose to force-delete with eyes open."""
    blockers: list[str] = []

    def n(sql, *p):
        return db.one(sql, p)["n"]

    n_kids = n("SELECT COUNT(*) AS n FROM clients WHERE parent_id=?", client_id)
    if n_kids:
        blockers.append(
            f"{n_kids} child client{'s' if n_kids != 1 else ''} (reparent or detach first)"
        )
    n_gal = n("SELECT COUNT(*) AS n FROM galleries WHERE client_id=?", client_id)
    if n_gal:
        blockers.append(f"{n_gal} linked galler{'ies' if n_gal != 1 else 'y'}")
    n_proj = n("SELECT COUNT(*) AS n FROM projects WHERE client_id=?", client_id)
    if n_proj:
        blockers.append(f"{n_proj} project{'s' if n_proj != 1 else ''}")
    n_brand = n("SELECT COUNT(*) AS n FROM brand_assets WHERE client_id=?", client_id)
    if n_brand:
        blockers.append(f"{n_brand} brand asset{'s' if n_brand != 1 else ''}")
    n_lic = n(
        """SELECT COUNT(*) AS n FROM licenses
                 WHERE holder_client_id=? AND deleted_at IS NULL""",
        client_id,
    )
    if n_lic:
        blockers.append(f"{n_lic} license{'s' if n_lic != 1 else ''}")
    portal = db.one("SELECT visits FROM portals WHERE client_id=?", (client_id,))
    if portal and portal["visits"]:
        blockers.append(
            f"portal with {portal['visits']} visit{'s' if portal['visits'] != 1 else ''}"
        )
    n_fav = n(
        """SELECT COUNT(*) AS n FROM favorites f
                 JOIN assets a ON a.id=f.asset_id
                 JOIN galleries g ON g.id=a.gallery_id
                 WHERE g.client_id=?""",
        client_id,
    )
    if n_fav:
        blockers.append(f"{n_fav} favorite{'s' if n_fav != 1 else ''} across their galleries")
    return blockers


@router.post("/clients/{client_id}/delete")
async def delete_client(client_id: int, force: bool = Form(False)):
    get_client(client_id)
    # Children are a HARD blocker: force cannot bypass it, and the DB's
    # ON DELETE RESTRICT would reject the delete anyway. Tree restructuring
    # happens only through the set-parent control, never as a delete side-effect.
    n_kids = db.one("SELECT COUNT(*) AS n FROM clients WHERE parent_id=?", (client_id,))["n"]
    if n_kids:
        raise HTTPException(
            status_code=400,
            detail=f"client still has {n_kids} child client"
            f"{'s' if n_kids != 1 else ''}; reparent or detach "
            "them first (force cannot override this).",
        )
    blockers = _client_blockers(client_id)
    if blockers and not force:
        raise HTTPException(
            status_code=400,
            detail="client still has "
            + ", ".join(blockers)
            + ". Re-submit with force=1 to delete anyway.",
        )
    # galleries.client_id has no ON DELETE clause (defaults to NO ACTION on
    # SQLite), so explicitly unlink before deleting the client. Galleries
    # survive as unowned; brand asset files on disk need explicit rmtree
    # (only the DB rows cascade through the FK).
    db.run("UPDATE galleries SET client_id=NULL WHERE client_id=?", (client_id,))
    brand_dir = config.BRAND_DIR / str(client_id)
    if brand_dir.exists():
        shutil.rmtree(brand_dir, ignore_errors=True)
    db.run("DELETE FROM clients WHERE id=?", (client_id,))
    log.info("client %s deleted (force=%s, blockers=%d)", client_id, force, len(blockers))
    return RedirectResponse("/admin/studio", status_code=303)


@router.post("/clients/{client_id}")
async def update_client(
    client_id: int,
    name: str = Form(...),
    company: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    usage_rights: str = Form(""),
    market: str = Form(pricing.DEFAULT_MARKET),
    platekit_slug: str = Form(""),
):
    get_client(client_id)
    # The market drives which base rate card the license-fee suggestion reads;
    # reject anything outside the live vocabulary rather than store a value that
    # would silently fall back to Asheville at suggest time.
    if market not in pricing.MARKETS:
        raise HTTPException(status_code=400, detail=f"unknown market {market!r}")
    db.run(
        """UPDATE clients SET name=?, company=?, email=?, phone=?, notes=?,
              usage_rights=?, market=?, platekit_slug=? WHERE id=?""",
        (
            name.strip(),
            company.strip() or None,
            email.strip() or None,
            phone.strip() or None,
            notes.strip() or None,
            usage_rights.strip() or None,
            market,
            platekit.normalize_slug(platekit_slug) or None,
            client_id,
        ),
    )
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/parent")
async def set_parent(client_id: int, parent_id: str = Form("")):
    """Set (or clear) a client's parent. Two cycle guards: A->A is rejected
    here (and by the DB CHECK as a backstop); A->B->A is rejected by checking
    the proposed parent against this client's descendants before the UPDATE."""
    get_client(client_id)
    pid = parent_id.strip()
    if not pid:
        db.run("UPDATE clients SET parent_id=NULL WHERE id=?", (client_id,))
        return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)
    new_parent = int(pid)
    if new_parent == client_id:
        raise HTTPException(status_code=422, detail="a client cannot be its own parent")
    if not db.one("SELECT id FROM clients WHERE id=?", (new_parent,)):
        raise HTTPException(status_code=404, detail="parent client not found")
    if new_parent in clients.descendant_ids(client_id):
        raise HTTPException(
            status_code=422,
            detail="that client is below this one in the tree — "
            "setting it as parent would create a cycle",
        )
    db.run("UPDATE clients SET parent_id=? WHERE id=?", (new_parent, client_id))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Portal (Phase 2) ───────────────────────────────────────────────────────


def _backfill_crops(client_id: int) -> int:
    """Queue social crops for every favorited, ready photo in the client's galleries
    (handler is idempotent, so re-queuing existing crops is a cheap no-op)."""
    rows = db.all_(
        """SELECT DISTINCT f.asset_id FROM favorites f
                      JOIN assets a ON a.id=f.asset_id
                      JOIN galleries g ON g.id=a.gallery_id
                      WHERE g.client_id=? AND a.kind='photo' AND a.status='ready'""",
        (client_id,),
    )
    for r in rows:
        jobs.enqueue("social_crops", {"asset_id": r["asset_id"]})
    return len(rows)


@router.post("/clients/{client_id}/portal")
async def create_portal(client_id: int):
    get_client(client_id)
    if db.one("SELECT id FROM portals WHERE client_id=?", (client_id,)):
        raise HTTPException(status_code=400, detail="portal already exists")
    db.run(
        "INSERT INTO portals (client_id, slug, pin) VALUES (?,?,?)",
        (client_id, security.new_slug(), security.new_pin()),
    )
    n = _backfill_crops(client_id)
    log.info("portal created for client %s (%d crop jobs queued)", client_id, n)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/portal/publish")
async def toggle_portal(client_id: int, published: bool = Form(False)):
    p = db.one("SELECT id FROM portals WHERE client_id=?", (client_id,))
    if not p:
        raise HTTPException(status_code=404)
    db.run("UPDATE portals SET published=? WHERE id=?", (1 if published else 0, p["id"]))
    if published:
        _backfill_crops(client_id)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Brand assets (Phase 2) ─────────────────────────────────────────────────


@router.post("/clients/{client_id}/brand")
async def upload_brand(client_id: int, files: list[UploadFile]):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rejected = []
    for f in files:
        name = _SAFE_NAME.sub("_", Path(f.filename or "upload").name)
        ext = Path(name).suffix.lower()
        if ext not in BRAND_EXTS:
            rejected.append(name)
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        size = await common.save_upload(f, dest_dir / stored)
        db.run(
            "INSERT INTO brand_assets (client_id, filename, stored, bytes) VALUES (?,?,?,?)",
            (client_id, name, stored, size),
        )
    if rejected:
        log.info("client %s brand upload: rejected %s", client_id, rejected)
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/brand/{ba_id}")
async def admin_brand_file(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if not b:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / b["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=b["filename"])


@router.post("/clients/{client_id}/brand/{ba_id}/delete")
async def delete_brand(client_id: int, ba_id: int):
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, client_id))
    if b:
        (config.BRAND_DIR / str(client_id) / b["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_assets WHERE id=?", (ba_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


# ── Brand kits (Slice 3 — composite overlay) ───────────────────────────────
# A brand_kit is a single raster logo + placement params composited onto social
# crops at render time. Distinct from brand_assets (the general file locker).
# Newest active kit wins (see app/brand_kits.overlay_for_client).


@router.post("/clients/{client_id}/kits")
async def upload_kit(
    client_id: int,
    logo: UploadFile,
    label: str = Form(""),
    position: str = Form("br"),
    opacity: int = Form(100),
    scale_pct: int = Form(22),
    margin_pct: int = Form(4),
):
    get_client(client_id)
    if shutil.disk_usage(config.DATA_DIR).free / 1e9 < config.MIN_FREE_GB:
        raise HTTPException(status_code=507, detail="low disk space — upload refused")
    name = _SAFE_NAME.sub("_", Path(logo.filename or "logo").name)
    ext = Path(name).suffix.lower()
    if ext not in KIT_EXTS:
        raise HTTPException(
            status_code=415, detail=f"brand-kit logo must be PNG/WebP/JPEG, not {ext}"
        )
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    dest_dir = config.BRAND_DIR / str(client_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"kit_{uuid.uuid4().hex}{ext}"
    size = await common.save_upload(logo, dest_dir / stored)
    db.run(
        "INSERT INTO brand_kits (client_id, label, stored, bytes, position, "
        "opacity, scale_pct, margin_pct) VALUES (?,?,?,?,?,?,?,?)",
        (
            client_id,
            label.strip() or None,
            stored,
            size,
            position,
            max(0, min(100, opacity)),
            max(1, min(100, scale_pct)),
            max(0, min(50, margin_pct)),
        ),
    )
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/kits/{kit_id}")
async def update_kit(
    client_id: int,
    kit_id: int,
    label: str = Form(""),
    position: str = Form("br"),
    opacity: int = Form(100),
    scale_pct: int = Form(22),
    margin_pct: int = Form(4),
    active: int = Form(0),
):
    k = db.one("SELECT id FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    if position not in KIT_POSITIONS:
        raise HTTPException(status_code=422, detail="bad position")
    db.run(
        "UPDATE brand_kits SET label=?, position=?, opacity=?, scale_pct=?, "
        "margin_pct=?, active=? WHERE id=?",
        (
            label.strip() or None,
            position,
            max(0, min(100, opacity)),
            max(1, min(100, scale_pct)),
            max(0, min(50, margin_pct)),
            1 if active else 0,
            kit_id,
        ),
    )
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.get("/clients/{client_id}/kits/{kit_id}/logo")
async def admin_kit_logo(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if not k:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(client_id) / k["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.post("/clients/{client_id}/kits/{kit_id}/delete")
async def delete_kit(client_id: int, kit_id: int):
    k = db.one("SELECT * FROM brand_kits WHERE id=? AND client_id=?", (kit_id, client_id))
    if k:
        (config.BRAND_DIR / str(client_id) / k["stored"]).unlink(missing_ok=True)
        db.run("DELETE FROM brand_kits WHERE id=?", (kit_id,))
    return RedirectResponse(f"/admin/studio/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/projects")
async def create_project(client_id: int, title: str = Form(...)):
    get_client(client_id)
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (client_id, title.strip()))
    log.info("project %s created for client %s", pid, client_id)
    return RedirectResponse(f"/admin/studio/projects/{pid}", status_code=303)


def _delivery_check(p) -> dict | None:
    """Read-only delivery-workbench state for the focused project (Screening
    Room 3h): the linked gallery's readiness at a glance — frames processed,
    encodes still running (files + social cuts), client link, PIN + expiry.
    Shared by project_detail and the self-updating fragment below."""
    if not p["gallery_id"]:
        return None
    g = db.one("SELECT * FROM galleries WHERE id=?", (p["gallery_id"],))
    if not g:
        return None
    counts = db.one(
        """SELECT COUNT(*) AS total, COALESCE(SUM(status='ready'), 0) AS ready,
                  COALESCE(SUM(status='failed'), 0) AS failed,
                  COALESCE(SUM(kind='video'), 0) AS films
           FROM assets WHERE gallery_id=?""",
        (g["id"],),
    )
    pending_r = db.one(
        """SELECT COUNT(*) AS n FROM asset_renditions r
           JOIN assets a ON a.id = r.asset_id
           WHERE a.gallery_id=? AND r.status='pending'""",
        (g["id"],),
    )["n"]
    total, ready = counts["total"] or 0, counts["ready"] or 0
    failed = counts["failed"] or 0
    return {
        "gallery": g,
        "total": total,
        "ready": ready,
        "failed": failed,
        "films": counts["films"] or 0,
        "pct": round(ready * 100 / total) if total else 0,
        # Only encodes that can still finish count as running — a failed asset
        # stays failed until it's retried from the bench, so it must not keep
        # the fragment polling forever. (Failed renditions likewise: only
        # 'pending' rows are counted at all.)
        "processing": max(total - ready - failed, 0) + pending_r,
        "pending_renditions": pending_r,
        "client_linked": bool(g["client_id"]),
        "sections": db.one("SELECT COUNT(*) AS n FROM sections WHERE gallery_id=?", (g["id"],))[
            "n"
        ],
        "cover": bool(g["cover_asset_id"]),
    }


def _project_stock_chip(project_id: int) -> str:
    """Film-stock billing for the project header, derived from the newest
    booking's event-type slug prefix (re-/pl-/fb-). Empty when the project has
    no booking — nothing is invented."""
    bk = db.one(
        """SELECT et.slug AS et_slug FROM bookings b
           JOIN event_types et ON et.id = b.event_type_id
           WHERE b.project_id=? AND b.status != 'cancelled'
           ORDER BY b.id DESC LIMIT 1""",
        (project_id,),
    )
    if not bk:
        return ""
    slug = bk["et_slug"] or ""
    key = "re" if slug.startswith("re-") else ("pl" if slug.startswith("pl-") else "fb")
    m = specialties.SPECIALTIES[key]
    return f"{m['stock']} — {m['screen_name']}"


@router.get("/projects/{project_id}/delivery-check", response_class=HTMLResponse)
async def project_delivery_check(request: Request, project_id: int):
    """Self-updating delivery workbench fragment — hx-get polled every ~8s
    while the linked gallery still has encodes running. Script-free."""
    p = get_project(project_id)
    return templates.TemplateResponse(
        request,
        "admin/_delivery_check.html",
        {"p": p, "d": _delivery_check(p), "base_url": config.BASE_URL},
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: int):
    p = get_project(project_id)
    proposals = db.all_(
        "SELECT * FROM proposals WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    )
    contracts = db.all_(
        "SELECT * FROM contracts WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    )
    invoices = db.all_(
        "SELECT * FROM invoices WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    )
    emails = db.all_(
        "SELECT * FROM emails_log WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    )
    galleries = db.all_("SELECT id, title FROM galleries ORDER BY created_at DESC")
    plans = db.all_(
        "SELECT id, title, total_cents, anchor_day, active, last_run_period "
        "FROM recurring_plans WHERE project_id=? AND deleted_at IS NULL "
        "ORDER BY created_at DESC",
        (project_id,),
    )
    # Domain F shot list (inline query, not a shotlist import — keeps studio.py
    # free of the shotlist->studio dependency direction).
    shots = db.all_(
        "SELECT * FROM shot_list WHERE project_id=? AND deleted_at IS NULL ORDER BY sort_order, id",
        (project_id,),
    )
    payments = db.all_(
        """SELECT pm.* FROM payments pm
                          JOIN invoices i ON i.id=pm.invoice_id
                          WHERE i.project_id=? ORDER BY pm.created_at DESC""",
        (project_id,),
    )
    timeline = _build_timeline(proposals, contracts, invoices, payments, emails)
    # Testimonial requests raised for this project, with the published state of
    # any quote the client has submitted (so the admin sees pending vs. live).
    testimonial_reqs = db.all_(
        """SELECT tr.*, t.published AS t_published
           FROM testimonial_requests tr
           LEFT JOIN testimonials t ON t.id=tr.testimonial_id
           WHERE tr.project_id=? ORDER BY tr.created_at DESC""",
        (project_id,),
    )
    return templates.TemplateResponse(
        request,
        "admin/project.html",
        {
            "p": p,
            "proposals": proposals,
            "contracts": contracts,
            "invoices": invoices,
            "emails": emails,
            "galleries": galleries,
            "plans": plans,
            "shots": shots,
            "timeline": timeline,
            "testimonial_reqs": testimonial_reqs,
            "shot_categories": usage_vocab.SHOT_CATEGORIES,
            "shot_priorities": usage_vocab.SHOT_PRIORITIES,
            "statuses": PROJECT_STATUSES,
            "base_url": config.BASE_URL,
            "payments": payments,
            "delivery": _delivery_check(p),
            "stock_chip": _project_stock_chip(project_id),
        },
    )


def _build_timeline(proposals, contracts, invoices, payments, emails):
    """Aggregate doc-status timestamps + payments + email sends into one
    reverse-chronological feed. Read-only narration of state already stored on
    the rows — no new state, no automation."""
    ev = []

    def add(ts, kind, text):
        if ts:
            ev.append({"ts": ts, "kind": kind, "text": text})

    for d in proposals:
        add(d["created_at"], "proposal", f"Proposal “{d['title']}” drafted")
        add(d["sent_at"], "proposal", f"Proposal “{d['title']}” sent")
        add(d["viewed_at"], "proposal", f"Proposal “{d['title']}” viewed by client")
        add(d["accepted_at"], "proposal", f"Proposal “{d['title']}” accepted")
    for d in contracts:
        add(d["created_at"], "contract", f"Contract “{d['title']}” drafted")
        add(d["sent_at"], "contract", f"Contract “{d['title']}” sent")
        add(d["viewed_at"], "contract", f"Contract “{d['title']}” viewed by client")
        add(
            d["signed_at"],
            "contract",
            f"Contract “{d['title']}” signed by {d['signer_name'] or 'client'}",
        )
    for d in invoices:
        add(d["created_at"], "invoice", f"Invoice “{d['title']}” drafted")
        add(d["sent_at"], "invoice", f"Invoice “{d['title']}” sent")
        add(d["viewed_at"], "invoice", f"Invoice “{d['title']}” viewed by client")
        add(d["paid_at"], "invoice", f"Invoice “{d['title']}” paid in full")
    for d in payments:
        add(
            d["created_at"],
            "payment",
            f"Payment received · ${d['amount_cents'] / 100:.2f} ({d['kind']})",
        )
    for d in emails:
        add(
            d["created_at"],
            "email",
            f"Email sent · {d['doc_kind']} “{d['subject']}” to {d['to_email']}",
        )

    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev


@router.post("/projects/{project_id}/workspace/publish")
async def publish_workspace(project_id: int):
    p = get_project(project_id)
    slug = p["workspace_slug"] or security.new_slug()
    pin = p["workspace_pin"] or security.new_pin()
    db.run(
        """UPDATE projects SET workspace_slug=?, workspace_pin=?,
              workspace_published=1 WHERE id=?""",
        (slug, pin, project_id),
    )
    log.info("workspace published for project %s", project_id)
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/workspace/unpublish")
async def unpublish_workspace(project_id: int):
    get_project(project_id)
    # Keep the slug/PIN so re-publishing reuses the same link; just close it.
    db.run("UPDATE projects SET workspace_published=0 WHERE id=?", (project_id,))
    log.info("workspace unpublished for project %s", project_id)
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


# ── Testimonials ──────────────────────────────────────────────────────────


@router.get("/testimonials", response_class=HTMLResponse)
async def testimonials_list(request: Request):
    rows = db.all_("""SELECT t.*, g.title AS gallery_title, g.slug AS gallery_slug,
                             EXISTS(SELECT 1 FROM testimonial_requests tr
                                    WHERE tr.testimonial_id=t.id) AS from_client
                      FROM testimonials t
                      LEFT JOIN galleries g ON g.id=t.gallery_id
                      ORDER BY t.position, t.id DESC""")
    galleries = db.all_("SELECT id, title FROM galleries ORDER BY created_at DESC")
    return templates.TemplateResponse(
        request,
        "admin/testimonials.html",
        {"testimonials": rows, "galleries": galleries, "base_url": config.BASE_URL},
    )


@router.post("/testimonials")
async def create_testimonial(
    quote: str = Form(...),
    attribution_name: str = Form(...),
    business: str = Form(""),
    gallery_id: int | None = Form(None),
    position: int = Form(0),
    published: bool = Form(False),
):
    if not (quote.strip() and attribution_name.strip()):
        raise HTTPException(status_code=400, detail="quote and name required")
    tid = db.run(
        """INSERT INTO testimonials (quote, attribution_name, business,
                                              gallery_id, position, published)
                    VALUES (?,?,?,?,?,?)""",
        (
            quote.strip(),
            attribution_name.strip(),
            business.strip() or None,
            gallery_id,
            position,
            1 if published else 0,
        ),
    )
    log.info("testimonial %s created", tid)
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/testimonials/{tid}")
async def update_testimonial(
    tid: int,
    quote: str = Form(...),
    attribution_name: str = Form(...),
    business: str = Form(""),
    gallery_id: int | None = Form(None),
    position: int = Form(0),
    published: bool = Form(False),
):
    if not db.one("SELECT id FROM testimonials WHERE id=?", (tid,)):
        raise HTTPException(status_code=404)
    db.run(
        """UPDATE testimonials SET quote=?, attribution_name=?, business=?,
              gallery_id=?, position=?, published=? WHERE id=?""",
        (
            quote.strip(),
            attribution_name.strip(),
            business.strip() or None,
            gallery_id,
            position,
            1 if published else 0,
            tid,
        ),
    )
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/testimonials/{tid}/delete")
async def delete_testimonial(tid: int):
    db.run("DELETE FROM testimonials WHERE id=?", (tid,))
    return RedirectResponse("/admin/studio/testimonials", status_code=303)


@router.post("/projects/{project_id}/testimonial-request")
async def request_testimonial(project_id: int, gallery_id: int | None = Form(None)):
    """Raise a tokened /t/{slug} link for the client to write their own
    testimonial. Manual send (matches the email doctrine) — the project page
    shows the link to copy. The submitted quote lands unpublished for review."""
    p = get_project(project_id)
    tid = db.run(
        """INSERT INTO testimonial_requests
                      (slug, client_id, project_id, gallery_id)
                    VALUES (?,?,?,?)""",
        (security.new_slug(), p["client_id"], project_id, gallery_id),
    )
    log.info("testimonial request %s raised for project %s", tid, project_id)
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}")
async def update_project(
    project_id: int,
    title: str = Form(...),
    status: str = Form(...),
    notes: str = Form(""),
    gallery_id: int | None = Form(None),
    notion_page_id: str = Form(""),
    shoot_date: str = Form(""),
):
    get_project(project_id)
    if status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    db.run(
        """UPDATE projects SET title=?, status=?, notes=?, gallery_id=?,
              notion_page_id=?, shoot_date=?,
              stage_changed_at=CASE WHEN status=? THEN stage_changed_at
                                    ELSE datetime('now') END
              WHERE id=?""",
        (
            title.strip(),
            status,
            notes.strip() or None,
            gallery_id,
            notion_page_id.strip() or None,
            shoot_date.strip() or None,
            status,
            project_id,
        ),
    )
    return RedirectResponse(f"/admin/studio/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/status")
async def move_project_status(project_id: int, status: str = Form(...)):
    """Kanban quick-move: change only a project's pipeline stage. The full project
    form (update_project) still owns title/notes/gallery edits; this is the
    board's drag-to-column equivalent — one field, one write, back to the board."""
    get_project(project_id)
    if status not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="bad status")
    db.run(
        """UPDATE projects SET status=?,
              stage_changed_at=CASE WHEN status=? THEN stage_changed_at
                                    ELSE datetime('now') END
              WHERE id=?""",
        (status, status, project_id),
    )
    log.info("project %s moved to status %s", project_id, status)
    return RedirectResponse("/admin/studio#projects", status_code=303)
