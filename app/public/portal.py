"""Client portal — galleries, social crops, brand assets, usage rights (Phase 2)."""

import datetime as dt
import hashlib
import logging
import mimetypes
import re
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import config, db, jobs, presets, security
from ..render import templates

log = logging.getLogger("mise.public.portal")
router = APIRouter(prefix="/portal")


def get_live_portal(slug: str) -> "db.sqlite3.Row":
    p = db.one(
        """SELECT p.*, c.name AS client_name, c.company, c.usage_rights
                  FROM portals p JOIN clients c ON c.id=p.client_id WHERE p.slug=?""",
        (slug,),
    )
    if not p or not p["published"]:
        raise HTTPException(status_code=404)
    return p


# Portal auth piggybacks the gallery PIN machinery; portals use NEGATIVE ids
# in pin_attempts so they never collide with gallery lockout rows.


def _cookie_name(portal_id: int) -> str:
    return f"mise_p{portal_id}"


def _has_access(request: Request, portal_id: int) -> bool:
    raw = request.cookies.get(_cookie_name(portal_id))
    return bool(raw) and security.unsign(raw) == f"portal:{portal_id}"


def _require_access(request: Request, portal_id: int) -> None:
    if not _has_access(request, portal_id):
        raise HTTPException(status_code=403, detail="portal access required")


@router.get("/{slug}", response_class=HTMLResponse)
async def view(request: Request, slug: str):
    p = get_live_portal(slug)
    if not _has_access(request, p["id"]):
        return templates.TemplateResponse(
            request, "public/portal_pin.html", {"p": p, "error": None}
        )
    # Capture the previous visit timestamp BEFORE incrementing — drives the
    # client-side "NEW" pill on galleries and brand assets created since the
    # client last looked. None on first visit (no noise on initial render).
    prev_visit = p["last_visit"]
    db.run("UPDATE portals SET visits=visits+1, last_visit=datetime('now') WHERE id=?", (p["id"],))
    galleries = db.all_(
        """SELECT * FROM galleries WHERE client_id=? AND published=1
                           ORDER BY created_at DESC""",
        (p["client_id"],),
    )
    crops = db.all_(
        """SELECT DISTINCT a.*, g.title AS gallery_title
                       FROM favorites f
                       JOIN assets a ON a.id=f.asset_id
                       JOIN galleries g ON g.id=a.gallery_id
                       WHERE g.client_id=? AND g.published=1
                         AND a.kind='photo' AND a.status='ready'
                       ORDER BY g.created_at DESC, a.id""",
        (p["client_id"],),
    )
    brand = db.all_(
        "SELECT * FROM brand_assets WHERE client_id=? ORDER BY created_at DESC", (p["client_id"],)
    )
    # Aggregate the client's favorites across every published gallery —
    # one-line trust signal at the top of the Social crops section so the
    # client knows how many selects they've already made.
    fav_summary = db.one(
        """SELECT COUNT(DISTINCT f.asset_id) AS n_faves,
                                   COUNT(DISTINCT a.gallery_id) AS n_galleries
                            FROM favorites f
                            JOIN assets a ON a.id=f.asset_id
                            JOIN galleries g ON g.id=a.gallery_id
                            WHERE g.client_id=? AND g.published=1
                              AND a.kind='photo' AND a.status='ready'""",
        (p["client_id"],),
    )
    # What-changed header: how many of each surface is new since prev_visit, +
    # a friendly relative-time string. None on first visit so the page lands
    # without a noisy summary.
    changes = None
    if prev_visit:
        n_new_g = sum(1 for g in galleries if g["created_at"] > prev_visit)
        n_new_b = sum(1 for b in brand if b["created_at"] > prev_visit)
        try:
            last = dt.datetime.fromisoformat(prev_visit)
            now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            delta = now - last
            secs = delta.total_seconds()
            if secs < 3600:
                when = f"{max(int(secs // 60), 1)} minutes ago"
            elif secs < 86400:
                hrs = int(secs // 3600)
                when = f"{hrs} hour" + ("s" if hrs != 1 else "") + " ago"
            elif delta.days < 14:
                when = f"{delta.days} day" + ("s" if delta.days != 1 else "") + " ago"
            elif delta.days < 60:
                wks = delta.days // 7
                when = f"{wks} week" + ("s" if wks != 1 else "") + " ago"
            else:
                when = f"on {last.date().isoformat()}"
        except ValueError:
            when = None
        changes = {"n_galleries": n_new_g, "n_brand": n_new_b, "when": when}
    biz = p["company"] or p["client_name"]
    share_subject = quote(f"Your shared portal — {biz}")
    share_body = quote(
        f"Here's the client portal for {biz}:\n\n"
        f"{config.BASE_URL}/portal/{p['slug']}\n"
        f"PIN: {p['pin']}\n\n"
        f"It includes gallery deliveries, social-ready crops of favorited "
        f"photos, and brand assets.\n"
    )
    share_href = f"mailto:?subject={share_subject}&body={share_body}"
    return templates.TemplateResponse(
        request,
        "public/portal.html",
        {
            "p": p,
            "galleries": galleries,
            "crops": crops,
            "brand": brand,
            "ratios": [ps["slug"] for ps in presets.active()],
            "prev_visit": prev_visit,
            "changes": changes,
            "fav_summary": fav_summary,
            "share_href": share_href,
        },
    )


@router.post("/{slug}/pin")
async def check_pin(request: Request, slug: str, pin: str = Form(...)):
    p = get_live_portal(slug)
    ip = security.client_ip(request)
    if security.pin_locked(ip, -p["id"]):
        return templates.TemplateResponse(
            request,
            "public/portal_pin.html",
            {"p": p, "error": f"Too many tries — wait {config.PIN_LOCKOUT_MIN} minutes."},
            status_code=429,
        )
    if pin.strip() != p["pin"]:
        security.pin_fail(ip, -p["id"])
        return templates.TemplateResponse(
            request, "public/portal_pin.html", {"p": p, "error": "Wrong PIN."}, status_code=401
        )
    security.pin_clear(ip, -p["id"])
    resp = RedirectResponse(f"/portal/{slug}", status_code=303)
    resp.set_cookie(
        _cookie_name(p["id"]),
        security.sign(f"portal:{p['id']}"),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return resp


def _client_asset(portal: "db.sqlite3.Row", asset_id: int) -> "db.sqlite3.Row":
    a = db.one(
        """SELECT a.* FROM assets a JOIN galleries g ON g.id=a.gallery_id
                  WHERE a.id=? AND g.client_id=? AND g.published=1 AND a.status='ready'""",
        (asset_id, portal["client_id"]),
    )
    if not a:
        raise HTTPException(status_code=404)
    return a


@router.get("/{slug}/thumb/{asset_id}")
async def thumb(request: Request, slug: str, asset_id: int):
    p = get_live_portal(slug)
    _require_access(request, p["id"])
    a = _client_asset(p, asset_id)
    path = config.MEDIA_DIR / str(a["gallery_id"]) / "thumb" / f"{Path(a['stored']).stem}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"}
    )


@router.get("/{slug}/crop/{asset_id}/{ratio}")
async def crop(request: Request, slug: str, asset_id: int, ratio: str):
    # `ratio` is an untrusted URL token. Only resolve it to a file if it names
    # an active preset; any other value (unknown or inactive) → clean 404 so a
    # token can't be steered toward a path outside the intended crop set.
    if ratio not in {ps["slug"] for ps in presets.active()}:
        raise HTTPException(status_code=404)
    p = get_live_portal(slug)
    _require_access(request, p["id"])
    a = _client_asset(p, asset_id)
    path = jobs.crops_dir(a["gallery_id"]) / f"{Path(a['stored']).stem}_{ratio}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="crop still processing")
    return FileResponse(
        path,
        media_type="image/jpeg",
        filename=f"{Path(a['filename']).stem}_{ratio}.jpg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/{slug}/crops.zip")
async def crops_zip(request: Request, slug: str):
    p = get_live_portal(slug)
    _require_access(request, p["id"])
    rows = db.all_(
        """SELECT DISTINCT a.* FROM favorites f
                      JOIN assets a ON a.id=f.asset_id
                      JOIN galleries g ON g.id=a.gallery_id
                      WHERE g.client_id=? AND g.published=1
                        AND a.kind='photo' AND a.status='ready'
                      ORDER BY a.id""",
        (p["client_id"],),
    )
    ratios = [ps["slug"] for ps in presets.active()]
    files = []
    for a in rows:
        for ratio in ratios:
            path = jobs.crops_dir(a["gallery_id"]) / f"{Path(a['stored']).stem}_{ratio}.jpg"
            if path.is_file():
                files.append((a, ratio, path))
    if not files:
        raise HTTPException(status_code=404, detail="no crops ready yet")

    key = hashlib.sha256("|".join(f"{a['id']}:{r}" for a, r, _ in files).encode()).hexdigest()[:8]
    out = config.ZIP_DIR / f"p{p['id']}-{key}.zip"
    if not out.is_file():
        part = out.with_suffix(".part")
        seen: set[str] = set()
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as zf:
            for a, ratio, path in files:
                arc = f"{Path(a['filename']).stem}_{ratio}.jpg"
                if arc in seen:
                    arc = f"{Path(a['filename']).stem}_{ratio}_{a['id']}.jpg"
                seen.add(arc)
                zf.write(path, arc)
        part.rename(out)
        for old in config.ZIP_DIR.glob(f"p{p['id']}-*.zip"):
            if old != out:
                old.unlink(missing_ok=True)
        log.info("portal %s crops zip built: %d files", p["slug"], len(files))

    dl = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{p['company'] or p['client_name']}-social-crops")
    return FileResponse(out, media_type="application/zip", filename=f"{dl}.zip")


@router.get("/{slug}/brand/{ba_id}")
async def brand_file(request: Request, slug: str, ba_id: int):
    p = get_live_portal(slug)
    _require_access(request, p["id"])
    b = db.one("SELECT * FROM brand_assets WHERE id=? AND client_id=?", (ba_id, p["client_id"]))
    if not b:
        raise HTTPException(status_code=404)
    path = config.BRAND_DIR / str(p["client_id"]) / b["stored"]
    if not path.is_file():
        raise HTTPException(status_code=404)
    media_type = mimetypes.guess_type(b["filename"])[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=b["filename"])
