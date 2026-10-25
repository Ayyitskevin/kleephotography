"""Hero discipline per spoke (plan D-3).

Each spoke's hero copy block carries kicker, h1, beat, the two CTAs and the
specline/price, and nothing else: at most eight text nodes, one of them the
booking link. Portraits move the lede to open the nerves section and the stats
into a full-width band after the hero; food & beverage drops the third CTA and
sets the quote ask as a lede; real estate stays as I-1 left it.

Renders run in a fresh database per test (as tests/test_public_render_golden.py)
so the food & beverage reel, which the third CTA needs, never leaks into other tests.
"""

import socket
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, scheduler, scheduling
from app.main import app
from app.public import site
from app.public.site_catalog import SPECIALTY_PAGES

pytestmark = pytest.mark.integration

# The hero copy block of each spoke (ruling RULING-P2-W3A-STOP.md, STOP 1 (a)).
HERO_COPY = {
    "/real-estate": "sr-featurehead",
    "/portraits": "sr-herosplit-copy",
    "/food-beverage": "sr-titlecard-copy",
}
SPOKE_TAG = {
    "/real-estate": "re/exteriors",
    "/portraits": "pl/headshots",
    "/food-beverage": "fb/plated",
}
MAX_HERO_TEXT_NODES = 8
VOID = set("area base br col embed hr img input link meta source track wbr".split())


class _Block(HTMLParser):
    """Text nodes and links inside the first element carrying `cls`."""

    def __init__(self, cls):
        super().__init__()
        self.cls = cls
        self.depth = 0  # >0 while inside the block
        self.done = False
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        a = dict(attrs)
        if self.depth:
            self.depth += 1
            if tag == "a":
                self.links.append((a.get("href") or "", a.get("class") or ""))
        elif not self.done and self.cls in (a.get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if tag in VOID or not self.depth:
            return
        self.depth -= 1
        if not self.depth:
            self.done = True

    def handle_data(self, data):
        if self.depth and data.strip():
            self.text.append(data.strip())


def hero_block(markup, route):
    block = _Block(HERO_COPY[route])
    block.feed(markup)
    assert block.done, f"{route}: no .{HERO_COPY[route]} block"
    return block


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    db.migrate()
    db.run("DELETE FROM testimonials")
    db.run("UPDATE assets SET portfolio=0, portfolio_tag=NULL")
    db.run("UPDATE galleries SET cs_published=0")
    for module in (jobs, scheduler):
        monkeypatch.setattr(module, "start", lambda: None)
        monkeypatch.setattr(module, "stop", lambda: None)
    monkeypatch.setattr(scheduling, "days_with_slots", lambda *_args: [])
    monkeypatch.setattr(site, "_avail_cache", {})

    def no_network(*_args, **_kwargs):
        raise AssertionError("external network is outside spoke hero coverage")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    with TestClient(app) as client:
        yield client


def seed_work(tag):
    """One ready portfolio still and one ready portfolio reel (6 s) for a spoke."""
    gallery = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)", ("hero-g", "Hero", "1234")
    )
    for kind, name, duration in (("photo", "hero.jpg", None), ("video", "hero.mp4", 6.0)):
        db.run(
            """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio,
                                   portfolio_tag, duration)
               VALUES (?,?,?,?,?,?,?,?)""",
            (gallery, kind, name, name, "ready", 1, tag, duration),
        )


def get(client, route):
    response = client.get(route)
    assert response.status_code == 200, (route, response.status_code)
    return response.text


def sticky_book_url(markup):
    """book_url as the page itself states it: the sticky CTA's link."""
    start = markup.index('class="sticky-cta"')
    href = markup.index('href="', start) + len('href="')
    return markup[href : markup.index('"', href)]


@pytest.mark.parametrize("seeded", [False, True], ids=["empty", "seeded"])
@pytest.mark.parametrize("route", list(HERO_COPY))
def test_hero_copy_block_is_at_most_eight_text_nodes_with_the_booking_link(fresh, route, seeded):
    if seeded:
        seed_work(SPOKE_TAG[route])
    markup = get(fresh, route)
    block = hero_block(markup, route)
    assert len(block.text) <= MAX_HERO_TEXT_NODES, (route, len(block.text), block.text)
    book_url = sticky_book_url(markup)
    assert book_url.startswith("/book")
    assert any(href == book_url and "sr-btn" in cls for href, cls in block.links), block.links


def sections(markup):
    """Top-level <section> chunks of the page content, in order."""
    out, pos = [], markup.index('id="main"')
    while (start := markup.find("<section", pos)) != -1:
        depth, i = 0, start
        while True:
            nxt_open, nxt_close = markup.find("<section", i + 1), markup.find("</section>", i + 1)
            if nxt_open != -1 and nxt_open < nxt_close:
                depth, i = depth + 1, nxt_open
            elif depth:
                depth, i = depth - 1, nxt_close
            else:
                end = nxt_close + len("</section>")
                out.append(markup[start:end])
                pos = end
                break
    return out


def test_portraits_stats_leave_the_hero_for_a_band_and_the_lede_opens_the_nerves(fresh):
    sr = SPECIALTY_PAGES["pl"]["sr"]
    markup = get(fresh, "/portraits")
    hero, band, nerves = sections(markup)[:3]
    assert 'aria-label="Portrait sessions"' in hero
    assert "sr-stats" not in hero and "sr-lede" not in hero
    assert 'class="sr-stats sr-stats--band"' in band
    for n, label in sr["stats"]:  # R15 proof numbers ride along unchanged
        assert f"<b>{n}</b><small>{label}</small>" in band
    assert "<b>From $" in band
    assert 'aria-label="Common worries, answered"' in nerves
    lede = nerves.index('<p class="sr-lede">')
    assert lede < nerves.index('class="sr-qa"')
    assert "Headshots, personal branding" in nerves[lede:]


def test_food_beverage_drops_the_watch_cta_and_sets_the_quote_ask_as_a_lede(fresh):
    seed_work(SPOKE_TAG["/food-beverage"])
    markup = get(fresh, "/food-beverage")
    hero = sections(markup)[0]
    assert 'aria-label="Food &amp; beverage"' in hero
    assert "<video" in hero  # the reel is the hero, so the watch CTA had a reason to exist
    assert 'href="/reels"' not in hero and "Watch 0:06" not in hero
    ask = SPECIALTY_PAGES["fb"]["sr"]["quote_ask"]
    assert f'<p class="sr-lede sr-lede--tight">{ask}</p>' in hero
    assert f'<p class="sr-mono">{ask}</p>' not in hero
