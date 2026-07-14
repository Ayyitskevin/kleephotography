"""Keep admin proposal PRESET paid totals aligned with public SERVICES."""

import pytest

from app.admin.proposals import PRESETS
from app.public.site_catalog import SERVICES

pytestmark = pytest.mark.unit

# Public service key + tier name → admin preset key. Brand Sessions are
# admin-only and intentionally omitted from this sync contract.
SERVICE_PRESET_MAP = {
    ("real_estate", "Essentials"): "realestate_essentials",
    ("real_estate", "Signature"): "realestate_signature",
    ("real_estate", "Premier"): "realestate_premier",
    ("portraits", "Tier I"): "portrait_starter",
    ("portraits", "Tier II"): "portrait_standard",
    ("portraits", "Tier III"): "portrait_premium",
    ("photography", "Starter"): "photo_starter",
    ("photography", "Standard"): "photo_standard",
    ("photography", "Premium"): "photo_premium",
    ("videography", "Starter"): "video_starter",
    ("videography", "Standard"): "video_standard",
    ("videography", "Premium"): "video_premium",
    ("brand_partner", "Photo"): "retainer_starter",
    ("brand_partner", "Photo + Reels"): "retainer_standard",
    ("brand_partner", "Two-day"): "retainer_premium",
}


def _preset_total(key: str) -> int:
    return sum(i["qty"] * i["unit_cents"] for i in PRESETS[key]["items"])


@pytest.mark.unit
def test_public_services_have_matching_preset_totals():
    for service in SERVICES:
        for tier in service["tiers"]:
            preset_key = SERVICE_PRESET_MAP[(service["key"], tier["name"])]
            assert _preset_total(preset_key) == tier["price_cents"], (
                service["key"],
                tier["name"],
                preset_key,
            )


@pytest.mark.unit
def test_service_preset_map_covers_every_public_tier():
    expected = {(s["key"], t["name"]) for s in SERVICES for t in s["tiers"]}
    assert set(SERVICE_PRESET_MAP) == expected
