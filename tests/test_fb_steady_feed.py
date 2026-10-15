"""/food-beverage points steady-content buyers at the existing Brand Partner plans.

Everything shown comes from SERVICES: no figure, term or 'cancel anytime' wording is
written in the template or in this test.
"""

import html
import socket

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SERVICES

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


def page_html(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


def steady_block(markup):
    start = markup.index('class="sp-steady"')
    return markup[start : markup.index("</p>\n      </div>", markup.index("</a>", start))]


def brand_partner():
    return next(g for g in SERVICES if g["key"] == "brand_partner")


def test_steady_feed_link_targets_the_brand_partner_cards(client):
    markup = page_html(client, "/food-beverage")
    assert 'href="#packages-brand_partner"' in steady_block(markup)
    assert 'id="packages-brand_partner"' in markup


def test_month_summary_is_drawn_from_the_catalog_includes(client):
    block = html.unescape(steady_block(page_html(client, "/food-beverage")))
    for tier in brand_partner()["tiers"]:
        assert tier["name"] in block
        for line in tier["includes"]:
            assert line in block
    assert "$" not in block


def test_brand_partner_terms_are_unchanged(client):
    markup = html.unescape(page_html(client, "/food-beverage"))
    assert "cancel anytime" in brand_partner()["tagline"]
    assert "Cancel anytime" in brand_partner()["tiers"][0]["includes"]
    assert markup.count("Cancel anytime") >= 1
    for tier in brand_partner()["tiers"]:
        assert tier["display_price"] in markup
    assert [t["display_price"] for t in brand_partner()["tiers"]] == ["$1,100", "$1,850", "$3,200"]


@pytest.mark.parametrize("path", ["/real-estate", "/portraits"])
def test_other_spokes_do_not_get_the_steady_feed_block(client, path):
    assert "sp-steady" not in page_html(client, path)
