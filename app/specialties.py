"""Specialty taxonomy for the three-vertical flagship site.

Real estate / portrait & lifestyle / food & beverage are derived from the
existing free-text assets.portfolio_tag column via a prefix convention —
're/exteriors', 'pl/headshots', 'fb/dishes' — so the public site can group
work per specialty with NO schema change. Unprefixed tags ('dishes',
'drinks', …) are legacy Food & Beverage: everything starred before the
revamp is F&B work, so it resolves to 'fb' without any re-tagging pass.

Single source of truth: the marketing routes (app/public/site.py), image
alt text (app/render.py), and the admin tag suggestions all read from here.
"""

SPECIALTIES: dict[str, dict] = {
    # key → public page slug, display name, craft phrase for <img alt> text,
    # plus the Screening Room vocabulary: feature billing, film-stock code, and
    # marquee screen name (shared by the lobby cards, spoke navs, archive
    # chips, and the admin deck so the language never drifts per surface).
    # Order matters: nav, homepage doors, and /work groups render in this order.
    "re": {
        "slug": "real-estate",
        "name": "Real Estate",
        "craft": "real estate photography",
        "feature": "Feature 01",
        "stock": "250D",
        "screen_name": "The Space",
    },
    "pl": {
        "slug": "portraits",
        "name": "Portrait & Lifestyle",
        "craft": "portrait & lifestyle photography",
        "feature": "Feature 02",
        "stock": "800T",
        "screen_name": "The Face",
    },
    "fb": {
        "slug": "food-beverage",
        "name": "Food & Beverage",
        "craft": "food & beverage photography",
        "feature": "Feature 03",
        "stock": "500T",
        "screen_name": "The Plate",
    },
}

DEFAULT_KEY = "fb"  # unprefixed tags = legacy F&B (everything pre-revamp)

# The Aerial Pass add-on rate, in ONE place (booking intake label, the
# /real-estate band, proposal copy all read this). $150 set by Kevin
# 2026-07-13 (ops/SPECIALTY-LAUNCH.md carries the launch checklist).
AERIAL_PASS_CENTS = 15000


def aerial_pass_display() -> str:
    """'+$150' — the add-on chip wherever the Aerial Pass is offered."""
    return f"+${AERIAL_PASS_CENTS // 100}"


def split_tag(tag: str | None) -> tuple[str, str]:
    """'re/exteriors' → ('re', 'exteriors'); 'dishes' → ('fb', 'dishes').

    Only a known specialty prefix is treated as one — an unrecognized prefix
    stays part of the label ('behind/scenes' → ('fb', 'behind/scenes')) so a
    stray slash never mis-buckets work into the wrong vertical.
    """
    t = (tag or "").strip()
    if "/" in t:
        prefix, _, label = t.partition("/")
        if prefix.strip().lower() in SPECIALTIES:
            return prefix.strip().lower(), label.strip()
    return DEFAULT_KEY, t


def specialty_key(tag: str | None) -> str:
    """Specialty key ('re'/'pl'/'fb') for a portfolio tag."""
    return split_tag(tag)[0]


def tag_label(tag: str | None) -> str:
    """Display label for a tag with any specialty prefix stripped."""
    return split_tag(tag)[1]


def by_slug(slug: str) -> tuple[str, dict] | None:
    """Resolve a public page slug ('real-estate') to (key, meta), or None."""
    for key, meta in SPECIALTIES.items():
        if meta["slug"] == slug:
            return key, meta
    return None
