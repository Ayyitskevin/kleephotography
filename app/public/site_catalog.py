"""Shared metadata for indexable static marketing pages."""

from .. import config

MARKETING_PAGE_META = {
    "/": {
        "title": "{site_name} — Real Estate · Portraits · Food & Beverage",
        "description": (
            "Photography & film studio in Asheville, NC — real estate listings, "
            "portraits & personal branding, and the food & beverage work that made "
            "the name. Photo and video, delivered fast."
        ),
    },
    "/portfolio": {
        "title": "The Archive — {site_name}",
        "description": (
            "Selected work across real estate, portraits, and food & beverage — "
            "exteriors, faces, dishes, and the rooms they live in, shot for listings, "
            "brands, and menus."
        ),
    },
    "/work": {
        "title": "Work — {site_name}",
        "description": (
            "Client case studies across real estate, portraits, and food & beverage — "
            "what we shot, and what happened after it went live."
        ),
    },
    "/services": {
        "title": "Services — {site_name}",
        "description": (
            "Photography, videography, and monthly Brand Partner retainers for real "
            "estate, portraits, and food & beverage — Asheville and Western North "
            "Carolina. Three tiers per category."
        ),
    },
    "/about": {
        "title": "About — {site_name}",
        "description": (
            "Meet Kevin Lee — photographer & filmmaker in Asheville, NC, shooting real "
            "estate, portraits, and food & beverage across Western North Carolina."
        ),
    },
    "/contact": {
        "title": "Contact — {site_name}",
        "description": (
            "Request a quote — tell me about your listing, portrait session, or food & "
            "beverage project and get a tailored proposal within a business day."
        ),
    },
    "/book": {
        "title": "Book a time — {site_name}",
        "description": (
            "Book a call or a shoot — pick a date and get an instant confirmation with "
            "a calendar invite."
        ),
    },
    "/reels": {
        "title": "Reels — {site_name}",
        "description": (
            "Short-form video across real estate, portraits, and food & beverage — "
            "walkthrough reels, brand films, and social motion by {site_name}. "
            "Asheville & Western North Carolina."
        ),
    },
    "/press": {
        "title": "Press — {site_name}",
        "description": (
            "Where the work has run — press features and publications covering "
            "{site_name}, in print and online."
        ),
    },
}


def marketing_meta(path: str) -> dict[str, str]:
    """Return formatted metadata for one static marketing route."""
    return {
        key: value.format(site_name=config.SITE_NAME)
        for key, value in MARKETING_PAGE_META[path].items()
    }


# Public services + tier cards for /services, grouped by specialty. Board
# dollars (display_price == price_cents / 100) are the value-first starter menu;
# mirrored admin proposal PRESET paid lines use the same price_cents. Final
# quotes stay tailored per client. The booking dropdown itself stays price-free.
# Order matters: the public page renders these in nav order.
# contact_service maps each group onto the /contact form's Project type
# options. The F&B groups keep their pre-revamp keys (photography/videography)
# so the #svc-… anchors linked from reels/footer never break.
SERVICES = [
    {
        "key": "real_estate",
        "title": "Real Estate",
        "contact_service": "Real Estate",
        "tagline": "Listings, photo and film — MLS-ready stills, twilight "
        "exteriors, and walkthrough reels that move buyers.",
        "monthly": False,
        "tiers": [
            {
                "name": "Essentials",
                "subtitle": "Per listing",
                "display_price": "$250",
                "price_cents": 25000,
                "includes": [
                    "Up to 1.5 hours on site",
                    "25 edited, MLS-ready images",
                    "Web + print resolution",
                    "Private gallery delivery",
                ],
            },
            {
                "name": "Signature",
                "subtitle": "Photo + reel",
                "display_price": "$450",
                "price_cents": 45000,
                "includes": [
                    "Up to 2.5 hours on site",
                    "40 edited, MLS-ready images",
                    "Twilight exterior set",
                    "Vertical walkthrough reel, 30–60s",
                    "Private gallery delivery",
                ],
            },
            {
                "name": "Premier",
                "subtitle": "Photo + film",
                "display_price": "$850",
                "price_cents": 85000,
                "includes": [
                    "Extended session — photo & film",
                    "60 edited, MLS-ready images",
                    "Walkthrough film, 1–2 min + reel",
                    "Twilight exterior set",
                    "Full-res files for print",
                ],
            },
        ],
    },
    {
        "key": "portraits",
        "title": "Portrait & Lifestyle",
        "contact_service": "Portraits",
        "tagline": "Headshots, personal branding, families — directed, not "
        "posed, and delivered ready for wherever they're going.",
        "monthly": False,
        "tiers": [
            {
                "name": "Tier I",
                "subtitle": "One look",
                "display_price": "$350",
                "price_cents": 35000,
                "includes": [
                    "~1 hour session, one look",
                    "10 edited portraits",
                    "Studio or on-location",
                    "Private gallery delivery",
                ],
            },
            {
                "name": "Tier II",
                "subtitle": "Two looks",
                "display_price": "$600",
                "price_cents": 60000,
                "includes": [
                    "~2 hour session, two looks",
                    "25 edited portraits",
                    "Wardrobe + location guidance",
                    "Private gallery delivery",
                ],
            },
            {
                "name": "Tier III",
                "subtitle": "Extended",
                "display_price": "$850",
                "price_cents": 85000,
                "includes": [
                    "~3 hour session, multiple looks",
                    "40 edited portraits",
                    "On-location options",
                    "Rush turnaround available",
                ],
            },
        ],
    },
    {
        "key": "photography",
        "title": "Food & Beverage — Photography",
        "contact_service": "Food & Beverage",
        "tagline": "Menu refreshes, launch weeks, and the rooms that sell the "
        "plate — private same-week gallery with social crops baked in.",
        "monthly": False,
        "tiers": [
            {
                "name": "Starter",
                "subtitle": "Half day",
                "display_price": "$750",
                "price_cents": 75000,
                "includes": [
                    "Up to 3 hours on site, one location",
                    "25 edited finals",
                    "Social crops — 1:1 · 4:5 · 9:16",
                    "One revision round",
                    "Same-week gallery delivery",
                ],
            },
            {
                "name": "Standard",
                "subtitle": "Full day",
                "display_price": "$1,400",
                "price_cents": 140000,
                "includes": [
                    "Up to 6 hours — menu, drinks & room",
                    "50 edited finals",
                    "Full social crop pack",
                    "Two revision rounds",
                    "Brand library starter set",
                ],
            },
            {
                "name": "Premium",
                "subtitle": "Extended",
                "display_price": "$2,600",
                "price_cents": 260000,
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
        "title": "Food & Beverage — Videography",
        "contact_service": "Food & Beverage",
        "tagline": "Social cutdowns that earn the scroll, plus hero brand films "
        "for launches, openings, and campaign weeks.",
        "monthly": False,
        "tiers": [
            {
                "name": "Starter",
                "subtitle": "The reel",
                "display_price": "$1,250",
                "price_cents": 125000,
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
                "display_price": "$2,200",
                "price_cents": 220000,
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
                "price_cents": 390000,
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
        "contact_service": "Brand Partner (monthly retainer)",
        "tagline": "A monthly content rhythm for kitchens that need a steady "
        "feed — built-in discount versus ad-hoc, cancel anytime.",
        "monthly": True,
        "tiers": [
            {
                "name": "Photo",
                "subtitle": "per month",
                "display_price": "$1,100",
                "price_unit": "/mo",
                "price_cents": 110000,
                "includes": [
                    "One half-day shoot monthly",
                    "25 finals each month",
                    "Social crop pack",
                    "Rolling content calendar",
                    "Cancel anytime",
                ],
            },
            {
                "name": "Photo + Reels",
                "subtitle": "per month",
                "display_price": "$1,850",
                "price_unit": "/mo",
                "price_cents": 185000,
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
                "display_price": "$3,200",
                "price_unit": "/mo",
                "price_cents": 320000,
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

BOOK_ACTIVE_PROMISES = [
    "Instant confirmation",
    "Calendar invite in your inbox",
    "Prep details before the shoot",
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
        "book_slug": "re-shoot",
        "empty_head": "Real estate work is being curated for the site.",
        "empty_body": "The listing pipeline is open — MLS-ready stills, twilight "
        "exteriors, and walkthrough reels land here as they're delivered.",
        "cta_h1": "Got a listing",
        "cta_h2": "going live?",
        # Screening Room (3b — "a listing premiere"): the walkthrough film IS
        # the page. Chapter times are fractions of the real hero film's
        # duration (no chapter schema); the aerial chapter and spec-line
        # segment ride the aerials_live flag.
        "sr": {
            "cta": "Book a listing",
            "h2a": "Listings worth",
            "h2b": "the drive.",
            "spec": ["MLS next-day", "Film in 48h"],
            "spec_aerial": "Aerial Pass {rate} — FAA Part 107",
            "chapters": [
                {"at": 0.0, "label": "The approach"},
                {"at": 0.21, "label": "Kitchen & great room"},
                {"at": 0.48, "label": "Primary suite"},
                {"at": 0.62, "label": "Aerial orbit", "aerial": True},
                {"at": 0.81, "label": "Twilight close"},
            ],
            "agents_cut": "vertical for social + hero for the listing page, "
            "delivered together, 48 hours after the shoot.",
            "agents_cut_aerial": "aerial establishing + vertical for social + "
            "hero for the listing page, delivered together, 48 hours after "
            "the shoot.",
            "strip_label": "Stills — frames pulled from the film day",
            "proof": [
                ("18h", "avg. MLS turnaround"),
                ("48h", "walkthrough film"),
                ("1 link", "agent + seller share"),
            ],
        },
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
        "book_slug": "pl-session",
        "empty_head": "Portrait sessions are being curated for the site.",
        "empty_body": "Headshots, branding, and family sessions land here as "
        "they're delivered — the calendar is open now.",
        "cta_h1": "Let's make time",
        "cta_h2": "for a session.",
        # Screening Room (3k — reassurance psychology): direction promised up
        # front, the nerves named and answered in the client's own words.
        "sr": {
            "cta": "Book a session",
            "beat": "directed, never posed — you'll always know what to do with your hands",
            "body": "Headshots, personal branding, families, and the moments "
            "in between. Every frame is coached — where to look, when to "
            "move — and delivered ready for wherever it's going: LinkedIn, "
            "the company site, the mantel.",
            "stats": [
                ("10 min", "until most people relax"),
                ("Same week", "private gallery"),
            ],
            "nerves": [
                (
                    "“I'm awkward in front of a camera.”",
                    "That's most people — and my favorite kind of session. "
                    "Everything is directed; you're never left posing in silence.",
                ),
                (
                    "“What do I even wear?”",
                    "Solid colors you already feel good in beat anything bought "
                    "for the shoot. Bring one backup look; we pick together.",
                ),
                (
                    "“Can you do our whole team?”",
                    "One visit, matched lighting and framing — new hires slot in "
                    "later without the team page looking stitched together.",
                ),
            ],
            "frames_label": "Selected sessions — none of them born camera-ready",
        },
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
        "book_slug": "fb-shoot",
        "empty_head": "New food & beverage work is being curated for the site.",
        "empty_body": "Menus, pours, and the rooms they live in land here as they're delivered.",
        "cta_h1": "Let's make your food look",
        "cta_h2": "the way it tastes.",
        # Screening Room (3c + 3l): full-bleed opening title card, appetite
        # grid, the process told as courses.
        "sr": {
            "cta": "Book a shoot",
            "presents": "Kevin Lee presents — Food & Beverage",
            "h2a": "Your menu,",
            "h2b": "selling itself.",
            "beat": "the steam, the pour, the first cut — in the thirty seconds a dish looks alive",
            "card_meta_l": "Stock 500T — appetite grade",
            "card_meta_r": "Same-week gallery · crops 1:1 / 4:5 / 9:16",
            "grid_label": "Recent menus — plates, pours & the rooms they live in",
            "courses": [
                (
                    "First course — Scout",
                    "We walk the menu together, agree the hero dishes and "
                    "drinks, and build the shot list around your service window.",
                ),
                (
                    "Main — Shoot fast",
                    "Natural light first, styling honest, shot in the thirty "
                    "seconds a dish looks alive. The kitchen never waits.",
                ),
                (
                    "Dessert — Same week",
                    "A private premiere within the week — full-res downloads, "
                    "circled takes, crops already cut for every platform.",
                ),
            ],
            "cta_final": "Book the kitchen's slot",
        },
    },
}
