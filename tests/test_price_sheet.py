"""The approved price sheet (Muse ruling 1, 2026-10-15) on the public pages.

One pin test holds the approved figures as literals (PRICE-SHEET-PROPOSAL.md
r1, lines 1, 2, 5, 6 and 8). Every render test reads its expected value from
the catalog, so a figure can only change in one place. Reservation fees
(lines 3/4) are live admin settings and are not tested here.
"""

import json
import re
import socket
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SPECIALTY_PAGES
from tests.test_specialty_packages import catalog_group, inquiry_link, rendered

pytestmark = pytest.mark.integration

TEAM_LINES = ("Team headshots", "Small-team pack", "On-site team day")
GROUP_SIZES = {"Tier I": 2, "Tier II": 5, "Tier III": 8}


@pytest.fixture
def client(monkeypatch):
    # Public rendering only: no background jobs, calendar query, or network.
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside price-sheet GET coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as test_client:
        yield test_client


def has_class(element, name):
    return name in (element.attrs.get("class") or "").split()


def price_text(line):
    # A card's price cell as rendered: the figure, then its unit.
    return " ".join(f"{line['display_price']} {line.get('price_unit') or ''}".split())


def team_lines():
    return {line["name"]: line for line in catalog_group("portraits")["lines"]}


def services_offers(client, group_title):
    markup = client.get("/services").text
    blocks = [
        json.loads(block)
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', markup, re.S)
    ]
    item_list = next(b for b in blocks if b.get("@type") == "ItemList")
    service = next(
        e["item"] for e in item_list["itemListElement"] if e["item"]["name"] == group_title
    )
    return markup, blocks, service["offers"]


def test_catalog_carries_the_approved_portrait_figures():
    lines = team_lines()
    assert list(lines) == list(TEAM_LINES)  # line 5 option A: no family line
    team, pack, onsite = (lines[name] for name in TEAM_LINES)
    assert (team["display_price"], team["price_unit"], team["price_cents"]) == (
        "$125",
        "/person",
        12500,
    )
    assert "One look, ~15-minute slot, 2 retouched finals" in team["includes"]
    assert (pack["display_price"], pack["price_cents"]) == ("$550", 55000)
    assert "Five people" in pack["subtitle"]
    assert (onsite["display_price"], onsite["price_cents"], onsite["minimum"]) == (
        "$750",
        75000,
        True,
    )
    for tier in catalog_group("portraits")["tiers"]:
        assert f"Up to {GROUP_SIZES[tier['name']]} people" in tier["includes"]


def test_catalog_carries_the_approved_real_estate_figures():
    re_page = SPECIALTY_PAGES["re"]
    assert re_page["twilight"]["addon_price"] == "+$150"
    assert re_page["str"]["price"] == (
        "From $450 per property (twilight set + 40 images); 3+ properties at $400 each, one invoice"
    )
    # "From $450" is Signature's published price (the approved Signature scope).
    signature = next(t for t in catalog_group("real_estate")["tiers"] if t["name"] == "Signature")
    assert signature["display_price"] == "$450"


def test_portraits_spoke_renders_each_team_line_from_the_catalog(client):
    section = rendered(client, "/portraits").one(id="rates").one("section", id="lines-portraits")
    articles = section.find("article")
    assert [a.one("h3").text() for a in articles] == list(TEAM_LINES)
    for article in articles:
        line = team_lines()[article.one("h3").text()]
        prices = [p.text() for p in article.find("p") if has_class(p, "svc-price")]
        assert prices == [price_text(line)]
        assert [li.text() for li in article.find("li")] == line["includes"]
        _link, query = inquiry_link(article)
        assert query == {"service": ["Portraits"], "tier": [line["name"]]}


@pytest.mark.parametrize("path", ["/portraits", "/services"])
def test_portrait_tiers_state_their_group_size(client, path):
    root = rendered(client, path)
    container = (
        root.one(id="rates").one("section", id="packages-portraits")
        if path == "/portraits"
        else root.one("div", id="svc-portraits")
    )
    articles = container.find("article")[:3]
    for tier, article in zip(catalog_group("portraits")["tiers"], articles, strict=True):
        assert article.one("h3").text() == tier["name"]
        items = [li.text() for li in article.find("li")]
        assert f"Up to {GROUP_SIZES[tier['name']]} people" in items


@pytest.mark.parametrize("path", ["/portraits", "/services", "/real-estate"])
def test_the_family_line_is_gone(client, path):
    assert "Family session" not in client.get(path).text


def test_services_lists_the_priced_team_lines(client):
    group = catalog_group("portraits")
    block = rendered(client, "/services").one("div", id="svc-portraits")
    heads = [h for h in block.find("div") if h.attrs.get("id") == "lines-portraits"]
    assert len(heads) == 1 and heads[0].one("h3").text() == group["lines_title"]
    articles = block.find("article")
    assert len(articles) == len(group["tiers"]) + len(TEAM_LINES)
    for article, line in zip(articles[len(group["tiers"]) :], group["lines"], strict=True):
        assert article.one("h3").text() == line["name"]
        prices = [p.text() for p in article.find("p") if has_class(p, "svc-price")]
        assert prices == [price_text(line)]
        links = [a for a in article.find("a") if urlsplit(a.attrs["href"]).path == "/contact"]
        assert parse_qs(urlsplit(links[0].attrs["href"]).query) == {
            "service": ["Portraits"],
            "tier": [line["name"]],
        }


def test_services_offer_json_ld_includes_the_team_lines(client):
    group = catalog_group("portraits")
    markup, blocks, offers = services_offers(client, group["title"])
    expected = [*group["tiers"], *group["lines"]]
    assert [o["name"] for o in offers] == [t["name"] for t in expected]
    for offer, item in zip(offers, expected, strict=True):
        assert offer["price"] == item["price_cents"] / 100
        assert offer["priceCurrency"] == "USD"
        if item.get("minimum"):
            assert offer["priceSpecification"] == {
                "@type": "PriceSpecification",
                "minPrice": item["price_cents"] / 100,
                "priceCurrency": "USD",
            }
        else:
            assert "priceSpecification" not in offer
    # Review JSON-LD stays off.
    assert not [
        b for b in blocks if "Review" in json.dumps(b) or "aggregateRating" in json.dumps(b)
    ]


def test_an_unpriced_line_leaves_services_and_its_offers(client, monkeypatch):
    group = catalog_group("portraits")
    pack = next(line for line in group["lines"] if line["name"] == "Small-team pack")
    monkeypatch.setitem(pack, "display_price", None)
    block = rendered(client, "/services").one("div", id="svc-portraits")
    names = [a.one("h3").text() for a in block.find("article")]
    assert "Small-team pack" not in names and "Team headshots" in names
    _markup, _blocks, offers = services_offers(client, group["title"])
    assert "Small-team pack" not in [o["name"] for o in offers]


def test_real_estate_renders_the_twilight_add_on_and_the_rental_rate(client):
    main = rendered(client, "/real-estate").one(id="main")
    twilight = main.one("section", id="twilight")
    rental = main.one("section", id="short-term-rentals")
    re_page = SPECIALTY_PAGES["re"]
    twilight_lines = [p.text() for p in twilight.find("p") if has_class(p, "sr-mono")]
    assert f"Twilight set added to Essentials · {re_page['twilight']['addon_price']}" in (
        twilight_lines
    )
    rental_lines = [p.text() for p in rental.find("p") if has_class(p, "sr-mono")]
    assert f"Rental set · {re_page['str']['price']}" in rental_lines
    # The volume note and the rate agree: three or more properties, one invoice.
    note = re_page["str"]["multi"][1].lower()
    assert "three or more" in note and "single invoice" in note
    assert "3+" in re_page["str"]["price"] and "one invoice" in re_page["str"]["price"]


@pytest.mark.parametrize("path", ["/real-estate", "/services"])
def test_no_virtual_twilight_anywhere(client, path):
    assert "virtual" not in client.get(path).text.lower()
