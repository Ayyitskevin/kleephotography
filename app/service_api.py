"""Service-to-service read API (Domain F / B-Direct integration).

The ONE inbound machine-to-machine surface in Mise. Odysseus's preshoot_pack calls
GET /api/shots?session=<notion_page_id> to read the shot list Kevin built locally in
the Studio, instead of (or ahead of) its own Notion shotlist DS. Bearer-gated by
security.require_shots_token — disarmed (503) until MISE_SHOTS_TOKEN is provisioned.

Keyed by Notion session-page id because that is the only handle Odysseus holds. We map
it to a Mise project via projects.notion_page_id, then read shot_list. An unmatched
session is NOT an error: it returns {"matched": false, ...} so the caller can fall back
to Notion cleanly. Only title/category/priority are returned — the three fields
preshoot_pack actually formats into its LLM context.

GET /api/galleries/expiring?days=7   — published galleries expiring within N days
GET /api/press/recent?days=30        — press hits published in the last N days
GET /api/galleries                   — published gallery index for Argus (MISE_ARGUS_TOKEN)
All are bearer-gated and read-only.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException

from . import config, db, security

log = logging.getLogger("mise.service_api")
router = APIRouter(prefix="/api")


@router.get("/shots", dependencies=[Depends(security.require_shots_token)])
async def shots(session: str = ""):
    session = (session or "").strip()
    if not session:
        raise HTTPException(status_code=400, detail="session required")

    proj = db.one("SELECT id FROM projects WHERE notion_page_id=?", (session,))
    if not proj:
        return {"matched": False, "session": session, "shots": []}

    rows = db.all_(
        "SELECT title, category, priority FROM shot_list "
        "WHERE project_id=? AND deleted_at IS NULL ORDER BY sort_order, id",
        (proj["id"],),
    )
    return {
        "matched": True,
        "project_id": proj["id"],
        "session": session,
        "shots": [
            {"title": r["title"], "category": r["category"], "priority": r["priority"]}
            for r in rows
        ],
    }


@router.get("/galleries/expiring", dependencies=[Depends(security.require_shots_token)])
async def galleries_expiring(days: int = 7):
    """Published galleries expiring within `days` days. For Odysseus gallery_expiration_warn."""
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be 1-30")
    today = dt.date.today().isoformat()
    horizon = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    rows = db.all_(
        "SELECT g.id, g.title, g.client_name, g.slug, g.expires_at "
        "FROM galleries g "
        "WHERE g.published=1 AND g.expires_at IS NOT NULL "
        "  AND g.expires_at >= ? AND g.expires_at <= ? "
        "ORDER BY g.expires_at",
        (today, horizon),
    )
    return {"galleries": [dict(r) for r in rows], "horizon_days": days, "today": today}


@router.get("/press/recent", dependencies=[Depends(security.require_shots_token)])
async def press_recent(days: int = 30):
    """Press hits with publish_date in the last `days` days. For Odysseus press_to_outreach."""
    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be 1-90")
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = db.all_(
        "SELECT p.outlet, p.title, p.url, p.publish_date, p.channel, "
        "       c.name AS client_name, c.company "
        "FROM press p LEFT JOIN clients c ON c.id=p.client_id "
        "WHERE p.deleted_at IS NULL AND p.publish_date IS NOT NULL "
        "  AND p.publish_date >= ? AND p.publish_date <= date('now', 'localtime') "
        "ORDER BY p.publish_date DESC",
        (since,),
    )
    return {"hits": [dict(r) for r in rows], "since": since}


@router.get("/galleries", dependencies=[Depends(security.require_argus_token)])
async def galleries(published: bool = True):
    """Read-only published gallery index for Argus (Phase 6 slice 1)."""
    if published:
        rows = db.all_(
            """SELECT id, slug, title, project_id, published, client_id,
                      argus_last_run_id, argus_last_job_id, argus_last_status, argus_last_at
               FROM galleries WHERE published=1 AND type='gallery'
               ORDER BY id DESC""",
        )
    else:
        rows = db.all_(
            """SELECT id, slug, title, project_id, published, client_id,
                      argus_last_run_id, argus_last_job_id, argus_last_status, argus_last_at
               FROM galleries WHERE type='gallery' ORDER BY id DESC""",
        )
    return {
        "galleries": [{
            "id": r["id"],
            "slug": r["slug"],
            "title": r["title"],
            "project_id": r["project_id"],
            "published": bool(r["published"]),
            "client_id": r["client_id"],
            "originals_path": str(config.MEDIA_DIR / str(r["id"]) / "original"),
            "argus_last_run_id": r["argus_last_run_id"],
            "argus_last_job_id": r["argus_last_job_id"],
            "argus_last_status": r["argus_last_status"],
            "argus_last_at": r["argus_last_at"],
        } for r in rows],
    }