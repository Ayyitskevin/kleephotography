"""/food-beverage: price floor, what to send for a quote, included/quoted-separately
strip, delivery/ads/stylist FAQs, and the drinks-and-food-brand lane.

Everything price-shaped is read from SERVICES; no dollar figure is typed here.
"""

import html
import re
import socket

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SERVICES

pytestmark = pytest.mark.integration

FB_GROUPS = ("photography", "videography")
NEW_FAQ_STEMS = (
    "delivery app",
    "paid ads",
    "stylist",
)
# Usage/licence/ownership wording belongs to Kevin (legal); these answers stay practical.
LEGAL_WORDS = ("licen", "perpetual", "exclusive", "ownership", "copyright", "own the", "rights")


@pytest.fixture
def client(monkeypatch):
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside specialty GET coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as test_client:
        yield test_client


def page_html(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


def section(markup, start_marker, end_marker="</section>"):
    start = markup.index(start_marker)
    return markup[start : markup.index(end_marker, start)]


def lowest_fb_tier():
    tiers = [t for g in SERVICES if g["key"] in FB_GROUPS for t in g["tiers"]]
    return min(tiers, key=lambda t: t["price_cents"])


def photography_group():
    return next(g for g in SERVICES if g["key"] == "photography")


def title_card(markup):
    return section(markup, 'aria-label="Food &amp; beverage"')


def test_title_card_shows_the_catalog_price_floor(client):
    card = title_card(page_html(client, "/food-beverage"))
    assert f"From {lowest_fb_tier()['display_price']}" in html.unescape(card)


def test_price_floor_is_bound_to_the_catalog_not_typed(client, monkeypatch):
    monkeypatch.setitem(photography_group()["tiers"][0], "display_price", "$777")
    card = title_card(page_html(client, "/food-beverage"))
    assert "From $777" in html.unescape(card)


def test_title_card_says_what_to_send_and_links_to_the_quote_form(client):
    card = html.unescape(title_card(page_html(client, "/food-beverage")))
    assert "how many dishes" in card
    assert 'href="/contact?service=Food%20%26%20Beverage"' in card
    # The form it points at already has the fields the line asks for.
    contact = page_html(client, "/contact?service=Food+%26+Beverage")
    assert 'name="dish_count"' in contact
    assert 'name="shoot_date"' in contact


def test_included_and_quoted_separately_strip_comes_from_the_catalog(client):
    markup = page_html(client, "/food-beverage")
    strip = html.unescape(section(markup, 'class="sp-included"', "</aside>"))
    for line in photography_group()["tiers"][0]["includes"]:
        assert line in strip
    assert "Quoted separately" in strip
    assert "stylist" in strip.lower()
    # No new fee or prop claim: nothing price-shaped inside the strip.
    assert "$" not in strip


@pytest.mark.parametrize("path", ["/real-estate", "/portraits"])
def test_other_spokes_do_not_get_the_fb_only_blocks(client, path):
    markup = page_html(client, path)
    assert "sp-included" not in markup
    assert "sp-brandlane" not in markup


def test_brand_lane_section_routes_to_brand_partner_without_prices(client):
    markup = page_html(client, "/food-beverage")
    lane = section(markup, 'class="sr-wrap sr-section sp-brandlane"')
    text = html.unescape(lane)
    for word in ("cans", "bottles", "packaging", "e-commerce", "social"):
        assert word in text.lower()
    assert "$" not in text
    assert 'href="#packages-brand_partner"' in lane
    assert 'id="packages-brand_partner"' in markup


def test_new_faqs_are_visible_in_the_page_and_the_faqpage_schema(client):
    markup = page_html(client, "/food-beverage")
    questions = re.findall(r"<summary>(.*?)</summary>", markup)
    for stem in NEW_FAQ_STEMS:
        assert any(stem in html.unescape(q).lower() for q in questions), stem
    schema = section(markup, '"@type": "FAQPage"', "</script>")
    asked = [html.unescape(q).lower() for q in re.findall(r'"name": "(.*?)",?\n', schema)]
    for stem in NEW_FAQ_STEMS:
        assert any(stem in q for q in asked), stem


def test_new_faq_answers_do_not_set_usage_or_licence_terms(client):
    markup = page_html(client, "/food-beverage")
    pairs = re.findall(r"<summary>(.*?)</summary>\s*<p>(.*?)</p>", markup, re.S)
    checked = 0
    for question, answer in pairs:
        if any(stem in html.unescape(question).lower() for stem in NEW_FAQ_STEMS[:2]):
            checked += 1
            lowered = html.unescape(answer).lower()
            assert not [w for w in LEGAL_WORDS if w in lowered], (question, answer)
    assert checked == 2
