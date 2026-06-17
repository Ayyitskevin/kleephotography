"""Public marketing site — the only indexable routes (everything else stays noindex).
Inquiry form emails Kevin's inbox so Odysseus inquiry_intake picks it up unchanged."""

import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .. import config, db, mailer, security
from ..render import templates

log = logging.getLogger("mise.public.site")
router = APIRouter()

# Paths the noindex middleware leaves crawlable (matched by exact path or prefix "/").
INDEXABLE = {"/", "/portfolio", "/work", "/about", "/contact", "/book", "/reels",
             "/services", "/press", "/robots.txt", "/sitemap.xml"}

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
                   "Natural light first; styled when the work needs it.",
        "monthly": False,
        "tiers": [
            {"name": "Starter", "price_cents": 90000, "includes": [
                "Half-day shoot (up to 4 hours)",
                "Up to 20 edited, web-ready images",
                "Online gallery delivery",
                "Standard usage rights",
            ]},
            {"name": "Standard", "price_cents": 180000, "includes": [
                "Full-day shoot (up to 8 hours)",
                "Up to 50 edited images",
                "Social crops (1:1, 4:5, 9:16) for hero selects",
                "Online gallery + standard usage rights",
            ]},
            {"name": "Premium", "price_cents": 320000, "includes": [
                "Extended-day shoot (up to 10 hours)",
                "Up to 75 edited images",
                "Social crops for every select",
                "5-day rush turnaround",
                "Extended usage rights",
            ]},
        ],
    },
    {
        "key": "videography",
        "title": "Videography",
        "tagline": "Short-form social motion that earns the scroll, plus "
                   "hero brand films for the launches and campaigns.",
        "monthly": False,
        "tiers": [
            {"name": "Starter", "price_cents": 180000, "includes": [
                "Half-day shoot (up to 4 hours)",
                "3 short-form vertical reels (15–30s each)",
                "Licensed music + color grade",
                "Standard usage rights",
            ]},
            {"name": "Standard", "price_cents": 320000, "includes": [
                "Full-day shoot (up to 8 hours)",
                "6 short-form vertical reels (15–60s each)",
                "B-roll package + color grade + licensed music",
                "Standard usage rights",
            ]},
            {"name": "Premium", "price_cents": 580000, "includes": [
                "Two shoot days (up to 16 hours total)",
                "10 short-form reels + 1 hero brand video (60–90s)",
                "B-roll package, color grade, rush available",
                "Extended usage rights",
            ]},
        ],
    },
    {
        "key": "brand_partner",
        "title": "Brand Partner",
        "tagline": "A monthly content rhythm — photo, photo + reels, or "
                   "a two-day photo + video schedule — at a meaningful "
                   "discount vs ad-hoc. Three-month minimum, month-to-month after.",
        "monthly": True,
        "tiers": [
            {"name": "Starter", "price_cents": 140000, "includes": [
                "1 photo content day per month",
                "~20 edited images",
                "Social crop pack (1:1, 4:5, 9:16) for hero selects",
                "Standing client portal + priority scheduling",
            ]},
            {"name": "Standard", "price_cents": 220000, "includes": [
                "1 photo + short-form video content day per month",
                "~30 edited images + 3 short-form reels",
                "Social crop pack for every select",
                "Priority scheduling + standing client portal",
            ]},
            {"name": "Premium", "price_cents": 380000, "includes": [
                "Two content days per month (photo + video)",
                "~50 edited images + 6 short-form reels",
                "Quarterly hero brand video (60–90s)",
                "Extended usage rights + concierge scheduling",
            ]},
        ],
    },
]

# F&B-specific FAQs — same list on /book and /contact so a question answered once
# never has to be answered by email again. Also emitted as JSON-LD FAQPage schema
# for Google's rich-result FAQ blocks.
FAQS = [
    ("What's the turnaround on edited images?",
     "Typically 7–10 business days from the shoot for photo deliveries; "
     "short-form video edits add 3–5 days. Rush turnaround is available for an "
     "additional fee — mention it in your booking message."),
    ("What are your service tiers?",
     "Three categories — Photography, Videography, and the Brand Partner "
     "monthly retainer — each with Starter, Standard, and Premium tiers. "
     "Photography tiers scale from a half-day shoot (~20 images) up to an "
     "extended day with ~75 images and rush turnaround. Videography tiers "
     "scale from 3 short-form reels up to two shoot days with a hero brand "
     "video. The Brand Partner retainer locks in monthly content days "
     "(photo, photo + reels, or a two-day photo + video schedule with a "
     "quarterly hero video) at a meaningful discount vs ad-hoc — tell me "
     "your goal and I'll quote the right tier."),
    ("Do I need to bring a food stylist or video crew?",
     "Not required — for most shoots I can work with what you put in front "
     "of me. For menu launches, campaigns, or hero video work I keep a short "
     "list of food stylists, prop stylists, and video assistants I trust and "
     "can bring in. Mention your budget on the booking form and I'll line it up."),
    ("Where do you shoot?",
     "Based in Asheville — Western North Carolina is home turf with no travel "
     "fee. Charlotte, Raleigh, and Wilmington are regulars and quoted per trip. "
     "Anywhere else is happily quoted; on-site at your restaurant, café, or "
     "venue is preferred because the room and the natural light tell the story."),
    ("What about usage rights?",
     "Standard delivery gives you full rights to use the photos and videos on "
     "your website, social, menus, in-store, press kits, and advertising. I "
     "retain the right to use selects in my own portfolio and marketing. "
     "Premium tiers include extended usage rights; exclusivity and talent "
     "releases are available on request."),
    ("How do payments work?",
     "A 50% deposit holds the date; the balance is due on delivery. Card or "
     "ACH via Stripe — invoices come from this same site. Brand Partner "
     "retainers are billed at the start of each month."),
    ("What if we need to reschedule?",
     "Reschedules with at least 7 days' notice are no-charge — your deposit "
     "moves to the new date. Inside 7 days, ask me — I'll do everything I can "
     "to make it work."),
    ("How does the Brand Partner retainer save money?",
     "Brand Partner locks in a monthly content rhythm — photo, photo + reels, "
     "or a two-day photo-plus-video schedule — at roughly 35–40% off the "
     "equivalent ad-hoc bundle, with priority scheduling and a standing client "
     "portal. It's built for restaurants and brands with seasonal menus or "
     "weekly social cadence. Three-month minimum, month-to-month after."),
]
templates.env.globals["faqs"] = FAQS


def _portfolio_assets() -> list:
    return db.all_("""SELECT * FROM assets
                      WHERE portfolio=1 AND status='ready' AND kind='photo'
                      ORDER BY id DESC""")


def _portfolio_reels() -> list:
    """Portfolio-starred videos for the public /reels showcase — same explicit
    portfolio=1 gate as photos, so nothing client-private is ever exposed."""
    return db.all_("""SELECT * FROM assets
                      WHERE portfolio=1 AND status='ready' AND kind='video'
                      ORDER BY id DESC""")


def _testimonials(gallery_id: int | None = None, limit: int | None = None) -> list:
    """Return published testimonials. gallery_id=None means 'general' (home + /services);
    pass an int for case-study-scoped quotes that ride along with that gallery."""
    if gallery_id is None:
        rows = db.all_("""SELECT * FROM testimonials
                          WHERE published=1 AND gallery_id IS NULL
                          ORDER BY position, id DESC""")
    else:
        rows = db.all_("""SELECT * FROM testimonials
                          WHERE published=1 AND gallery_id=?
                          ORDER BY position, id DESC""", (gallery_id,))
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
        message = (f"Hi — following up on the gallery delivery. Could I get "
                   f"additional formats (or larger sizes) for the {count} "
                   f"select{'s' if count != 1 else ''} I've favorited? Thanks!")
    elif prefill_kind == "gallery_question" and gallery:
        message = (f"Hi — I have a question about the \"{gallery}\" gallery "
                   f"delivery. ")
    elif prefill_kind == "demo_gallery":
        message = ("Hi — I'd like to walk through the sample client gallery "
                   "and see how proofing and social crops work.")
    elif service and tier:
        message = (f"Hi — I'm interested in the {tier} tier for {service}. "
                   f"Here's a bit about my project:\n\n")
    elif service:
        message = (f"Hi — I'm interested in {service}. "
                   f"Here's a bit about my project:\n\n")
    return {"business": business, "message": message, "service": service, "tier": tier}


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    featured = _portfolio_assets()[:6]
    return templates.TemplateResponse(request, "site/home.html",
                                      {"featured": featured,
                                       "press": _press_features()[:12],
                                       "testimonials": _testimonials(limit=3),
                                       "demo_gallery": _demo_gallery()})


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request):
    assets = _portfolio_assets()
    # distinct tags actually in use, alphabetical — the filter chips
    tags = sorted({a["portfolio_tag"] for a in assets if a["portfolio_tag"]})
    # Case studies also surface here as a "Featured clients" band; the dedicated
    # /work index + /work/{slug} detail pages are public + crawlable too.
    return templates.TemplateResponse(request, "site/portfolio.html",
                                      {"assets": assets, "tags": tags,
                                       "studies": _case_studies(),
                                       "demo_gallery": _demo_gallery()})


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(request, "site/about.html")


@router.get("/press", response_class=HTMLResponse)
async def press(request: Request):
    return templates.TemplateResponse(request, "site/press.html",
                                      {"press": _press_features()})


@router.get("/services", response_class=HTMLResponse)
async def services(request: Request):
    return templates.TemplateResponse(request, "site/services.html",
                                      {"services": SERVICES,
                                       "testimonials": _testimonials(limit=3)})


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    """Optional ?prefill=<kind>&service=&tier= for cross-surface deep links
    (parsed in _contact_prefill straight off the query string)."""
    pf = _contact_prefill(request)
    return templates.TemplateResponse(request, "site/contact.html",
                                      {"sent": False, "error": None,
                                       "prefill": pf})


@router.post("/contact", response_class=HTMLResponse)
async def submit_inquiry(request: Request, name: str = Form(...), email: str = Form(...),
                         business: str = Form(""), message: str = Form(...),
                         website: str = Form(""), service: str = Form(""),
                         shoot_date: str = Form(""), dish_count: str = Form(""),
                         usage: str = Form(""), budget: str = Form("")):
    # Honeypot: real visitors never see the "website" field — bots fill it.
    if website.strip():
        return templates.TemplateResponse(request, "site/contact.html",
                                          {"sent": True, "error": None})
    # Per-IP throttle: 3 inquiries / hour. Real visitors send 1; a determined
    # spammer's 4th submit hits this wall (honeypot kicks in earlier for naive
    # bots; this is for the ones smart enough to skip the trap).
    ip = security.client_ip(request)
    if security.inquiry_throttled(ip, security.INQUIRY_BUCKET_CONTACT):
        log.warning("contact form throttled for ip=%s", ip)
        return templates.TemplateResponse(
            request, "site/contact.html",
            {"sent": False, "error":
             "You've sent a few inquiries recently — give me a chance to reply "
             "before sending another one. If it's urgent, email me directly."},
            status_code=429)
    name, email, message = name.strip(), email.strip(), message.strip()
    if not (name and message and "@" in email and "." in email.rsplit("@", 1)[-1]):
        return templates.TemplateResponse(
            request, "site/contact.html",
            {"sent": False, "error": "Please fill in your name, a valid email, "
                                     "and a message."},
            status_code=400)
    # Optional scope fields from the quote-request form. service + target date
    # get their own inquiry columns (so the inquiry→quote button can lift them
    # into a project without re-typing); the rest fold into the message + email
    # body so Odysseus inquiry_intake keeps parsing one plain-text block unchanged.
    service, shoot_date = service.strip(), shoot_date.strip()
    details = [(lbl, v.strip()) for lbl, v in (
        ("Project type", service), ("Target date", shoot_date),
        ("Dishes / setups", dish_count), ("Usage / licensing", usage),
        ("Budget range", budget)) if v.strip()]
    full_message = message
    if details:
        full_message += "\n\n— Project details —\n" + "\n".join(
            f"{lbl}: {v}" for lbl, v in details)
    security.inquiry_record(ip, security.INQUIRY_BUCKET_CONTACT)
    iid = db.run("""INSERT INTO inquiries (name, email, business, message, service, shoot_date)
                    VALUES (?,?,?,?,?,?)""",
                 (name, email, business.strip() or None, full_message,
                  service or None, shoot_date or None))
    if mailer.configured():
        body = (f"New inquiry via kleephotography.com\n\n"
                f"Name: {name}\nEmail: {email}\n"
                f"Business: {business.strip() or '—'}\n\n{full_message}\n")
        try:
            mailer.send(config.GMAIL_USER, f"New inquiry — {name}", body, reply_to=email)
            db.run("UPDATE inquiries SET emailed=1 WHERE id=?", (iid,))
        except Exception as e:
            log.error("inquiry %s stored but email failed: %s", iid, e)
    else:
        log.error("inquiry %s stored — mailer not configured, no email sent", iid)
    log.info("inquiry %s received", iid)
    return templates.TemplateResponse(request, "site/contact.html",
                                      {"sent": True, "error": None})


@router.get("/work", response_class=HTMLResponse)
async def work_index(request: Request):
    return templates.TemplateResponse(request, "site/work_index.html",
                                      {"studies": _case_studies(),
                                       "demo_gallery": _demo_gallery()})


@router.get("/work/{slug}", response_class=HTMLResponse)
async def work_detail(request: Request, slug: str):
    g = db.one("SELECT * FROM galleries WHERE slug=? AND cs_published=1", (slug,))
    if not g:
        raise HTTPException(status_code=404)
    photos = db.all_("""SELECT * FROM assets WHERE gallery_id=? AND portfolio=1
                        AND status='ready' AND kind='photo'
                        ORDER BY position, id""", (g["id"],))
    credits = [ln.strip() for ln in (g["cs_credits"] or "").splitlines()
               if ln.strip()]
    return templates.TemplateResponse(request, "site/work_detail.html",
                                      {"g": g, "photos": photos, "credits": credits,
                                       "testimonials": _testimonials(gallery_id=g["id"])})


@router.get("/reels", response_class=HTMLResponse)
async def reels(request: Request):
    return templates.TemplateResponse(request, "site/reels.html",
                                      {"reels": _portfolio_reels(),
                                       "demo_gallery": _demo_gallery()})


@router.get("/site/img/{asset_id}")
async def portfolio_image(asset_id: int, variant: str = "web"):
    """Unauthenticated — serves ONLY portfolio-flagged, ready photos."""
    if variant not in ("web", "thumb"):
        raise HTTPException(status_code=404)
    a = db.one("""SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='photo'""", (asset_id,))
    if not a:
        raise HTTPException(status_code=404)
    path = (config.MEDIA_DIR / str(a["gallery_id"]) / variant
            / f"{Path(a['stored']).stem}.jpg")
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/site/vid/{asset_id}")
async def portfolio_video(asset_id: int):
    """Unauthenticated — serves ONLY portfolio-flagged, ready videos (the /reels
    showcase). FileResponse handles HTTP Range, so iOS scrubbing works."""
    a = db.one("""SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='video'""", (asset_id,))
    if not a:
        raise HTTPException(status_code=404)
    path = (config.MEDIA_DIR / str(a["gallery_id"]) / "web"
            / f"{Path(a['stored']).stem}.mp4")
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="video/mp4",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/site/poster/{asset_id}")
async def portfolio_video_poster(asset_id: int):
    """Poster frame for a portfolio video — same public portfolio gate."""
    a = db.one("""SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind='video'""", (asset_id,))
    if not a:
        raise HTTPException(status_code=404)
    path = (config.MEDIA_DIR / str(a["gallery_id"]) / "web"
            / f"{Path(a['stored']).stem}_poster.jpg")
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return ("User-agent: *\n"
            "Disallow: /g/\nDisallow: /portal/\nDisallow: /media/\n"
            "Disallow: /admin\nDisallow: /p/\nDisallow: /c/\nDisallow: /i/\n"
            "Allow: /\n"
            f"Sitemap: {config.BASE_URL}/sitemap.xml\n")


@router.get("/sitemap.xml")
async def sitemap():
    paths = ["/", "/portfolio", "/work", "/services", "/about", "/contact",
             "/book", "/reels", "/press"]
    # Case-study detail pages are also surfaced on /portfolio (Featured clients)
    # but get their own crawlable URLs here (/work index + /work/{slug} details).
    paths += [f"/work/{g['slug']}" for g in _case_studies()]
    urls = "".join(f"<url><loc>{config.BASE_URL}{p}</loc></url>" for p in paths)
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>'
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml")
