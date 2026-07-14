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
