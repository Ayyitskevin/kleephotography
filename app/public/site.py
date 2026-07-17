"""Public marketing site — the only indexable routes (everything else stays noindex).
Inquiry form emails Kevin's inbox so Odysseus inquiry_intake picks it up unchanged."""

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .. import config, db, features, jobs, mailer, security, specialties
from ..render import ROOT, _static_rev, templates
from . import site_catalog as _site_catalog

BOOK_ACTIVE_PROMISES = _site_catalog.BOOK_ACTIVE_PROMISES
BOOK_FAQS = _site_catalog.BOOK_FAQS
BOOK_PROMISES = _site_catalog.BOOK_PROMISES
CONTACT_FAQS = _site_catalog.CONTACT_FAQS
FAQS = _site_catalog.FAQS
SERVICES = _site_catalog.SERVICES
SPECIALTY_PAGES = _site_catalog.SPECIALTY_PAGES
marketing_meta = _site_catalog.marketing_meta

log = logging.getLogger("mise.public.site")
router = APIRouter()

templates.env.globals["faqs"] = FAQS

# Paths the noindex middleware leaves crawlable (matched by exact path or prefix "/").
INDEXABLE = {
    "/",
    "/real-estate",
    "/portraits",
    "/food-beverage",
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


def marketing_page_catalog() -> list[dict[str, str]]:
    """Indexable static routes and their rendered title/description metadata."""
    pages = []
    for path in (
        "/",
        "/real-estate",
        "/portraits",
        "/food-beverage",
        "/portfolio",
        "/work",
        "/services",
        "/about",
        "/contact",
        "/book",
        "/reels",
        "/press",
    ):
        specialty = next(
            (
                page
                for key, page in SPECIALTY_PAGES.items()
                if f"/{specialties.SPECIALTIES[key]['slug']}" == path
            ),
            None,
        )
        if specialty:
            title = f"{specialty['title']} — {config.SITE_NAME}"
            description = specialty["meta"]
        else:
            meta = marketing_meta(path)
            title = meta["title"]
            description = meta["description"]
        pages.append({"path": path, "title": title, "description": description})
    return pages


def _sr_rate_cells(key: str) -> list[dict]:
    """Rate cells for the spoke rails (#rates). Labels are the approved
    Screening Room copy; every dollar figure is read from SERVICES so pricing
    keeps its single source of truth."""

    def tier(gkey: str, i: int) -> dict:
        return next(s for s in SERVICES if s["key"] == gkey)["tiers"][i]

    if key == "re":
        return [
            {"price": tier("real_estate", 0)["display_price"], "label": "Essentials — per listing"},
            {
                "price": tier("real_estate", 1)["display_price"],
                "label": "Signature — photo + reel",
                "pick": True,
            },
            {"price": tier("real_estate", 2)["display_price"], "label": "Premier — photo + film"},
        ]
    if key == "pl":
        return [
            {"price": tier("portraits", 0)["display_price"], "label": "Tier I — one look"},
            {
                "price": tier("portraits", 1)["display_price"],
                "label": "Tier II — two looks",
                "pick": True,
            },
            {"price": tier("portraits", 2)["display_price"], "label": "Tier III — extended"},
        ]
    return [
        {"price": tier("photography", 0)["display_price"], "label": "Photo starter — half day"},
        {
            "price": tier("videography", 0)["display_price"],
            "label": "Reel starter — hero + 3 cutdowns",
        },
        {
            "price": tier("brand_partner", 1)["display_price"],
            "unit": "/mo",
            "label": "Brand partner — photo + reels monthly",
            "pick": True,
        },
    ]


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
    portfolio=1 gate as photos, so nothing client-private is ever exposed.

    No demo fallback: the /site/vid and /site/poster serving routes also gate on
    portfolio=1, so returning non-starred client videos here would render <video>
    players whose src + poster both 404 (and leak private asset IDs into the
    page). When nothing is starred we return [] and the templates fall back to
    their empty states (home hides the motion band; /reels shows the empty copy)."""
    return db.all_("""SELECT a.*, g.client_name, g.title AS gallery_title
                       FROM assets a
                       LEFT JOIN galleries g ON g.id = a.gallery_id
                       WHERE a.portfolio=1 AND a.status='ready' AND a.kind='video'
                       ORDER BY a.id DESC""")


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


def _cs_specialty_map() -> dict[int, str]:
    """gallery_id → specialty key for grouping case studies, derived as the
    majority tag-prefix of each gallery's portfolio-starred assets (unprefixed
    tags = legacy F&B; a gallery with no starred assets stays unmapped and the
    callers' .get() default buckets it F&B — same legacy rule, documented in
    ops/SPECIALTY-LAUNCH.md). Zero-schema by design: the gallery table has no
    category column, and the starred assets are what a case study displays
    anyway, so they're the honest signal of what kind of work it is."""
    # ORDER BY makes ties deterministic: Counter.most_common keeps first-seen
    # on equal counts, so an even split resolves to the oldest starred asset's
    # specialty instead of whatever order SQLite happened to return rows in.
    rows = db.all_("""SELECT gallery_id, portfolio_tag FROM assets
                      WHERE portfolio=1 AND status='ready'
                      ORDER BY gallery_id, id""")
    tags: dict[int, list[str | None]] = {}
    for r in rows:
        tags.setdefault(r["gallery_id"], []).append(r["portfolio_tag"])
    return {
        gid: specialties.infer_specialty(values) or specialties.DEFAULT_KEY
        for gid, values in tags.items()
    }


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


def _contact_scope(service: str) -> tuple[str, str]:
    """Human-facing scope copy while the legacy dish_count field stays stable."""
    if service == "Real Estate":
        return "Listing / property scope", "e.g. 3,200 sq ft · 4 bed 3 bath"
    if service == "Portraits":
        return "Subject / team scope", "e.g. just me · team of 8 · family of 5"
    if service in ("Food & Beverage", "Brand Partner (monthly retainer)"):
        return "Dishes / setups", "e.g. 8–10 dishes · 3 drink setups"
    return "Project scope", "e.g. property size, team size, or number of setups"


# Subtitle under each lobby title card (Screening Room 3a). The RE line gains
# "& aerials" only once the Part 107 cert is live (aerials_live flag).
DOOR_LINES = {
    "re": "real estate — stills & film",
    "re_aerial": "real estate — stills, film & aerials",
    "pl": "portraits — directed, never posed",
    "fb": "food & beverage — the original craft",
}


def _specialty_doors() -> list[dict]:
    """The lobby's three feature title cards: screen name, film-stock billing,
    subtitle, that vertical's newest starred photo as the card artwork (None →
    house-black card), and honest work counts (rendered only when non-zero —
    no vanity zeros)."""
    assets = _portfolio_assets()
    reels = _portfolio_reels()
    doors = []
    for key, meta in specialties.SPECIALTIES.items():
        mine = [a for a in assets if specialties.specialty_key(a["portfolio_tag"]) == key]
        line_key = "re_aerial" if key == "re" and features.aerials_live() else key
        doors.append(
            {
                "key": key,
                "slug": meta["slug"],
                "name": meta["name"],
                "feature": meta["feature"],
                "stock": meta["stock"],
                "screen_name": meta["screen_name"],
                "line": DOOR_LINES[line_key],
                "lead": mine[0] if mine else None,
                "n_photos": len(mine),
                "n_reels": sum(
                    1 for r in reels if specialties.specialty_key(r["portfolio_tag"]) == key
                ),
            }
        )
    return doors


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
            "doors": _specialty_doors(),
        },
    )


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request):
    # photos AND starred videos share the masonry — /services sells motion, so
    # the archive shouldn't be stills-only. Videos play in the same lightbox.
    assets = sorted(
        [*_portfolio_assets(), *_portfolio_reels()], key=lambda r: r["id"], reverse=True
    )
    # distinct tags actually in use, alphabetical — the subject filter chips
    tags = sorted({a["portfolio_tag"] for a in assets if a["portfolio_tag"]}, key=str.lower)
    # specialty chips render only for verticals that actually have starred work
    # (chip = the film-stock label the Screening Room archive filters by)
    sp_counts = Counter(specialties.specialty_key(a["portfolio_tag"]) for a in assets)
    sp_chips = [
        {
            "key": k,
            "name": m["name"],
            "count": sp_counts[k],
            "chip": f"{m['stock']} — {m['screen_name'].removeprefix('The ')}",
        }
        for k, m in specialties.SPECIALTIES.items()
        if sp_counts[k]
    ]
    # Case studies also surface here as a "Featured clients" band; the dedicated
    # /work index + /work/{slug} detail pages are public + crawlable too.
    return templates.TemplateResponse(
        request,
        "site/portfolio.html",
        {
            "assets": assets,
            "tags": tags,
            "sp_chips": sp_chips if len(sp_chips) > 1 else [],
            "studies": _case_studies(),
            "demo_gallery": _demo_gallery(),
        },
    )


def _about_portrait_static() -> str | None:
    """Filename under /static for the About studio portrait, if present.

    Prefers MISE_ABOUT_PORTRAIT when set; otherwise the first of
    about-portrait.{jpg,jpeg,png,webp} that exists on disk. Returns None so
    the template can fall back to a starred portfolio still.
    """
    static = ROOT / "static"
    if config.ABOUT_PORTRAIT:
        name = Path(config.ABOUT_PORTRAIT).name  # basename only — no path escape
        if (static / name).is_file():
            return name
        return None
    for name in (
        "about-portrait.jpg",
        "about-portrait.jpeg",
        "about-portrait.png",
        "about-portrait.webp",
    ):
        if (static / name).is_file():
            return name
    return None


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(
        request,
        "site/about.html",
        {
            "portrait_static": _about_portrait_static(),
            "featured": _portfolio_assets()[:1],
        },
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
    scope_label, scope_placeholder = _contact_scope(pf["service"])
    return templates.TemplateResponse(
        request,
        "site/contact.html",
        {
            "sent": False,
            "error": None,
            "prefill": pf,
            "scope_label": scope_label,
            "scope_placeholder": scope_placeholder,
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
    phone: str = Form(""),
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
        scope_label, scope_placeholder = _contact_scope(service.strip())
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
                    "phone": phone.strip(),
                    "usage": usage.strip(),
                    "budget": budget.strip(),
                },
                "scope_label": scope_label,
                "scope_placeholder": scope_placeholder,
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
    phone = phone.strip()[:40]
    scope_label, _ = _contact_scope(service)
    details = [
        (lbl, v.strip())
        for lbl, v in (
            ("Project type", service),
            ("Target date", shoot_date),
            (scope_label, dish_count),
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
        """INSERT INTO inquiries
                    (name, email, business, message, service, shoot_date, phone)
                    VALUES (?,?,?,?,?,?,?)""",
        (
            name,
            email,
            business.strip() or None,
            full_message,
            service or None,
            shoot_date or None,
            phone,
        ),
    )
    if mailer.configured():
        body = (
            f"New inquiry via kleephotography.com\n\n"
            f"Name: {name}\nEmail: {email}\n"
            f"Phone: {phone or '—'}\n"
            f"Business: {business.strip() or '—'}\n\n{full_message}\n"
        )
        try:
            mailer.send(config.GMAIL_USER, f"New inquiry — {name}", body, reply_to=email)
            db.run("UPDATE inquiries SET emailed=1 WHERE id=?", (iid,))
        except Exception as e:
            log.error("inquiry %s stored but email failed: %s", iid, e)
        acknowledgement = (
            f"Hi {name},\n\n"
            "Thanks for reaching out — your inquiry made it through. I review "
            "every project personally and will reply within one business day "
            "with availability, any follow-up questions, and the best next step.\n\n"
            f"See services and starting rates: {config.BASE_URL}/services\n"
            f"Ready to choose a time? {config.BASE_URL}/book\n\n"
            f"Kevin\n{config.SITE_NAME}\n"
        )
        try:
            mailer.send(email, "Your inquiry is in — what happens next", acknowledgement)
        except Exception as e:
            log.warning("inquiry %s acknowledgement email failed: %s", iid, e)
    else:
        log.error("inquiry %s stored — mailer not configured, no email sent", iid)
    jobs.enqueue("notion_sync_inquiry", {"inquiry_id": iid})
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
    studies = _case_studies()
    csmap = _cs_specialty_map()
    # Group by derived specialty (SPECIALTIES order). Headings only render
    # when more than one group exists — a single-vertical archive stays flat.
    groups = []
    for key, meta in specialties.SPECIALTIES.items():
        rows = [g for g in studies if csmap.get(g["id"], specialties.DEFAULT_KEY) == key]
        if rows:
            groups.append({"name": meta["name"], "slug": meta["slug"], "studies": rows})
    return templates.TemplateResponse(
        request,
        "site/work_index.html",
        {"studies": studies, "groups": groups, "demo_gallery": _demo_gallery()},
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
    specialty_key = _cs_specialty_map().get(g["id"], specialties.DEFAULT_KEY)
    cta = {
        "re": {
            "heading": "Like the look? Let's shoot your listing.",
            "label": "Request a listing quote",
            "service": "Real Estate",
        },
        "pl": {
            "heading": "Like the look? Let's plan your session.",
            "label": "Request a portrait quote",
            "service": "Portraits",
        },
        "fb": {
            "heading": "Like the look? Let's shoot your menu.",
            "label": "Request a food & beverage quote",
            "service": "Food & Beverage",
        },
    }[specialty_key]
    return templates.TemplateResponse(
        request,
        "site/work_detail.html",
        {
            "g": g,
            "photos": photos,
            "credit_items": credit_items,
            "testimonials": testimonials,
            "pull_quote": testimonials[0] if testimonials else None,
            "cta": cta,
        },
    )


@router.get("/reels", response_class=HTMLResponse)
async def reels(request: Request):
    vids = _portfolio_reels()
    sp_counts = Counter(specialties.specialty_key(r["portfolio_tag"]) for r in vids)
    sp_chips = [
        {
            "key": k,
            "name": m["name"],
            "count": sp_counts[k],
            "chip": f"{m['stock']} — {m['screen_name'].removeprefix('The ')}",
        }
        for k, m in specialties.SPECIALTIES.items()
        if sp_counts[k]
    ]
    return templates.TemplateResponse(
        request,
        "site/reels.html",
        {
            "reels": vids,
            "sp_chips": sp_chips if len(sp_chips) > 1 else [],
            "demo_gallery": _demo_gallery(),
        },
    )


def _specialty_page(request: Request, key: str):
    """Shared renderer for the three specialty spokes — same anatomy, distinct
    copy (SPECIALTY_PAGES) and specialty-filtered work/reels/studies."""
    page = SPECIALTY_PAGES[key]
    # CTA deep-link: the moment Kevin creates the conventional event type in
    # the live admin (ops/SPECIALTY-LAUNCH.md slugs), spokes route straight to
    # its picker; until then they land on the /book index. No code redeploy.
    et = db.one("SELECT slug FROM event_types WHERE slug=? AND active=1", (page["book_slug"],))
    book_url = f"/book/{et['slug']}" if et else "/book"
    photos = [
        a for a in _portfolio_assets() if specialties.specialty_key(a["portfolio_tag"]) == key
    ]
    vids = [r for r in _portfolio_reels() if specialties.specialty_key(r["portfolio_tag"]) == key]
    csmap = _cs_specialty_map()
    studies = [g for g in _case_studies() if csmap.get(g["id"], specialties.DEFAULT_KEY) == key]
    # Only quotes tied to this specialty's published studies belong here.
    # General quotes may be F&B-specific; using them as fallback on portraits or
    # real estate silently misattributes the proof.
    quotes: list = []
    for s in studies:
        quotes += _testimonials(gallery_id=s["id"])
    return templates.TemplateResponse(
        request,
        "site/specialty.html",
        {
            "sp": specialties.SPECIALTIES[key],
            "sp_key": key,
            "page": page,
            "photos": photos,
            "reels": vids,
            "hero_reel": vids[0] if vids else None,
            "studies": studies[:4],
            "testimonials": quotes[:3],
            "demo_gallery": _demo_gallery(),
            "book_url": book_url,
            "faqs": page["faqs"],
            "faq_heading": "Good to know",
            "rates": _sr_rate_cells(key),
            "aerial_rate": specialties.aerial_pass_display(),
        },
    )


@router.get("/real-estate", response_class=HTMLResponse)
async def specialty_real_estate(request: Request):
    return _specialty_page(request, "re")


@router.get("/portraits", response_class=HTMLResponse)
async def specialty_portraits(request: Request):
    return _specialty_page(request, "pl")


@router.get("/food-beverage", response_class=HTMLResponse)
async def specialty_food_beverage(request: Request):
    return _specialty_page(request, "fb")


@router.get("/site/img/{asset_id}")
async def portfolio_image(asset_id: int, variant: str = "web"):
    """Unauthenticated — serves ONLY portfolio-flagged, ready photos."""
    if variant not in ("web", "thumb"):
        raise HTTPException(status_code=404)
    # thumb serves both kinds (the transcode job writes thumb jpgs for videos —
    # the /portfolio masonry needs them); web stays photo-only, a video's web
    # rendition is the mp4 behind /site/vid.
    kinds = ("photo", "video") if variant == "thumb" else ("photo",)
    a = db.one(
        f"""SELECT * FROM assets WHERE id=? AND portfolio=1
                  AND status='ready' AND kind IN ({",".join("?" * len(kinds))})""",
        (asset_id, *kinds),
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


@router.get("/.well-known/security.txt", response_class=PlainTextResponse)
async def security_txt():
    """RFC 9116 — tells security researchers where to report vulnerabilities
    instead of leaving them to guess (or post publicly). Expires is required by
    the RFC and must stay under a year out; rendering it live keeps the file
    from silently going stale."""
    contact = (
        f"mailto:{config.CONTACT_EMAIL}" if config.CONTACT_EMAIL else f"{config.BASE_URL}/contact"
    )
    expires = (datetime.now(UTC) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"Contact: {contact}\n"
        f"Expires: {expires}\n"
        "Preferred-Languages: en\n"
        f"Canonical: {config.BASE_URL}/.well-known/security.txt\n"
    )


def _sitemap_day(value) -> str:
    """W3C Datetime date portion (YYYY-MM-DD) for <lastmod>."""
    if value is None or value == "":
        return datetime.now(UTC).strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%d")
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m-%d")
        )
    except ValueError:
        return datetime.now(UTC).strftime("%Y-%m-%d")


def _sitemap_url(path: str, lastmod: str) -> str:
    return f"<url><loc>{config.BASE_URL}{path}</loc><lastmod>{lastmod}</lastmod></url>"


@router.get("/sitemap.xml")
async def sitemap():
    # Static marketing paths share the newest /static asset mtime — a cheap
    # freshness signal that moves on every CSS/JS deploy without inventing
    # per-page edit dates we don't store.
    shell_day = _sitemap_day(_static_rev())
    paths = [
        "/",
        "/real-estate",
        "/portraits",
        "/food-beverage",
        "/portfolio",
        "/work",
        "/services",
        "/about",
        "/contact",
        "/book",
        "/reels",
        "/press",
    ]
    urls = "".join(_sitemap_url(p, shell_day) for p in paths)
    # Case-study detail pages are also surfaced on /portfolio (Featured clients)
    # but get their own crawlable URLs here (/work index + /work/{slug} details).
    # Prefer created_at so publishing a study bumps lastmod for crawlers.
    for g in _case_studies():
        urls += _sitemap_url(f"/work/{g['slug']}", _sitemap_day(g["created_at"]))
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )
