"""The /portraits spoke: two booking doors, the session in three steps, practical
prep in the FAQ, and team lines that stay hidden while their price is unset (the
approved figures themselves are pinned in tests/test_price_sheet.py).

Witnesses are listed here independently of the template (question wording, line
names, the Portraits scope placeholder). Where a test pins a template-to-catalog
binding (the process strip, a priced line's includes) it says so.
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

PREP_QUESTIONS = (
    "I wear glasses. Will they catch the light?",  # D-9
    "Can I bring a second look?",
    "Should I get hair and makeup done?",
    "How should I arrive?",
)
TEAM_LINES = ("Team headshots", "Small-team pack", "On-site team day")
FAMILY_LINE = "Family session"
PORTRAIT_SCOPE_PLACEHOLDER = "e.g. just me · team of 8 · family of 5"
TIER_PRICES = ["$350", "$600", "$850"]


@pytest.fixture
def client(monkeypatch):
    # Same isolation as the specialty-package suite: public rendering only, no
    # background jobs, no calendar query, no network.
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside portraits GET coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as test_client:
        yield test_client


def has_class(element, name):
    return name in (element.attrs.get("class") or "").split()


def contact_links(container):
    return [a for a in container.find("a") if urlsplit(a.attrs.get("href", "")).path == "/contact"]


def svc_prices(container):
    return [p.text() for p in container.find("p") if has_class(p, "svc-price")]


def test_portraits_render_the_session_in_three_steps_after_the_frames(client):
    main = rendered(client, "/portraits").one(id="main")
    strip = main.one("section", **{"aria-label": "How a session runs"})
    steps = strip.one("div", **{"class": "sr-ledger"}).find("div")  # D-6: the process is a ledger
    got = [tuple(p.text() for p in step.find("p")) for step in steps]
    # Binding pin: the strip renders the catalog's portrait process, in order.
    assert got == [tuple(step) for step in SPECIALTY_PAGES["pl"]["process"]]
    assert len(got) == 3 and got[0][0] == "Plan the session"
    sections = main.find("section")
    position = {id(section): index for index, section in enumerate(sections)}
    frames_or_empty = [
        s
        for s in sections
        if s.attrs.get("aria-label") == "Selected sessions"
        or s.find("div", **{"class": "empty-state"})
    ]
    assert frames_or_empty, "the frames block (or its empty state) must render"
    rates = main.one("section", id="rates")
    assert position[id(frames_or_empty[-1])] < position[id(strip)] < position[id(rates)]


def test_portraits_offer_a_team_door_beside_the_booking_door(client):
    page = rendered(client, "/portraits")
    sr = SPECIALTY_PAGES["pl"]["sr"]
    # Drift pin: the door's inquiry names a real team line in the catalog.
    assert sr["team_tier"] in TEAM_LINES
    assert sr["team_tier"] in [line["name"] for line in catalog_group("portraits")["lines"]]
    boxes = [
        page.one("div", **{"class": "sr-herosplit-actions"}),
        page.one("section", **{"aria-label": "Book"}),
        *page.find("div", **{"class": "empty-state-actions"}),
    ]
    team_hrefs = set()
    for box in boxes:
        links = box.find("a")
        assert any(urlsplit(a.attrs["href"]).path.startswith("/book") for a in links)
        team = contact_links(box)
        assert len(team) == 1, "one quote door per action row: the team door"
        query = parse_qs(urlsplit(team[0].attrs["href"]).query, strict_parsing=True)
        assert query == {"service": ["Portraits"], "tier": [sr["team_tier"]]}
        assert "team" in team[0].text().lower()
        assert "request a quote" not in box.text().lower()
        team_hrefs.add(team[0].attrs["href"])
    assert len(team_hrefs) == 1
    contact = rendered(client, team_hrefs.pop())
    selected = [
        option.attrs["value"]
        for option in contact.one("select", id="contact-service").find("option")
        if "selected" in option.attrs
    ]
    assert selected == ["Portraits"]
    assert contact.one("input", id="contact-scope").attrs["placeholder"] == (
        PORTRAIT_SCOPE_PLACEHOLDER
    )
    message = contact.one("textarea", id="contact-message").text()
    assert f"the {sr['team_tier']} tier for Portraits" in message


@pytest.mark.parametrize(
    "path,service", [("/real-estate", "Real Estate"), ("/food-beverage", "Food & Beverage")]
)
def test_other_spokes_keep_their_generic_quote_link(client, path, service):
    final = rendered(client, path).one("section", **{"aria-label": "Book"})
    links = contact_links(final)
    assert len(links) == 1
    assert parse_qs(urlsplit(links[0].attrs["href"]).query, strict_parsing=True) == {
        "service": [service]
    }
    assert links[0].text() == "Request a quote"


def test_portraits_faq_answers_practical_prep(client):
    page = rendered(client, "/portraits")
    faq = page.one("section", **{"aria-label": "Frequently asked questions"})
    summaries = [summary.text() for summary in faq.find("summary")]
    for question in PREP_QUESTIONS:
        assert question in summaries
    structured = [
        json.loads("".join(c for c in script.children if isinstance(c, str)))
        for script in page.find("script", type="application/ld+json")
    ]
    faq_pages = [d for d in structured if d.get("@type") == "FAQPage"]
    assert len(faq_pages) == 1
    answers = {e["name"]: e["acceptedAnswer"]["text"] for e in faq_pages[0]["mainEntity"]}
    for question in PREP_QUESTIONS:
        assert answers[question]
        # Usage/ownership answers belong to the legal-gated usage-rights change.
        assert not re.search(r"licen|rights|copyright|own the", answers[question].lower())


def unprice_lines(monkeypatch, group):
    for line in group["lines"]:
        monkeypatch.setitem(line, "display_price", None)


def test_team_lines_stay_hidden_while_unpriced(client, monkeypatch):
    # The lines carry approved prices now (ruling 1); the gate still holds for a
    # line whose price is unset.
    group = catalog_group("portraits")
    assert [line["name"] for line in group["lines"]] == list(TEAM_LINES)
    unprice_lines(monkeypatch, group)
    rates = rendered(client, "/portraits").one(id="rates")
    assert [s for s in rates.find("section") if s.attrs.get("id", "").startswith("lines-")] == []
    for name in (*TEAM_LINES, FAMILY_LINE):
        assert name not in rates.text()
    # Nothing price-shaped beyond the three published tiers.
    assert svc_prices(rates) == TIER_PRICES
    assert "$" not in rates.text().replace("$350", "").replace("$600", "").replace("$850", "")
    for other in ("/real-estate", "/food-beverage"):
        other_rates = rendered(client, other).one(id="rates")
        assert not [
            s for s in other_rates.find("section") if s.attrs.get("id", "").startswith("lines-")
        ]


@pytest.mark.parametrize("priced", TEAM_LINES)
def test_a_priced_line_renders_alone_with_its_inquiry(client, monkeypatch, priced):
    group = catalog_group("portraits")
    unprice_lines(monkeypatch, group)
    line = next(item for item in group["lines"] if item["name"] == priced)
    monkeypatch.setitem(line, "display_price", "$123")  # synthetic fixture figure
    rates = rendered(client, "/portraits").one(id="rates")
    section = rates.one("section", id="lines-portraits")
    assert section.one("h2").text() == group["lines_title"]
    articles = section.find("article")
    assert len(articles) == 1
    article = articles[0]
    assert article.one("h3").text() == priced
    unit = line.get("price_unit")
    assert svc_prices(article) == [" ".join(f"$123 {unit or ''}".split())]
    # Binding pin: the card lists the line's catalog includes.
    assert [li.text() for li in article.find("li")] == line["includes"]
    if priced in TEAM_LINES:
        assert any("new hires" in li.text().lower() for li in article.find("li"))
    _link, query = inquiry_link(article)
    assert query == {"service": ["Portraits"], "tier": [priced]}
    for other in TEAM_LINES:
        if other != priced:
            assert other not in section.text()
    # The per-group tier pin keeps seeing exactly three tier cards.
    assert len(rates.one("section", id="packages-portraits").find("article")) == 3
