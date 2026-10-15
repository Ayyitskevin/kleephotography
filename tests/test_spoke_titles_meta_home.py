"""Spoke titles name the city."""

from __future__ import annotations

import html
import re

import pytest
from fastapi.testclient import TestClient

from app import specialties
from app.main import app
from app.public import site_catalog

CITY = "Asheville"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _rendered_title(text: str) -> str:
    return html.unescape(re.search(r"<title>(.*?)</title>", text, re.S).group(1))


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
