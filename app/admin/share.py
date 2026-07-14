"""Share-card debugger — surfaces every indexable public URL with its OG
metadata + one-click links to the social-platform unfurl debuggers so Kevin can
verify that a fresh case-study card looks right before posting on IG / LI /
Facebook."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .. import config, db, security, specialties
from ..public.site import marketing_page_catalog
from ..render import templates

log = logging.getLogger("mise.admin.share")
router = APIRouter(prefix="/admin", dependencies=[Depends(security.require_admin)])


def _first_starred_id() -> int | None:
    """The og:image for marketing pages with no specific subject."""
    row = db.one("""SELECT id FROM assets WHERE portfolio=1 AND status='ready'
                    AND kind='photo' ORDER BY id LIMIT 1""")
    return row["id"] if row else None


def _specialty_hero_ids() -> dict[str, int]:
    """Match each spoke's first photo from site._portfolio_assets()."""
    rows = db.all_(
        """SELECT id, portfolio_tag FROM assets
           WHERE portfolio=1 AND status='ready' AND kind='photo'
           ORDER BY id DESC"""
    )
    heroes = {}
    for row in rows:
        heroes.setdefault(specialties.specialty_key(row["portfolio_tag"]), row["id"])
    return heroes


def _build_urls() -> list[dict]:
    """Compose every indexable URL with its computed OG metadata. Mirrors what
    the templates produce server-side so what shows here matches what the
    socials will scrape (no live HTTP fetch needed)."""
    default_img = _first_starred_id()
    specialty_heroes = _specialty_hero_ids()
    specialty_by_path = {f"/{meta['slug']}": key for key, meta in specialties.SPECIALTIES.items()}
    urls: list[dict] = []
    for page in marketing_page_catalog():
        path = page["path"]
        specialty_key = specialty_by_path.get(path)
        urls.append(
            {
                **page,
                "meta_description": page["description"],
                "og_description": page["description"],
                "og_image_id": (
                    specialty_heroes.get(specialty_key) if specialty_key else default_img
                ),
                "kind": "marketing",
            }
        )
    # case studies — only published, with the gallery's own hero photo
    studies = db.all_("""SELECT g.slug, g.title, g.client_name, g.cs_tagline,
                                g.cs_brief, g.cs_location,
                                (SELECT a.id FROM assets a WHERE a.gallery_id=g.id
                                 AND a.portfolio=1 AND a.status='ready'
                                 AND a.kind='photo'
                                 ORDER BY a.position, a.id LIMIT 1)
                                AS hero_id
                         FROM galleries g WHERE g.cs_published=1
                         ORDER BY g.created_at DESC""")
    for s in studies:
        meta_description = (s["cs_brief"] or s["cs_tagline"] or s["title"])[:200]
        urls.append(
            {
                "path": f"/work/{s['slug']}",
                "title": f"{s['cs_tagline'] or s['title']} — {config.SITE_NAME}",
                "description": meta_description,
                "meta_description": meta_description,
                "og_description": (s["cs_brief"] or "")[:200],
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
        request,
        "admin/share.html",
        {
            "urls": _build_urls(),
            "base_url": config.BASE_URL,
            "selected_path": request.query_params.get("path", ""),
        },
    )
