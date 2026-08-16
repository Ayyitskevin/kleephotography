"""Live-availability strip on the specialty spokes (Cal.com-parity item 3).

What must hold: the strip shows the next few genuinely open days as deep links
into the picker with that day preselected; the underlying scan (which can hit
Google free/busy) is TTL-cached so marketing pages never block on the network
twice in a row; and any scheduling failure renders the page without the strip
rather than failing it. Scoped to rows this file creates — shared test DB."""

import pytest
from fastapi.testclient import TestClient

from app import db, scheduling
from app.main import app
from app.public import site

pytestmark = pytest.mark.integration

_ET = {"slug": "avstrip-fake", "booking_window_days": 60}
_DAYS = {"2027-04-09", "2027-04-03", "2027-04-05", "2027-04-11"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    site._avail_cache.clear()
    yield
    site._avail_cache.clear()
    db.run("DELETE FROM event_types WHERE slug LIKE 'avstrip-%'")


def test_next_open_days_builds_picker_deep_links(monkeypatch):
    monkeypatch.setattr(scheduling, "days_with_slots", lambda et, start, n, ex=None: set(_DAYS))
    days = site._next_open_days(_ET)
    assert [d["iso"] for d in days] == ["2027-04-03", "2027-04-05", "2027-04-09"]
    assert days[0]["url"] == "/book/avstrip-fake?year=2027&month=4&day=2027-04-03"
    assert days[0]["label"].startswith("Sat")  # 2027-04-03 is a Saturday


def test_scan_is_ttl_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduling, "days_with_slots", lambda et, start, n, ex=None: calls.append(1) or set(_DAYS)
    )
    site._next_open_days(_ET)
    site._next_open_days(_ET)
    assert len(calls) == 1


def test_failure_yields_empty_strip_not_error(monkeypatch):
    def boom(et, start, n, ex=None):
        raise RuntimeError("google fell over")

    monkeypatch.setattr(scheduling, "days_with_slots", boom)
    assert site._next_open_days(_ET) == []


def test_specialty_page_shows_strip_with_real_event_type(monkeypatch):
    monkeypatch.setattr(scheduling, "days_with_slots", lambda et, start, n, ex=None: set(_DAYS))
    created = not db.one("SELECT 1 FROM event_types WHERE slug='re-shoot'")
    if created:
        db.run(
            "INSERT INTO event_types (slug, name, duration_min) VALUES ('re-shoot','RE Shoot',120)"
        )
    try:
        with TestClient(app) as pub:
            page = pub.get("/real-estate").text
        assert "Next openings:" in page
        assert "/book/re-shoot?year=2027&amp;month=4&amp;day=2027-04-03" in page
    finally:
        if created:
            db.run("DELETE FROM event_types WHERE slug='re-shoot'")


def test_specialty_page_without_event_type_has_no_strip():
    # No pl-session event type in the test DB — the portraits spoke must render
    # cleanly with the plain /book CTA and no strip.
    assert not db.one("SELECT 1 FROM event_types WHERE slug='pl-session'")
    with TestClient(app) as pub:
        r = pub.get("/portraits")
    assert r.status_code == 200
    assert "Next openings:" not in r.text
