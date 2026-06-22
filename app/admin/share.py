"""Share-card debugger — surfaces every indexable public URL with its OG
metadata + one-click links to the social-platform unfurl debuggers so Kevin can
verify that a fresh case-study card looks right before posting on IG / LI /
Facebook."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import config, db, security
from ..render import templates

log = logging.getLogger("mise.admin.share")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


def _first_starred_id() -> int | None:
    """The og:image for marketing pages with no specific subject."""
    row = db.one("""SELECT id FROM assets WHERE portfolio=1 AND status='ready'
                    AND kind='photo' ORDER BY id LIMIT 1""")
    return row["id"] if row else None


def _build_urls() -> list[dict]:
    """Compose every indexable URL with its computed OG metadata. Mirrors what
    the templates produce server-side so what shows here matches what the
    socials will scrape (no live HTTP fetch needed)."""
    name = config.SITE_NAME
    default_img = _first_starred_id()
    urls: list[dict] = [
        {
            "path": "/",
            "title": f"{name} — Food & Beverage Photography",
            "description": "Menus, dishes, drinks, and the rooms they live in.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/portfolio",
            "title": f"Portfolio — {name}",
            "description": "F&B photography portfolio — dishes, drinks, interiors.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/work",
            "title": f"Work — {name}",
            "description": "Selected shoots — menus, dishes, drinks, and the rooms they live in.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/services",
            "title": f"Services — {name}",
            "description": "F&B photography, videography, and monthly retainers — Asheville-based.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/about",
            "title": f"About — {name}",
            "description": f"{name} — food & beverage photography.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/book",
            "title": f"Book a shoot — {name}",
            "description": "Pick a date and a starting service.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
        {
            "path": "/contact",
            "title": f"Contact — {name}",
            "description": "Tell me about your restaurant, café, bar, or brand.",
            "og_image_id": default_img,
            "kind": "marketing",
        },
    ]
    # case studies — only published, with the gallery's own hero photo
    studies = db.all_("""SELECT g.slug, g.title, g.client_name, g.cs_tagline,
                                g.cs_brief, g.cs_location,
                                (SELECT a.id FROM assets a WHERE a.gallery_id=g.id
                                 AND a.portfolio=1 AND a.status='ready'
                                 AND a.kind='photo' ORDER BY a.id DESC LIMIT 1)
                                AS hero_id
                         FROM galleries g WHERE g.cs_published=1
                         ORDER BY g.created_at DESC""")
    for s in studies:
        urls.append(
            {
                "path": f"/work/{s['slug']}",
                "title": f"{s['cs_tagline'] or s['title']} — {name}",
                "description": (s["cs_brief"] or "")[:200],
                "og_image_id": s["hero_id"],
                "kind": "case_study",
                "client": s["client_name"],
                "location": s["cs_location"],
            }
        )
    base = config.BASE_URL.rstrip("/")
    for u in urls:
        full = base + u["path"]
        u["full_url"] = full
        u["debuggers"] = [
            ("Facebook", f"https://developers.facebook.com/tools/debug/?q={quote(full)}"),
            ("LinkedIn", f"https://www.linkedin.com/post-inspector/inspect/{quote(full, safe='')}"),
            ("OpenGraph.xyz", f"https://www.opengraph.xyz/url/{quote(full, safe='')}"),
        ]
    return urls


@router.get("/share", response_class=HTMLResponse)
async def share_debugger(request: Request):
    return templates.TemplateResponse(
        request, "admin/share.html", {"urls": _build_urls(), "base_url": config.BASE_URL}
    )
