"""D-8 motion layer: the public pages carry no scroll-progress bar, the sticky
nav state has a sentinel the script observes, and the public stylesheets ship
no dead progress-bar rules (FABLE-IMPROVEMENT-PLAN §3c D-8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration

PAGES = ["/", "/portraits", "/real-estate", "/food-beverage", "/about", "/services", "/contact"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", PAGES)
def test_public_pages_have_no_scroll_progress_bar(client, path):
    html = client.get(path).text
    assert "scroll-progress" not in html, path
    assert "data-progress-bar" not in html, path


@pytest.mark.parametrize("path", PAGES)
def test_public_pages_place_one_nav_sentinel_before_the_header(client, path):
    html = client.get(path).text
    assert html.count("data-nav-sentinel") == 1, path
    assert html.index("data-nav-sentinel") < html.index("data-nav>"), path


def test_public_sheets_ship_no_progress_bar_rules(client):
    for sheet in ("/static/screening.css", "/static/mise.css"):
        css = client.get(sheet).text
        assert ".scroll-progress" not in css, sheet
        assert "data-progress-bar" not in css, sheet
