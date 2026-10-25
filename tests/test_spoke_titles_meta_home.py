"""Spoke titles name the city; spoke meta descriptions carry the catalog price floor.

The floor is computed from SERVICES in these tests, never typed, so a catalog
price change moves the rendered meta with it.
"""

from __future__ import annotations

import html
import re

import pytest
from fastapi.testclient import TestClient

from app import specialties
from app.main import app
from app.public import site_catalog

CITY = "Asheville"
HOME_LINE = "Photography & film for real estate, portraits, and food & beverage, Asheville & Western North Carolina."
# The catalog groups each spoke's package comparison shows (site.py _specialty_package_groups).
SPOKE_GROUPS = {
    "re": ("real_estate",),
    "pl": ("portraits",),
    "fb": ("photography", "videography", "brand_partner"),
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _floor_text(key: str) -> str:
    """'$250': the lowest tier price across the spoke's groups, read from the catalog."""
    cents = min(
        tier["price_cents"]
        for group in site_catalog.SERVICES
        if group["key"] in SPOKE_GROUPS[key]
        for tier in group["tiers"]
    )
    return f"${cents // 100:,}"


def _rendered_title(text: str) -> str:
    return html.unescape(re.search(r"<title>(.*?)</title>", text, re.S).group(1))


def _rendered_meta(text: str) -> str:
    return html.unescape(re.search(r'<meta name="description" content="([^"]*)"', text).group(1))


@pytest.mark.unit
@pytest.mark.parametrize("key", ["re", "pl", "fb"])
def test_spoke_catalog_title_names_the_city(key):
    assert CITY in site_catalog.SPECIALTY_PAGES[key]["title"]


@pytest.mark.integration
@pytest.mark.parametrize("key", ["re", "pl", "fb"])
def test_spoke_rendered_title_names_the_city(client, key):
    slug = specialties.SPECIALTIES[key]["slug"]
    r = client.get(f"/{slug}")
    assert r.status_code == 200
    assert CITY in _rendered_title(r.text)


@pytest.mark.unit
@pytest.mark.parametrize("key", ["re", "pl", "fb"])
def test_spoke_catalog_meta_ends_with_the_catalog_price_floor(key):
    assert site_catalog.SPECIALTY_PAGES[key]["meta"].endswith(f"From {_floor_text(key)}.")


@pytest.mark.integration
@pytest.mark.parametrize("key", ["re", "pl", "fb"])
def test_spoke_rendered_meta_carries_the_catalog_price_floor(client, key):
    slug = specialties.SPECIALTIES[key]["slug"]
    r = client.get(f"/{slug}")
    assert r.status_code == 200
    assert f"From {_floor_text(key)}." in _rendered_meta(r.text)


@pytest.mark.integration
def test_home_keeps_the_brand_h1_and_says_what_and_where_under_it(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<h1>The space. The face.<br>The plate.</h1>" in r.text
    text = html.unescape(r.text)
    assert HOME_LINE in text
    assert text.index(HOME_LINE) > text.index("The plate.")
