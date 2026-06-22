"""Portals — admin overview of the client-facing content hubs (Phase 2).

Adapts the Admin Portal prototype. The prototype fabricated a brand palette,
typefaces, a client-side caption generator, and a hard-coded license number —
none of which Mise stores. This page shows the REAL portals table instead: one
hub per client, its publish state, the live share link + PIN, engagement
(visits / last visit), and a count of what's inside (galleries, favourited
selects that become social crops, brand assets, usage-rights blurb). It also
lists clients who don't have a portal yet, so it's clear who to set one up for.

Each portal links out to the real management surface (the client's Studio page)
and to the live public hub. Read-only — no writes, so nothing narrates to the
Notion Activity Log. Portal CRUD itself stays per-client in Studio.
"""

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import config, db, security
from ..render import templates

router = APIRouter(prefix="/admin/portals", dependencies=[Depends(security.require_admin)])


def _rel(ts: str | None) -> str:
    """Friendly relative time for a last-visit timestamp (UTC ISO)."""
    if not ts:
        return "never"
    try:
        last = dt.datetime.fromisoformat(ts)
    except ValueError:
        return "unknown"
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    secs = (now - last).total_seconds()
    if secs < 3600:
        return f"{max(int(secs // 60), 1)}m ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h}h ago"
    days = int(secs // 86400)
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    return last.date().isoformat()


@router.get("", response_class=HTMLResponse)
async def portals(request: Request):
    rows = db.all_(
        """SELECT po.id, po.slug, po.pin, po.published, po.visits, po.last_visit,
                  po.client_id, c.name AS client_name, c.company, c.usage_rights,
                  (SELECT COUNT(*) FROM galleries g
                     WHERE g.client_id=po.client_id AND g.published=1) AS n_galleries,
                  (SELECT COUNT(*) FROM brand_assets b
                     WHERE b.client_id=po.client_id) AS n_brand,
                  (SELECT COUNT(DISTINCT f.asset_id) FROM favorites f
                     JOIN assets a ON a.id=f.asset_id
                     JOIN galleries g ON g.id=a.gallery_id
                     WHERE g.client_id=po.client_id AND g.published=1
                       AND a.kind='photo' AND a.status='ready') AS n_faves
             FROM portals po JOIN clients c ON c.id=po.client_id
            ORDER BY po.published DESC, po.last_visit IS NULL, po.last_visit DESC,
                     c.name"""
    )

    base = config.BASE_URL.rstrip("/")
    portals = []
    for r in rows:
        biz = r["company"] or r["client_name"]
        url = f"{base}/portal/{r['slug']}"
        body = quote(
            f"Here's the client portal for {biz}:\n\n{url}\n"
            f"PIN: {r['pin']}\n\nIt includes gallery deliveries, "
            f"social-ready crops of favourited photos, and brand assets.\n"
        )
        portals.append(
            {
                "id": r["id"],
                "client_id": r["client_id"],
                "biz": biz,
                "published": bool(r["published"]),
                "slug": r["slug"],
                "pin": r["pin"],
                "url": url,
                "path": f"/portal/{r['slug']}",
                "share_href": f"mailto:?subject={quote('Your shared portal — ' + biz)}&body={body}",
                "visits": r["visits"] or 0,
                "last": _rel(r["last_visit"]),
                "n_galleries": r["n_galleries"],
                "n_faves": r["n_faves"],
                "n_brand": r["n_brand"],
                "rights": bool((r["usage_rights"] or "").strip()),
            }
        )

    no_portal = db.all_(
        """SELECT id, name, company FROM clients
            WHERE id NOT IN (SELECT client_id FROM portals)
            ORDER BY name"""
    )
    no_portal = [{"id": c["id"], "biz": c["company"] or c["name"]} for c in no_portal]

    n_pub = sum(1 for p in portals if p["published"])
    n_visits = sum(p["visits"] for p in portals)
    n_unopened = sum(1 for p in portals if p["published"] and p["last"] == "never")
    cards = [
        {
            "label": "Published portals",
            "value": str(n_pub),
            "tone": "dark",
            "sub": f"{len(portals) - n_pub} draft",
        },
        {"label": "Total visits", "value": f"{n_visits:,}", "tone": "ok", "sub": "across all hubs"},
        {
            "label": "Never opened",
            "value": str(n_unopened),
            "tone": "warn",
            "sub": "published but unvisited",
        },
        {
            "label": "No portal yet",
            "value": str(len(no_portal)),
            "tone": "plain",
            "sub": "clients to set up",
        },
    ]

    return templates.TemplateResponse(
        request,
        "admin/portals.html",
        {
            "cards": cards,
            "portals": portals,
            "no_portal": no_portal,
        },
    )
