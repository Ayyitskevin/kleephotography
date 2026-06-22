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
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from . import db, security

log = logging.getLogger("mise.service_api")
router = APIRouter(prefix="/api", dependencies=[Depends(security.require_shots_token)])


@router.get("/shots")
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
