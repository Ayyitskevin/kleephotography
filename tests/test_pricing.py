"""Unit tests for pricing.suggest_license_fee (pure logic, no DB).

Extracted from the monolithic smoke for better modularity (plan item).
"""

import pytest

from app import pricing


@pytest.mark.unit
def test_suggest_asheville_standard():
    row = {
        "usage_tier": "standard",
        "territory": "[]",
        "channels": "[]",
        "perpetual": 0,
        "starts_on": "2026-01-01",
        "ends_on": "2026-12-31",
        "exclusivity": "none",
    }
    res = pricing.suggest_license_fee(row, "asheville")
    assert res["market"] == "asheville"
    assert res["base_cents"] == 27500
    assert res["total_cents"] == 27500  # base * 1s


def test_suggest_with_territory_and_channels():
    row = {
        "usage_tier": "standard",
        "territory": '["US"]',  # 1.4
        "channels": '["social_paid"]',  # +0.20
        "perpetual": 0,
        "starts_on": "2026-01-01",
        "ends_on": "2026-12-31",
        "exclusivity": "none",
    }
    res = pricing.suggest_license_fee(row)
    # 27500 * 1.4 * 1.2 = 46200
    assert res["total_cents"] == 46200


def test_suggest_exclusive_and_perpetual():
    row = {
        "usage_tier": "standard",
        "territory": "[]",
        "channels": "[]",
        "perpetual": 1,
        "starts_on": None,
        "ends_on": None,
        "exclusivity": "exclusive",
    }
    res = pricing.suggest_license_fee(row)
    # base * perpetual 2.0 * excl 1.8 (for standard)
    assert res["total_cents"] == round(27500 * 2.0 * 1.8)


def test_suggest_raleigh_base():
    row = {
        "usage_tier": "standard",
        "territory": "[]",
        "channels": "[]",
        "perpetual": 0,
        "starts_on": "2026-01-01",
        "ends_on": "2026-12-31",
        "exclusivity": "none",
    }
    res = pricing.suggest_license_fee(row, "raleigh")
    assert res["base_cents"] == 35000
