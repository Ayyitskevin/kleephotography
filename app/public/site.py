"""Public marketing site — the only indexable routes (everything else stays noindex).
Inquiry form emails Kevin's inbox so Odysseus inquiry_intake picks it up unchanged."""

import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .. import config, db, mailer, security
from ..render import ROOT, templates

log = logging.getLogger("mise.public.site")
router = APIRouter()

# Paths the noindex middleware leaves crawlable (matched by exact path or prefix "/").
INDEXABLE = {
    "/",
    "/portfolio",
    "/work",
    "/about",
    "/contact",
    "/book",
    "/reels",
    "/services",
    "/press",
    "/robots.txt",
    "/sitemap.xml",
}

# Public services + tier cards for /services. Mirrors the admin proposal PRESETS
# in both deliverables and price (price_cents = the shoot-day anchor from
# app/admin/proposals.py PRESETS — the market-researched floor; final quotes are
# still tailored on the booking form). The booking dropdown itself stays
# price-free. Order matters: the public page renders these in nav order.
SERVICES = [
    {
        "key": "photography",
        "title": "Photography",
        "tagline": "Menus, dishes, drinks, and the rooms they live in. "
        "Every tier delivers a private same-week gallery with social crops baked in.",
        "monthly": False,
        "tiers": [
            {
                "name": "Starter",
                "subtitle": "Half day",
                "display_price": "$650",
                "price_cents": 90000,
                "includes": [
                    "Up to 3 hours on site, one location",
                    "20 edited finals",
                    "Social crops — 1:1 · 4:5 · 9:16",
                    "One revision round",
                    "Same-week gallery delivery",
                ],
            },
            {
                "name": "Standard",
                "subtitle": "Full day",
                "display_price": "$1,200",
                "price_cents": 180000,
                "includes": [
                    "Up to 6 hours — menu, drinks & room",
                    "45 edited finals",
                    "Full social crop pack",
                    "Two revision rounds",
                    "Brand library starter set",
                ],
            },
            {
                "name": "Premium",
                "subtitle": "Extended",
                "display_price": "$2,400",
                "price_cents": 320000,
                "includes": [
                    "Up to two shoot days",
                    "Full menu + campaign concepts",
                    "90+ edited finals",
                    "On-set art direction",
                    "Commercial usage license",
                ],
            },
        ],
    },
    {
        "key": "videography",
        "title": "Videography",
        "tagline": "Short-form social motion that earns the scroll, plus hero "
        "brand films for launches and campaigns.",
        "monthly": False,
        "tiers": [
            {
                "name": "Starter",
                "subtitle": "The reel",
                "display_price": "$850",
                "price_cents": 180000,
                "includes": [
                    "Half-day shoot",
                    "One hero reel, 15–30s",
                    "Three vertical cutdowns",
                    "Licensed audio",
                    "Delivered 9:16 + 1:1",
                ],
            },
            {
                "name": "Standard",
                "subtitle": "Social set",
                "display_price": "$1,800",
                "price_cents": 320000,
                "includes": [
                    "Full-day shoot",
                    "One brand film, 45–60s",
                    "Five social cutdowns",
                    "Photo stills included",
                    "Color grade + captions",
                ],
            },
            {
                "name": "Premium",
                "subtitle": "Campaign",
                "display_price": "$3,900",
                "price_cents": 580000,
                "includes": [
                    "Two shoot days",
                    "Hero film + reel series",
                    "Storyboard & art direction",
                    "Cinematic color grade",
                    "Full usage license",
                ],
            },
        ],
    },
    {
        "key": "brand_partner",
        "title": "Brand Partner",
        "tagline": "A monthly content rhythm with a built-in discount versus "
        "ad-hoc. Deliverables auto-draft each month — never auto-sent or charged.",
        "monthly": True,
        "tiers": [
            {
                "name": "Photo",
                "subtitle": "per month",
                "display_price": "$900",
                "price_unit": "/mo",
                "price_cents": 140000,
                "includes": [
                    "One half-day shoot monthly",
                    "20 finals each month",
                    "Social crop pack",
                    "Rolling content calendar",
                    "Cancel anytime",
                ],
            },
            {
                "name": "Photo + Reels",
                "subtitle": "per month",
                "display_price": "$1,600",
                "price_unit": "/mo",
                "price_cents": 220000,
                "includes": [
                    "One full-day shoot monthly",
                    "30 finals + 4 reels",
                    "Caption packs",
                    "Brand kit on file",
                    "Priority scheduling",
                ],
            },
            {
                "name": "Two-day",
                "subtitle": "per month",
                "display_price": "$2,900",
                "price_unit": "/mo",
                "price_cents": 380000,
                "includes": [
                    "Two shoot days monthly",
                    "Photo + video each visit",
                    "Quarterly hero film",
                    "Biggest per-asset discount",
                    "Dedicated calendar",
                ],
            },
        ],
    },
]

# Page-specific FAQs — prototype copy from Contact.dc.html and Book.dc.html.
CONTACT_FAQS = [
    (
        "What does a typical project cost?",
        "It depends on scope — a half-day menu refresh and a multi-day campaign "
        "sit far apart. Send a few details and I will quote the right tier; full "
        "ranges live on the Services page.",
    ),
    (
        "How soon can you shoot?",
        "Usually within two to three weeks, sometimes sooner for a launch. Tell "
        "me your target date and I will be honest about what is open.",
    ),
    (
        "Do you offer ongoing content?",
        "Yes — the Brand Partner retainer covers a monthly rhythm of photo, photo "
        "+ reels, or a two-day schedule, at a built-in discount versus ad-hoc shoots.",
    ),
    (
        "Who owns the photos?",
        "You get a clear commercial usage license for your marketing and social. "
        "Licensing terms are spelled out in your gallery and proposal — no surprises.",
    ),
]

BOOK_PROMISES = [
    "Reply within one business day",
    "Tailored proposal — no obligation",
    "Same-week gallery after the shoot",
]

BOOK_FAQS = [
    (
        "How far in advance should I book?",
        "Two to three weeks is comfortable, but I keep a few slots open for menu "
        "changes and launches. If it is urgent, say so in the form and I will tell "
        "you honestly what is possible.",
    ),
    (
        "Do you style the food?",
        "Light, honest styling is included — the goal is for the dish to look like "
        "what arrives at the table, at its best. For elaborate set styling I can "
        "bring in a stylist and fold it into the quote.",
    ),
    (
        "What do I actually get, and when?",
        "A private online gallery, usually within the week: full-resolution "
        "downloads, your favorites, and social crops in 1:1, 4:5, and 9:16 — "
        "already sized for every platform.",
    ),
    (
        "Do you travel outside Asheville?",
        "Often. Charlotte, Raleigh, and Wilmington are regular trips, and travel is "
        "easy to fold into a quote for anywhere in Western North Carolina and beyond.",
    ),
]

FAQS = BOOK_FAQS  # default for templates that don't override
templates.env.globals["faqs"] = FAQS


def _portfolio_assets() -> list:
    return db.all_("""SELECT a.*, g.client_name, g.title AS gallery_title
                      FROM assets a
                      LEFT JOIN galleries g ON g.id = a.gallery_id
                      WHERE a.portfolio=1 AND a.status='ready' AND a.kind='photo'
                      ORDER BY a.id DESC""")


def _parse_cs_credits(raw: str | None) -> list[dict]:
    """Turn case-study credit lines into label/value pairs for the grid layout."""
    out: list[dict] = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ":" in ln:
            label, _, value = ln.partition(":")
            out.append({"label": label.strip(), "value": value.strip()})
        else:
            out.append({"label": "Credit", "value": ln})
    return out


def _portfolio_reels() -> list:
    """Portfolio-starred videos for the public /reels showcase — same explicit
    portfolio=1 gate as photos, so nothing client-private is ever exposed."""
    rows = db.all_("""SELECT * FROM assets
                       WHERE portfolio=1 AND status='ready' AND kind='video'
                       ORDER BY id DESC""")
    if rows:
        return rows
    # Demo fallback: one ready reel is enough to render the motion layout.
    return db.all_("""SELECT * FROM assets
                       WHERE status='ready' AND kind='video'
                       ORDER BY id DESC LIMIT 5""")


def _testimonials(gallery_id: int | None = None, limit: int | None = None) -> list:
    """Return published testimonials. gallery_id=None means 'general' (home + /services);
    pass an int for case-study-scoped quotes that ride along with that gallery."""
    if gallery_id is None:
        rows = db.all_("""SELECT * FROM testimonials
                          WHERE published=1 AND gallery_id IS NULL
                          ORDER BY position, id DESC""")
    else:
        rows = db.all_(
            """SELECT * FROM testimonials
                          WHERE published=1 AND gallery_id=?
                          ORDER BY position, id DESC""",
            (gallery_id,),
        )
    return rows[:limit] if limit else rows


def _press_features() -> list:
    """Press hits Kevin has opted onto the public site, newest-first, deduped by
    outlet — one entry per publication, the most recent piece keeping the link.
    Gate matches the H->E rule plus the public opt-in: show_on_site=1 AND a
    populated, non-future publish_date. Default-0 flag means nothing is here
    until Kevin explicitly features it."""
    rows = db.all_("""SELECT outlet, title, url, publish_date FROM press
                      WHERE deleted_at IS NULL AND show_on_site=1
                        AND publish_date IS NOT NULL
                        AND publish_date <= date('now','localtime')
                      ORDER BY publish_date DESC, id DESC""")
    seen, out = set(), []
    for r in rows:
        key = (r["outlet"] or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _case_studies() -> list:
    """Galleries Kevin has promoted as public case studies, newest first."""
    return db.all_("""SELECT g.*,
                      (SELECT a.id FROM assets a WHERE a.gallery_id=g.id
                       AND a.portfolio=1 AND a.status='ready' AND a.kind='photo'
                       ORDER BY a.id DESC LIMIT 1) AS hero_id
                      FROM galleries g WHERE g.cs_published=1
                      ORDER BY g.created_at DESC""")


def _demo_gallery() -> dict | None:
    """Prospect-facing sample gallery link (optional)."""
    slug = (config.DEMO_GALLERY_SLUG or "").strip()
    if slug:
        g = db.one("SELECT slug, title FROM galleries WHERE slug=?", (slug,))
    else:
        g = db.one("""SELECT slug, title FROM galleries
                      WHERE lower(title) LIKE '%sample%'
                         OR lower(slug) LIKE '%sample%'
                      ORDER BY id LIMIT 1""")
    if not g:
        return None
    out = {"slug": g["slug"], "title": g["title"], "url": f"/g/{g['slug']}"}
    if config.DEMO_GALLERY_PIN.strip():
        out["pin"] = config.DEMO_GALLERY_PIN.strip()
    return out


def _contact_prefill(request: Request) -> dict:
    """Build contact form prefill from ?prefill= / ?service= / ?tier= deep links."""
    q = request.query_params
    prefill_kind = (q.get("prefill") or "").strip()
    service = (q.get("service") or "").strip()
    tier = (q.get("tier") or "").strip()
    business = (q.get("business") or "").strip()
    try:
        count = int(q.get("count") or 0)
    except ValueError:
        count = 0
    gallery = (q.get("gallery") or "").strip()
    message = ""
    if prefill_kind == "gallery_formats" and count > 0:
        message = (
            f"Hi — following up on the gallery delivery. Could I get "
            f"additional formats (or larger sizes) for the {count} "
            f"select{'s' if count != 1 else ''} I've favorited? Thanks!"
        )
    elif prefill_kind == "gallery_question" and gallery:
        message = f'Hi — I have a question about the "{gallery}" gallery delivery. '
    elif prefill_kind == "demo_gallery":
        message = (
            "Hi — I'd like to walk through the sample client gallery "
            "and see how proofing and social crops work."
        )
    elif service and tier:
        message = (
            f"Hi — I'm interested in the {tier} tier for {service}. "
            f"Here's a bit about my project:\n\n"
        )
    elif service:
        message = f"Hi — I'm interested in {service}. Here's a bit about my project:\n\n"
    return {"business": business, "message": message, "service": service, "tier": tier}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    featured = _portfolio_assets()[:6]
    reels = _portfolio_reels()
    return templates.TemplateResponse(
        request,
        "site/home.html",
        {
            "featured": featured,
            "reels": reels,
            "hero_reel": reels[0] if reels else None,
            "press": _press_features()[:12],
            "testimonials": _testimonials(limit=3),
            "demo_gallery": _demo_gallery(),
        },
    )


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request):
    assets = _portfolio_assets()
    # distinct tags actually in use, alphabetical — the filter chips
    tags = sorted({a["portfolio_tag"] for a in assets if a["portfolio_tag"]})
    # Case studies also surface here as a "Featured clients" band; the dedicated
    # /work index + /work/{slug} detail pages are public + crawlable too.
    return templates.TemplateResponse(
        request,
        "site/portfolio.html",
        {
            "assets": assets,
            "tags": tags,
            "studies": _case_studies(),
            "demo_gallery": _demo_gallery(),
        },
    )


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(
        request, "site/about.html", {"featured": _portfolio_assets()[:1]}
    )


@router.get("/press", response_class=HTMLResponse)
async def press(request: Request):
    return templates.TemplateResponse(request, "site/press.html", {"press": _press_features()})


@router.get("/services", response_class=HTMLResponse)
async def services(request: Request):
    return templates.TemplateResponse(
        request,
        "site/services.html",
        {"services": SERVICES, "testimonials": _testimonials(limit=3)},
    )


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    """Optional ?prefill=<kind>&service=&tier= for cross-surface deep links
    (parsed in _contact_prefill straight off the query string)."""
    pf = _contact_prefill(request)
    return templates.TemplateResponse(
        request,
        "site/contact.html",
        {
            "sent": False,
            "error": None,
            "prefill": pf,
            "featured": _portfolio_assets()[:1],
            "faqs": CONTACT_FAQS,
            "faq_heading": "Good to know",
        },
    )


@router.post("/contact", response_class=HTMLResponse)
async def submit_inquiry(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    business: str = Form(""),
    message: str = Form(...),
    website: str = Form(""),
    service: str = Form(""),
    shoot_date: str = Form(""),
    dish_count: str = Form(""),
    usage: str = Form(""),
    budget: str = Form(""),
):
    # Honeypot: real visitors never see the "website" field — bots fill it.
    if website.strip():
        return templates.TemplateResponse(
            request,
            "site/contact.html",
            {
                "sent": True,
                "error": None,
                "faqs": CONTACT_FAQS,
                "faq_heading": "Good to know",
            },
        )

    def _error(msg: str, status: int):
        # Re-render with an error AND every submitted value echoed back, so a
        # typo or throttle never wipes a visitor's typed quote request (the
        # template reads these off `prefill`).
        return templates.TemplateResponse(
            request,
            "site/contact.html",
            {
                "sent": False,
                "error": msg,
                "prefill": {
                    "name": name.strip(),
                    "email": email.strip(),
                    "business": business.strip(),
                    "message": message.strip(),
                    "service": service.strip(),
                    "shoot_date": shoot_date.strip(),
                    "dish_count": dish_count.strip(),
                    "usage": usage.strip(),
                    "budget": budget.strip(),
                },
                "featured": _portfolio_assets()[:1],
                "faqs": CONTACT_FAQS,
                "faq_heading": "Good to know",
            },
            status_code=status,
        )

    # Per-IP throttle: 3 inquiries / hour. Real visitors send 1; a determined
    # spammer's 4th submit hits this wall (honeypot kicks in earlier for naive
    # bots; this is for the ones smart enough to skip the trap).
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_CONTACT):
        log.warning("contact form throttled for ip=%s", ip)
        return _error(
            "You've sent a few inquiries recently — give me a chance to reply "
            "before sending another one. If it's urgent, email me directly.",
            429,
        )
    name, email, message = name.strip(), email.strip(), message.strip()
    if not (name and message and "@" in email and "." in email.rsplit("@", 1)[-1]):
        return _error("Please add your name, a valid email, and a short message.", 400)
    # Optional scope fields from the quote-request form. service + target date
    # get their own inquiry columns (so the inquiry→quote button can lift them
    # into a project without re-typing); the rest fold into the message + email
    # body so Odysseus inquiry_intake keeps parsing one plain-text block unchanged.
    service, shoot_date = service.strip(), shoot_date.strip()
    details = [
        (lbl, v.strip())
        for lbl, v in (
            ("Project type", service),
            ("Target date", shoot_date),
            ("Dishes / setups", dish_count),
            ("Usage / licensing", usage),
            ("Budget range", budget),
        )
        if v.strip()
    ]
    full_message = message
    if details:
        full_message += "\n\n— Project details —\n" + "\n".join(f"{lbl}: {v}" for lbl, v in details)
    security.inquiry_record(ip, security.INQUIRY_BUCKET_CONTACT)
    iid = db.run(
        """INSERT INTO inquiries (name, email, business, message, service, shoot_date)
                    VALUES (?,?,?,?,?,?)""",
        (name, email, business.strip() or None, full_message, service or None, shoot_date or None),
    )
    if mailer.configured():
        body = (
            f"New inquiry via kleephotography.com\n\n"
            f"Name: {name}\nEmail: {email}\n"
            f"Business: {business.strip() or '—'}\n\n{full_message}\n"
        )
        try:
            mailer.send(config.GMAIL_USER, f"New inquiry — {name}", body, reply_to=email)
            db.run("UPDATE inquiries SET emailed=1 WHERE id=?", (iid,))
        except Exception as e:
            log.error("inquiry %s stored but email failed: %s", iid, e)
    else:
        log.error("inquiry %s stored — mailer not configured, no email sent", iid)
    log.info("inquiry %s received", iid)
    return templates.TemplateResponse(
        request,
        "site/contact.html",
        {
            "sent": True,
            "error": None,
            "faqs": CONTACT_FAQS,
            "faq_heading": "Good to know",
        },
    )


@router.get("/work", response_class=HTMLResponse)
async def work_index(request: Request):
    return templates.TemplateResponse(
        request,
        "site/work_index.html",
        {"studies": _case_studies(), "demo_gallery": _demo_gallery()},
    )


@router.get("/work/{slug}", response_class=HTMLResponse)
async def work_detail(request: Request, slug: str):
    g = db.one("SELECT * FROM galleries WHERE slug=? AND cs_published=1", (slug,))
    if not g:
        raise HTTPException(status_code=404)
    photos = db.all_(
        """SELECT * FROM assets WHERE gallery_id=? AND portfolio=1
                        AND status='ready' AND kind='photo'
                        ORDER BY position, id""",
        (g["id"],),
    )
    credit_items = _parse_cs_credits(g["cs_credits"])
    testimonials = _testimonials(gallery_id=g["id"])
    return templates.TemplateResponse(
        request,
        "site/work_detail.html",
        {
            "g": g,
            "photos": photos,
            "credit_items": credit_items,
            "testimonials": testimonials,
            "pull_quote": testimonials[0] if testimonials else None,
        },
    )


@router.get("/reels", response_class=HTMLResponse)
async def reels(request: Request):
    return templates.TemplateResponse(
        request, "site/reels.html", {"reels": _portfolio_reels(), "demo_gallery": _demo_gallery()}
    )


@router.get("/site/img/{asset_id}")
async def portfolio_image(asset_id: int, variant: str = "web"):
    """Unauthenticated — serves ONLY portfolio-flagged, ready photos."""
    if variant not in ("web", "thumb"):
        raise HTTPException(status_code=404)
    a = db.one(
        """SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='photo'""",
        (asset_id,),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(a["gallery_id"]) / variant / f"{Path(a['stored']).stem}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/site/vid/{asset_id}")
async def portfolio_video(asset_id: int):
    """Unauthenticated — serves ONLY portfolio-flagged, ready videos (the /reels
    showcase). FileResponse handles HTTP Range, so iOS scrubbing works."""
    a = db.one(
        """SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='video'""",
        (asset_id,),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(a["gallery_id"]) / "web" / f"{Path(a['stored']).stem}.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/site/poster/{asset_id}")
async def portfolio_video_poster(asset_id: int):
    """Poster frame for a portfolio video — same public portfolio gate."""
    a = db.one(
        """SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='video'""",
        (asset_id,),
    )
    if not a:
        raise HTTPException(status_code=404)
    path = config.MEDIA_DIR / str(a["gallery_id"]) / "web" / f"{Path(a['stored']).stem}_poster.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"}
    )


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Old crawlers and share scrapers request /favicon.ico directly, ignoring
    the <link rel=icon> tags — serve the real file instead of a 404."""
    return FileResponse(
        ROOT / "static" / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return (
        "User-agent: *\n"
        "Disallow: /g/\nDisallow: /portal/\nDisallow: /media/\n"
        "Disallow: /admin\nDisallow: /p/\nDisallow: /c/\nDisallow: /i/\n"
        "Allow: /\n"
        f"Sitemap: {config.BASE_URL}/sitemap.xml\n"
    )


@router.get("/sitemap.xml")
async def sitemap():
    paths = [
        "/",
        "/portfolio",
        "/work",
        "/services",
        "/about",
        "/contact",
        "/book",
        "/reels",
        "/press",
    ]
    # Case-study detail pages are also surfaced on /portfolio (Featured clients)
    # but get their own crawlable URLs here (/work index + /work/{slug} details).
    paths += [f"/work/{g['slug']}" for g in _case_studies()]
    urls = "".join(f"<url><loc>{config.BASE_URL}{p}</loc></url>" for p in paths)
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )
