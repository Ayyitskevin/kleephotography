"""Current package promises must survive the actual public page/contact render.

The historical pricing transition is deliberately NOT activated by these tests.
No new selector/helper is called to produce either the page or inquiry URL.
"""

import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, scheduler, scheduling, specialties
from app.main import app
from app.public import site
from app.public.site_catalog import SERVICES

pytestmark = pytest.mark.integration

# Independent witnesses for the held/current commercial values and taxonomy.
PAGES = [
    ("/real-estate", "re", "The Space", ("real_estate",), "re-shoot"),
    ("/portraits", "pl", "The Face", ("portraits",), "pl-session"),
    (
        "/food-beverage",
        "fb",
        "The Plate",
        ("photography", "videography", "brand_partner"),
        "fb-shoot",
    ),
]
CURRENT_PRICES = {
    "real_estate": ("$250", "$450", "$850"),
    "portraits": ("$350", "$600", "$850"),
    "photography": ("$750", "$1,400", "$2,600"),
    "videography": ("$1,250", "$2,200", "$3,900"),
    "brand_partner": ("$1,100", "$1,850", "$3,200"),
}


class Element:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def find(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if isinstance(child, Element):
                if (tag is None or child.tag == tag) and all(
                    child.attrs.get(key) == value for key, value in attrs.items()
                ):
                    found.append(child)
                found.extend(child.find(tag, **attrs))
        return found

    def one(self, tag=None, **attrs):
        found = self.find(tag, **attrs)
        assert len(found) == 1, (tag, attrs, len(found))
        return found[0]

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(
            " ".join(
                child.text() if isinstance(child, Element) else child for child in self.children
            ).split()
        )


class Page(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs)
        self.stack[-1].children.append(element)
        if tag not in self.VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Element(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.stack[-1].children.append(text)


@pytest.fixture
def client(monkeypatch):
    # Keep normal app lifespan/migration and public rendering, but never launch
    # background financial/delivery work or query a calendar during a GET.
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


@pytest.fixture(params=[False, True], ids=["legacy-palette", "screening-room"])
def theme(request, monkeypatch):
    monkeypatch.setattr(config, "SCREENING_ROOM", request.param)
    return request.param


def rendered(client, path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    assert response.url.path == urlsplit(path).path
    return Page(response.text).root


def catalog_group(key):
    return next(group for group in SERVICES if group["key"] == key)


def inquiry_link(article):
    links = [a for a in article.find("a") if urlsplit(a.attrs.get("href", "")).path == "/contact"]
    assert len(links) == 1
    link = links[0]
    target = urlsplit(link.attrs["href"])
    assert not target.scheme and not target.netloc and not target.fragment
    assert "onclick" not in link.attrs
    assert link.text(), "Inquiry must be a usable native link without JavaScript"
    return link, parse_qs(target.query, strict_parsing=True)


@pytest.mark.parametrize("path,key,screen,groups,book_slug", PAGES)
def test_rates_render_every_current_tier_and_literal_inclusion(
    client, theme, path, key, screen, groups, book_slug
):
    page = rendered(client, path)
    assert specialties.SPECIALTIES[key]["slug"] == path.lstrip("/")
    assert specialties.SPECIALTIES[key]["screen_name"] == screen
    assert screen in page.text()
    rates = page.one(id="rates")
    actual_groups = [
        section
        for section in rates.find("section")
        if section.attrs.get("id", "").startswith("packages-")
    ]
    assert [group.attrs["id"] for group in actual_groups] == [f"packages-{g}" for g in groups]
    for group, expected_key in zip(actual_groups, groups, strict=True):
        expected = catalog_group(expected_key)
        assert group.one("h2").text() == expected["title"]
        articles = group.find("article")
        assert len(articles) == 3
        assert tuple(t["display_price"] for t in expected["tiers"]) == CURRENT_PRICES[expected_key]
        for article, tier, current_price in zip(
            articles, expected["tiers"], CURRENT_PRICES[expected_key], strict=True
        ):
            assert article.one("h3").text() == tier["name"]
            assert tier["subtitle"] in article.text()
            prices = [
                p for p in article.find("p") if "svc-price" in p.attrs.get("class", "").split()
            ]
            assert len(prices) == 1
            assert prices[0].text() == current_price + (
                " " + tier["price_unit"] if tier.get("price_unit") else ""
            )
            assert [li.text() for li in article.find("li")] == tier["includes"]
            if expected_key == "brand_partner":
                assert "/mo" in prices[0].text()
                assert "per month" in article.text().lower()
            else:
                assert "/mo" not in prices[0].text()
    # An ordinary inquiry is not a financial transaction or reservation.
    assert "does not confirm scope, reserve a time, or take payment" in rates.text()
    assert any(a.attrs.get("href") == "/services" for a in rates.find("a"))


@pytest.mark.parametrize("path,key,screen,groups,book_slug", PAGES)
def test_each_native_tier_inquiry_prefills_existing_contact_form(
    client, theme, path, key, screen, groups, book_slug
):
    rates = rendered(client, path).one(id="rates")
    for group_key in groups:
        expected = catalog_group(group_key)
        articles = rates.one(id=f"packages-{group_key}").find("article")
        assert len(articles) == len(expected["tiers"])
        for article, tier in zip(articles, expected["tiers"], strict=True):
            link, query = inquiry_link(article)
            tier_text = f"{expected['title']} — {tier['name']}" if key == "fb" else tier["name"]
            assert query == {"service": [expected["contact_service"]], "tier": [tier_text]}
            assert tier["name"] in link.text()
            contact = rendered(client, link.attrs["href"])
            selected = [
                option
                for option in contact.one("select", id="contact-service").find("option")
                if "selected" in option.attrs
            ]
            assert [option.attrs["value"] for option in selected] == [expected["contact_service"]]
            message = contact.one("textarea", id="contact-message").text()
            assert f"the {tier_text} tier for {expected['contact_service']}" in message


def test_food_beverage_starter_inquiries_are_independently_disambiguated(client, theme):
    rates = rendered(client, "/food-beverage").one(id="rates")
    seen = []
    for group_key, title in (
        ("photography", "Food & Beverage — Photography"),
        ("videography", "Food & Beverage — Videography"),
    ):
        article = rates.one(id=f"packages-{group_key}").find("article")[0]
        assert article.one("h3").text() == "Starter"
        link, query = inquiry_link(article)
        assert query == {"service": ["Food & Beverage"], "tier": [f"{title} — Starter"]}
        contact = rendered(client, link.attrs["href"])
        assert f"{title} — Starter" in contact.one("textarea", id="contact-message").text()
        seen.append(link.attrs["href"])
    assert seen[0] != seen[1], "Shared Starter name must not collapse photo and video intent"


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
def test_real_estate_has_no_current_aerial_sale_claims_or_booking_cta(
    client, theme, monkeypatch, aerial_flag
):
    # Separate negative witness: a missing launch notice must not short-circuit
    # observation of the baseline's live certification/sale assertions.
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    main = rendered(client, "/real-estate").one(id="main")
    text = main.text().lower()
    for claim in (
        "faa part 107 certified pilot",
        "laanc airspace clearance, handled",
        "included in premier",
        "add-on to any tier",
        "aerial establishing + vertical for social",
    ):
        assert claim not in text
    for link in main.find("a"):
        label = (link.text() + " " + (link.attrs.get("aria-label") or "")).lower()
        assert not re.search(r"add aerials|book aerials|aerial booking", label)
        if "aerial" in label:
            assert urlsplit(link.attrs.get("href", "")).path == "/contact"


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
@pytest.mark.parametrize("with_reel", [False, True], ids=["empty-media", "synthetic-reel"])
def test_real_estate_aerials_are_launch_gated_at_either_flag(
    client, theme, monkeypatch, aerial_flag, with_reel
):
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    gallery = db.one("SELECT id FROM galleries WHERE slug='synthetic-specialty-reel'")
    if gallery is None:
        gallery_id = db.run(
            "INSERT INTO galleries (slug,title,pin) VALUES (?,?,?)",
            ("synthetic-specialty-reel", "Synthetic public render fixture", "synthetic-only"),
        )
    else:
        gallery_id = gallery["id"]
    reel = db.one("SELECT id FROM assets WHERE gallery_id=?", (gallery_id,))
    if reel is None:
        reel_id = db.run(
            """INSERT INTO assets
               (gallery_id,kind,filename,stored,status,portfolio,portfolio_tag,duration)
               VALUES (?,'video','synthetic.mp4','synthetic.mp4','ready',?,'re/walkthrough',120)""",
            (gallery_id, int(with_reel)),
        )
    else:
        reel_id = reel["id"]
        db.run("UPDATE assets SET portfolio=? WHERE id=?", (int(with_reel), reel_id))
    main = rendered(client, "/real-estate").one(id="main")
    text = main.text().lower()
    assert re.search(r"aerial coverage is planned[^.]*not available to book today", text)
    assert "part 107" in text
    for old_live_claim in (
        "faa part 107 certified pilot",
        "laanc airspace clearance, handled",
        "included in premier",
        "add-on to any tier",
        "aerial establishing + vertical for social",
    ):
        assert old_live_claim not in text
    for link in main.find("a"):
        label = (link.text() + " " + (link.attrs.get("aria-label") or "")).lower()
        assert not re.search(r"add aerials|book aerials|aerial booking", label)
        if "aerial" in label:
            assert urlsplit(link.attrs.get("href", "")).path == "/contact"
    if aerial_flag:
        panel = main.one("section", **{"aria-label": "The Aerial Pass"})
        assert "launch requires" in panel.text().lower()
        assert "local airspace" in panel.text().lower()
        assert "not available to book today" in panel.text().lower()
    if with_reel:
        assert main.one("video").one("source").attrs["src"] == f"/site/vid/{reel_id}"
        chapters = [
            button.text().lower() for button in main.find("button") if "data-seek" in button.attrs
        ]
        assert any("kitchen & great room" in chapter for chapter in chapters)
        assert not any("aerial" in chapter or "drone" in chapter for chapter in chapters)
    else:
        assert main.find("video") == []
    db.run("UPDATE assets SET portfolio=0 WHERE id=?", (reel_id,))


@pytest.mark.parametrize("path,key,screen,groups,book_slug", PAGES)
def test_general_booking_links_follow_real_active_event_and_inactive_fallback(
    client, theme, path, key, screen, groups, book_slug
):
    # Owned synthetic DB only; retain rows instead of deleting fixtures.
    db.run("UPDATE event_types SET active=0 WHERE slug=?", (book_slug,))

    def booking_links():
        page = rendered(client, path)
        # Shared desktop/mobile navigation deliberately links the global /book.
        # The specialty body and its sticky CTA alone own active-event routing.
        links = page.one(id="main").find("a") + page.one("div", **{"class": "sticky-cta"}).find("a")
        return [
            a.attrs["href"]
            for a in links
            if urlsplit(a.attrs.get("href", "")).path.startswith("/book")
        ]

    assert booking_links() and set(booking_links()) == {"/book"}
    row = db.one("SELECT id FROM event_types WHERE slug=?", (book_slug,))
    if row is None:
        db.run(
            "INSERT INTO event_types (slug,name,active) VALUES (?,?,1)",
            (book_slug, "Synthetic specialty availability"),
        )
    else:
        db.run("UPDATE event_types SET active=1 WHERE id=?", (row["id"],))
    active_urls = booking_links()
    assert active_urls and set(active_urls) == {f"/book/{book_slug}"}
    db.run("UPDATE event_types SET active=0 WHERE slug=?", (book_slug,))
    assert booking_links() and set(booking_links()) == {"/book"}
