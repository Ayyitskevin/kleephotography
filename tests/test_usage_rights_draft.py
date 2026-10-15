"""DRAFT usage-rights line on each package card (awaiting Kevin's legal review).

Every tier with a TIER_USAGE entry shows exactly that line on its spoke; a tier
without one shows none. The covered (group, tier) pairs are listed here
independently of the catalog.
"""

import re
import socket

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import TIER_USAGE
from tests.test_specialty_packages import catalog_group, rendered

pytestmark = pytest.mark.integration

SPOKES = {
    "/real-estate": ("real_estate",),
    "/portraits": ("portraits",),
    "/food-beverage": ("photography", "videography", "brand_partner"),
}
COVERED = {
    ("real_estate", "Essentials"),
    ("real_estate", "Signature"),
    ("real_estate", "Premier"),
    ("portraits", "Tier I"),
    ("portraits", "Tier II"),
    ("portraits", "Tier III"),
    ("photography", "Starter"),
    ("photography", "Standard"),
    ("photography", "Premium"),
    ("videography", "Starter"),
    ("videography", "Standard"),
    ("videography", "Premium"),
}


@pytest.fixture
def client(monkeypatch):
    # Public rendering only: no background jobs, calendar query, or network.
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside usage-line GET coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as test_client:
        yield test_client


def usage_lines(article):
    return [
        p.text() for p in article.find("p") if "svc-usage" in (p.attrs.get("class") or "").split()
    ]


def tier_cards(client, path):
    rates = rendered(client, path).one(id="rates")
    for group_key in SPOKES[path]:
        cards = rates.one(id=f"packages-{group_key}").find("article")
        tiers = catalog_group(group_key)["tiers"]
        assert len(cards) == len(tiers)
        yield from (
            (group_key, tier["name"], card) for tier, card in zip(tiers, cards, strict=True)
        )


def test_draft_covers_exactly_the_r11_tiers():
    assert {(g, t) for g, tiers in TIER_USAGE.items() for t in tiers} == COVERED
    for group_key, tier_name in COVERED:
        assert tier_name in [t["name"] for t in catalog_group(group_key)["tiers"]]


@pytest.mark.parametrize("path", list(SPOKES))
def test_each_tier_card_renders_its_usage_line(client, path):
    seen = set()
    for group_key, tier_name, card in tier_cards(client, path):
        expected = TIER_USAGE.get(group_key, {}).get(tier_name)
        if (group_key, tier_name) in COVERED:
            assert usage_lines(card) == [expected], (group_key, tier_name)
            seen.add((group_key, tier_name))
        else:
            assert usage_lines(card) == [], (group_key, tier_name)
    assert seen == {pair for pair in COVERED if pair[0] in SPOKES[path]}


def test_brand_partner_has_no_entry_and_renders_none(client):
    assert "brand_partner" not in TIER_USAGE
    cards = [c for g, _t, c in tier_cards(client, "/food-beverage") if g == "brand_partner"]
    assert len(cards) == 3
    assert all(usage_lines(card) == [] for card in cards)


def test_a_tier_with_its_entry_removed_renders_none(client, monkeypatch):
    monkeypatch.delitem(TIER_USAGE["portraits"], "Tier II")
    by_tier = {t: usage_lines(c) for _g, t, c in tier_cards(client, "/portraits")}
    assert by_tier["Tier II"] == []
    assert by_tier["Tier I"] == [TIER_USAGE["portraits"]["Tier I"]]
    assert by_tier["Tier III"] == [TIER_USAGE["portraits"]["Tier III"]]


def test_draft_lines_stay_consistent_with_the_catalog_and_contracts():
    # Premium tiers already promise a licence in their includes; the line must agree.
    assert "Commercial usage license" in catalog_group("photography")["tiers"][2]["includes"]
    assert "commercial usage license" in TIER_USAGE["photography"]["Premium"].lower()
    assert "Full usage license" in catalog_group("videography")["tiers"][2]["includes"]
    assert "full usage license" in TIER_USAGE["videography"]["Premium"].lower()
    for tiers in TIER_USAGE.values():
        for line in tiers.values():
            # Deliverables stay the studio's property: a line licenses use, never ownership.
            assert not re.search(r"\byou own\b|\bownership\b|copyright", line.lower()), line
    for line in TIER_USAGE["real_estate"].values():
        assert "listing" in line.lower()
