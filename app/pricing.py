"""Usage-license fee suggestion. Pure: no DB import. A license row carries the
usage parameters (tier, territory, channels, term, exclusivity); this maps them
to a SUGGESTED licensing fee in cents. `fee_cents` on the row stays the
human-committed number — this only suggests, it never sets the price.

Base rates are per-MARKET; the usage multipliers are market-independent doctrine.
Asheville is Kevin's primary market (the value market, ~32% below US national avg
per 2026 regional research); Charlotte and Raleigh bases get added to
MARKET_BASE_CENTS when he expands — same multipliers, zero other change.
Full doctrine + market research: ORACLE mise.md, Notion KLP CRM pricing page.
"""

import json
import math

DEFAULT_MARKET = "asheville"

# Base license fee (cents) by usage_tier, per market. Licensing portion only —
# the creative/shoot fee is billed separately on the invoice. Charlotte/Raleigh
# bases were derived from the same 2026 regional research as Asheville: scale the
# Asheville bases by the market's position (Charlotte premium ~1.5×, Raleigh mid
# ~1.25×), rounded to clean $25. Kevin approved the figures 2026-06-13.
MARKET_BASE_CENTS = {
    "asheville": {
        "standard": 27500,
        "extended": 60000,
        "exclusive": 170000,
        "unpublished_commercial": 20000,
    },
    "raleigh": {
        "standard": 35000,
        "extended": 75000,
        "exclusive": 210000,
        "unpublished_commercial": 25000,
    },
    "charlotte": {
        "standard": 42500,
        "extended": 90000,
        "exclusive": 255000,
        "unpublished_commercial": 30000,
    },
}

# The live market vocabulary — single source for the client-editor dropdown and
# route validation. Order = how they list in the UI (primary first).
MARKETS = ("asheville", "raleigh", "charlotte")

# Scope of distribution. Take the MAX over selected territories.
TERRITORY_MULT = {
    "local_metro": 1.0,
    "US": 1.4,
    "UK": 1.4,
    "EU": 1.4,
    "north_america": 1.8,
    "worldwide": 2.5,
}
# `website` is included in the base; every other selected channel adds an uplift.
# Split because a billboard is not an email: paid/print/broadcast/OOH/delivery
# command far more than organic web/social (2026 licensing research).
LIGHT_CHANNELS = {"social_organic", "email", "menu", "pr_editorial"}
HEAVY_CHANNELS = {"social_paid", "ooh_billboard", "print", "delivery_apps", "broadcast"}
LIGHT_UPLIFT = 0.08
HEAVY_UPLIFT = 0.20

PERPETUAL_MULT = 2.0
EXTRA_YEAR_UPLIFT = 0.25  # base covers year 1; each additional year adds this
EXCLUSIVE_MULT = 1.8


def _territory_mult(territories) -> float:
    mults = [TERRITORY_MULT[t] for t in territories if t in TERRITORY_MULT]
    return max(mults) if mults else 1.0


def _channel_mult(channels) -> float:
    uplift = 0.0
    for c in channels:
        if c in HEAVY_CHANNELS:
            uplift += HEAVY_UPLIFT
        elif c in LIGHT_CHANNELS:
            uplift += LIGHT_UPLIFT
    return 1.0 + uplift


def _term_mult(row) -> float:
    if row["perpetual"]:
        return PERPETUAL_MULT
    start, end = row["starts_on"], row["ends_on"]
    if start and end and end > start:
        days = (_date(end) - _date(start)).days
        years = max(1, math.ceil(days / 365))
        return 1.0 + EXTRA_YEAR_UPLIFT * (years - 1)
    return 1.0


def _date(s):
    from datetime import date

    return date.fromisoformat(s[:10])


def suggest_license_fee(row, market: str = DEFAULT_MARKET) -> dict:
    """Suggested usage-license fee for a license row. Returns a breakdown dict:
    {market, base_cents, territory_mult, channel_mult, term_mult, excl_mult,
     total_cents}. Deterministic and pure — same row always yields the same fee."""
    base_by_tier = MARKET_BASE_CENTS.get(market, MARKET_BASE_CENTS[DEFAULT_MARKET])
    tier = row["usage_tier"]
    base = base_by_tier.get(tier, base_by_tier["standard"])

    territories = json.loads(row["territory"] or "[]")
    channels = json.loads(row["channels"] or "[]")
    t_mult = _territory_mult(territories)
    c_mult = _channel_mult(channels)
    term_mult = _term_mult(row)
    # The 'exclusive' tier already prices category lockout, so the exclusivity
    # flag does not additionally stack on it (avoids sticker-shock double-count).
    excl_mult = EXCLUSIVE_MULT if row["exclusivity"] == "exclusive" and tier != "exclusive" else 1.0

    total = round(base * t_mult * c_mult * term_mult * excl_mult)
    return {
        "market": market,
        "base_cents": base,
        "territory_mult": t_mult,
        "channel_mult": c_mult,
        "term_mult": term_mult,
        "excl_mult": excl_mult,
        "total_cents": total,
    }
