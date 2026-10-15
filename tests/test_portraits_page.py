"""The /portraits spoke: two booking doors, the session in three steps, practical
prep in the FAQ, and team/family lines that stay hidden until they carry a price.

Witnesses are listed here independently of the template (question wording, line
names, the Portraits scope placeholder). Where a test pins a template-to-catalog
binding (the process strip, a priced line's includes) it says so.
"""

import socket
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SPECIALTY_PAGES
from tests.test_specialty_packages import rendered

pytestmark = pytest.mark.integration

PREP_QUESTIONS = (
    "I wear glasses — will they catch the light?",
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
    steps = strip.one("div", **{"class": "sr-3up"}).find("div")
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
