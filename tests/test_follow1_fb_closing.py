"""/food-beverage: the closing call repeats what to send for a quote, and the
'quoted separately' strip lists props beside the stylist.

The line is read from the catalog (reused from the title card), never retyped.
Guards (pass on main by design): the pl/re closing calls keep their exact links.
"""

import html
import socket
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SPECIALTY_PAGES
from tests.test_specialty_packages import rendered

pytestmark = pytest.mark.integration


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


def closing(client, path):
    return rendered(client, path).one("section", **{"aria-label": "Book"})


def hrefs(box):
    return [a.attrs["href"] for a in box.find("a")]


def test_fb_closing_call_carries_the_what_to_send_line(client):
    box = closing(client, "/food-beverage")
    assert SPECIALTY_PAGES["fb"]["sr"]["quote_ask"] in html.unescape(box.text())
    # Still one generic quote door, to the prefilled form.
    quote = [h for h in hrefs(box) if urlsplit(h).path == "/contact"]
    assert [parse_qs(urlsplit(h).query) for h in quote] == [{"service": ["Food & Beverage"]}]


@pytest.mark.parametrize("path", ["/portraits", "/real-estate"])
def test_other_closing_calls_do_not_get_the_line(client, path):
    box = closing(client, path)
    assert SPECIALTY_PAGES["fb"]["sr"]["quote_ask"] not in html.unescape(box.text())
    assert "how many dishes" not in box.text()


def test_guard_pl_closing_call_keeps_its_exact_links(client):
    sr = SPECIALTY_PAGES["pl"]["sr"]
    links = [urlsplit(h) for h in hrefs(closing(client, "/portraits"))]
    assert [u.path for u in links].count("/contact") == 1
    contact = next(u for u in links if u.path == "/contact")
    assert parse_qs(contact.query) == {"service": ["Portraits"], "tier": [sr["team_tier"]]}


def test_guard_re_closing_call_keeps_its_exact_links(client):
    links = [urlsplit(h) for h in hrefs(closing(client, "/real-estate"))]
    contact = [u for u in links if u.path == "/contact"]
    assert [parse_qs(u.query) for u in contact] == [{"service": ["Real Estate"]}]


def test_quoted_separately_lists_props_and_stylist_without_a_price(client):
    markup = html.unescape(client.get("/food-beverage").text)
    start = markup.index('class="sp-included"')
    strip = markup[start : markup.index("</aside>", start)].lower()
    assert "stylist" in strip
    assert "props" in strip
    assert "props and backdrops beyond what we shoot with, folded into your quote" in strip  # D-9
    assert "sourced" not in strip
    assert "$" not in strip
