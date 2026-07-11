"""Public marketing site — the only indexable routes (everything else stays noindex).
Inquiry form emails Kevin's inbox so Odysseus inquiry_intake picks it up unchanged."""

import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .. import config, db, mailer, security, specialties
from ..render import ROOT, templates

log = logging.getLogger("mise.public.site")
router = APIRouter()

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
        "It depends on scope — a single listing, a half-day menu refresh, and a "
        "multi-day campaign sit far apart. Send a few details and I will quote "
        "the right tier; full ranges live on the Services page.",
    ),
    (
        "How soon can you shoot?",
        "Usually within two to three weeks, sometimes sooner for a launch or a "
        "listing going live. Tell me your target date and I will be honest "
        "about what is open.",
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

# ── Specialty spokes ─────────────────────────────────────────────────────────
# Copy for the three specialty landing pages (/real-estate, /portraits,
# /food-beverage) — the "spokes" of the 3-vertical flagship IA. Work, reels,
# case studies, and testimonials are filtered by the portfolio-tag prefix
# convention (app/specialties.py). The F&B spoke inherits the pre-revamp home
# copy nearly verbatim so the indexed F&B keywords keep a dedicated page when
# the homepage broadens into the studio hub.
SPECIALTY_PAGES = {
    "re": {
        "title": "Real Estate Photography & Video",
        "meta": "Real estate photography and video for Asheville & Western "
        "North Carolina — MLS-ready stills, twilight exteriors, and vertical "
        "walkthrough reels that move listings.",
        "kicker": "Real estate · Asheville & WNC",
        "h1a": "Listings that look",
        "h1b": "worth the drive.",
        "lede": "Photography and film for agents and brokerages across Western "
        "North Carolina — MLS-ready stills, twilight exteriors, and walkthrough "
        "reels that move buyers before the first showing.",
        "pills": [
            "Photo · Video",
            "MLS & Zillow-ready formats",
            "Twilight exteriors",
            "Full-res for print",
        ],
        "hero_caption": "Twilight exterior · Asheville",
        "work_heading": "Selected listings.",
        "work_sub": "Interiors that show, exteriors that stop the scroll.",
        "motion_heading": "Walkthroughs that do the showing.",
        "motion_sub": "Vertical reels for social; hero cuts for the listing page.",
        "deliverables": [
            (
                "Stills that sell the space",
                "Clean, bright interiors and true-color exteriors — composed "
                "for MLS, Zillow, and your listing page, with full resolution "
                "on hand for flyers and print.",
            ),
            (
                "Motion that books showings",
                "Vertical walkthrough reels for social and a hero walkthrough "
                "cut for the listing — the drive-by a buyer takes from their "
                "couch, shot in the same visit as the stills.",
            ),
            (
                "Delivery agents can run with",
                "One private gallery link: preview everything, download in the "
                "sizes each platform wants, and share the same link with your "
                "seller.",
            ),
        ],
        "process": [
            (
                "Book the window",
                "Pick a slot that fits the listing's light — bright-morning "
                "interiors, twilight exteriors. Tell me the go-live date and "
                "I'll work backwards from it.",
            ),
            (
                "Shoot day",
                "I move room to room fast and stage lightly as I go — a tidy "
                "house is most of the battle, and sellers don't need to "
                "disappear for long.",
            ),
            (
                "Gallery, fast",
                "A private gallery with MLS-and-social-ready files sized to "
                "upload anywhere, plus full resolution for print.",
            ),
        ],
        "faqs": [
            (
                "How fast can you turn a listing around?",
                "Tell me the go-live date and I'll be straight about what's "
                "possible — listings move fast and the calendar keeps room for "
                "them. If it's urgent, say so on the booking form.",
            ),
            (
                "Do you shoot video as well as photos?",
                "Yes — vertical walkthrough reels for social and a horizontal "
                "cut for the listing page, shot in the same visit as the "
                "photos. Motion is where buyers linger.",
            ),
            (
                "How should the property be prepped?",
                "Clean, declutter, lights on, blinds open. I stage lightly as "
                "I shoot — a tidy house is most of the battle, and I'll flag "
                "anything worth moving before the camera comes out.",
            ),
            (
                "What formats do I get?",
                "Web-sized files that upload straight to MLS and portals, "
                "social crops for the feed, and full resolution for print — "
                "all in one gallery, all yours to download.",
            ),
        ],
        "contact_service": "Real Estate",
        "cta_h1": "Got a listing",
        "cta_h2": "going live?",
    },
    "pl": {
        "title": "Portrait & Lifestyle Photography",
        "meta": "Portrait and lifestyle photography in Asheville, NC — "
        "headshots, personal branding, families, and golden-hour sessions, "
        "delivered same week in a private gallery.",
        "kicker": "Portrait & lifestyle · Asheville & WNC",
        "h1a": "Look like yourself —",
        "h1b": "on your best day.",
        "lede": "Headshots, personal branding, families, and the moments in "
        "between — directed so nothing feels stiff, and delivered ready for "
        "wherever they're going.",
        "pills": [
            "Headshots & personal branding",
            "Families & couples",
            "Studio or on-location",
            "Golden-hour sessions",
        ],
        "hero_caption": "Golden hour · Asheville",
        "work_heading": "Selected sessions.",
        "work_sub": "Faces on their best day — none of them born camera-ready.",
        "motion_heading": "Motion, in between the stills.",
        "motion_sub": "Short loops that carry the feel a still can't.",
        "deliverables": [
            (
                "Direction, not posing",
                "Every frame is coached — where to look, what to do with your "
                "hands — so the photos read like you on a good day, not a "
                "stock photo.",
            ),
            (
                "A gallery made for choosing",
                "Proof, favorite, and download in one private link — pick the "
                "frames you love and grab them full-res, cropped for LinkedIn, "
                "web, and print.",
            ),
            (
                "Consistent team headshots",
                "Whole teams in one visit with matched lighting and framing — "
                "new hires slot in later without the team page looking "
                "stitched together.",
            ),
        ],
        "process": [
            (
                "Plan the session",
                "A short call to pick looks, locations, and what the photos "
                "are for — LinkedIn, the company site, the mantel.",
            ),
            (
                "The session",
                "Directed and unhurried, in the studio or out in the light. "
                "Most people relax inside ten minutes; the best frames come "
                "right after.",
            ),
            (
                "Same-week gallery",
                "A private gallery to pick favorites and download finals — "
                "with crops sized for profiles, sites, and print.",
            ),
        ],
        "faqs": [
            (
                "I'm awkward in front of a camera — will this work?",
                "That's most people, and it's my favorite kind of session. "
                "Everything is directed: where to look, what to do with your "
                "hands, when to move. You'll never be left posing in silence.",
            ),
            (
                "What should I wear?",
                "Solid colors and things you already feel good in beat "
                "anything bought for the shoot. Bring one backup look and "
                "we'll pick together before we start.",
            ),
            (
                "Where do we shoot?",
                "In the studio in Asheville or on location anywhere the light "
                "is good — your office, a trailhead, downtown at golden hour. "
                "We pick the spot to match where the photos will live.",
            ),
            (
                "Can you photograph our whole team?",
                "Yes — consistent headshots for a full team in one visit, "
                "matched lighting and framing, so the site looks unified even "
                "as new people join.",
            ),
        ],
        "contact_service": "Portraits",
        "cta_h1": "Let's make time",
        "cta_h2": "for a session.",
    },
    "fb": {
        "title": "Food & Beverage Photography & Video",
        # Inherits the pre-revamp home meta so the indexed F&B positioning
        # keeps a dedicated page — see the module comment above.
        "meta": "Photography that makes people hungry — food & beverage "
        "photography and videography for the restaurants, cafés, and bars of "
        "Asheville & Western North Carolina.",
        "kicker": "Food & beverage · Asheville & WNC",
        "h1a": "We make your menu",
        "h1b": "sell itself.",
        "lede": "For the restaurants, cafés, and bars of Western North "
        "Carolina — plates, pours, and the rooms they live in, shot to sell "
        "the seat.",
        "pills": [
            "Menus, dishes & drinks",
            "Reels built for the feed",
            "Social crops 1:1 · 4:5 · 9:16",
            "Same-week gallery",
        ],
        "hero_caption": "Tasting menu · Asheville",
        "work_heading": "Plates that earn the double-take.",
        "work_sub": "A few favorites from recent menus — plates, pours, and "
        "the rooms they live in.",
        "motion_heading": "Motion that earns the scroll.",
        "motion_sub": "The steam. The pour. The first cut. Built vertical for the feed.",
        "deliverables": [
            (
                "Plates at their peak",
                "Menu heroes, drinks, and the rooms they live in — lit "
                "honest, styled light, and shot in the thirty seconds a dish "
                "looks alive.",
            ),
            (
                "Motion that earns the scroll",
                "The steam, the pour, the first cut — vertical reels built "
                "for Instagram and TikTok, shot in the same visit as the "
                "stills.",
            ),
            (
                "Delivery that posts itself",
                "A private same-week gallery: favorites, full-res downloads, "
                "and social crops in 1:1, 4:5, and 9:16 — already sized for "
                "every platform.",
            ),
        ],
        "process": [
            (
                "Scout the menu",
                "We go through the menu together, agree on the hero dishes "
                "and drinks, and build a shot list around your service window.",
            ),
            (
                "Shoot fast, on the day",
                "Natural light first, styling minimal and honest. I work "
                "around the kitchen — most full menus wrap between lunch and "
                "dinner.",
            ),
            (
                "Deliver same week",
                "A private gallery lands within the week: full-res downloads, "
                "favorites, and social crops already sized for every platform.",
            ),
        ],
        "faqs": [
            (
                "Do you style the food?",
                "Light, honest styling is included — the goal is for the dish "
                "to look like what arrives at the table, at its best. For "
                "elaborate set styling I can bring in a stylist and fold it "
                "into the quote.",
            ),
            (
                "What do I actually get, and when?",
                "A private online gallery, usually within the week: "
                "full-resolution downloads, your favorites, and social crops "
                "in 1:1, 4:5, and 9:16 — already sized for every platform.",
            ),
            (
                "Can you shoot during service?",
                "Around it, always — we plan the shot list so the kitchen "
                "never waits on a photographer, and most full menus wrap "
                "between lunch and dinner.",
            ),
            (
                "Do you shoot video too?",
                "Yes — steam, pours, and first cuts built vertical for the "
                "feed, plus brand films for launches. Photo and motion in the "
                "same visit is the most efficient way to buy either.",
            ),
        ],
        "contact_service": "Food & Beverage",
        "cta_h1": "Let's make your food look",
        "cta_h2": "the way it tastes.",
    },
}


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
    tags = legacy F&B). Zero-schema by design: the gallery table has no
    category column, and the starred assets are what a case study displays
    anyway, so they're the honest signal of what kind of work it is."""
    rows = db.all_("""SELECT gallery_id, portfolio_tag FROM assets
                      WHERE portfolio=1 AND status='ready'""")
    votes: dict[int, Counter] = defaultdict(Counter)
    for r in rows:
        votes[r["gallery_id"]][specialties.specialty_key(r["portfolio_tag"])] += 1
    return {gid: c.most_common(1)[0][0] for gid, c in votes.items()}


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


# One-liner under each homepage specialty door. The F&B line keeps the
# long-standing "makes people hungry" phrase on the home page.
DOOR_LINES = {
    "re": "MLS-ready stills and walkthrough reels that move listings.",
    "pl": "Headshots, branding, and families — directed, not posed.",
    "fb": "Menus, pours, and rooms — photography that makes people hungry.",
}


def _specialty_doors() -> list[dict]:
    """The homepage's three specialty doors: name, one-liner, and that
    vertical's newest starred photo as the lead image (None → slate frame)."""
    assets = _portfolio_assets()
    doors = []
    for key, meta in specialties.SPECIALTIES.items():
        lead = next(
            (a for a in assets if specialties.specialty_key(a["portfolio_tag"]) == key),
            None,
        )
        doors.append(
            {
                "key": key,
                "slug": meta["slug"],
                "name": meta["name"],
                "line": DOOR_LINES[key],
                "lead": lead,
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
    assets = _portfolio_assets()
    # distinct tags actually in use, alphabetical — the subject filter chips
    tags = sorted({a["portfolio_tag"] for a in assets if a["portfolio_tag"]}, key=str.lower)
    # specialty chips render only for verticals that actually have starred work
    sp_counts = Counter(specialties.specialty_key(a["portfolio_tag"]) for a in assets)
    sp_chips = [
        {"key": k, "name": m["name"], "count": sp_counts[k]}
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
    vids = _portfolio_reels()
    sp_counts = Counter(specialties.specialty_key(r["portfolio_tag"]) for r in vids)
    sp_chips = [
        {"key": k, "name": m["name"], "count": sp_counts[k]}
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
    photos = [
        a for a in _portfolio_assets() if specialties.specialty_key(a["portfolio_tag"]) == key
    ]
    vids = [r for r in _portfolio_reels() if specialties.specialty_key(r["portfolio_tag"]) == key]
    csmap = _cs_specialty_map()
    studies = [g for g in _case_studies() if csmap.get(g["id"], specialties.DEFAULT_KEY) == key]
    # Specialty-scoped quotes first (they ride on this vertical's case
    # studies); top up with general ones so the block never renders thin.
    quotes: list = []
    for s in studies:
        quotes += _testimonials(gallery_id=s["id"])
    if len(quotes) < 3:
        quotes += _testimonials(limit=3 - len(quotes))
    return templates.TemplateResponse(
        request,
        "site/specialty.html",
        {
            "sp": specialties.SPECIALTIES[key],
            "page": page,
            "photos": photos,
            "reels": vids,
            "hero_reel": vids[0] if vids else None,
            "studies": studies[:4],
            "testimonials": quotes[:3],
            "demo_gallery": _demo_gallery(),
            "faqs": page["faqs"],
            "faq_heading": "Good to know",
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


@router.get("/sitemap.xml")
async def sitemap():
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
    # Case-study detail pages are also surfaced on /portfolio (Featured clients)
    # but get their own crawlable URLs here (/work index + /work/{slug} details).
    paths += [f"/work/{g['slug']}" for g in _case_studies()]
    urls = "".join(f"<url><loc>{config.BASE_URL}{p}</loc></url>" for p in paths)
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )
