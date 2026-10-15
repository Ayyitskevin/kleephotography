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
def test_real_estate_aerial_sale_claims_follow_the_flag(client, theme, monkeypatch, aerial_flag):
    """Flag on: the certified, bookable Aerial Pass renders and nothing says planned.
    Flag off (kill switch): no aerial sale claim and no booking link anywhere."""
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    main = rendered(client, "/real-estate").one(id="main")
    text = main.text().lower()
    sale_claims = (
        "faa part 107 certified pilot",
        "laanc airspace clearance, handled",
        "included in premier",
        "add-on to any tier",
    )
    aerial_links = [
        link
        for link in main.find("a")
        if re.search(
            r"add aerials|book aerials|aerial booking",
            (link.text() + " " + (link.attrs.get("aria-label") or "")).lower(),
        )
    ]
    if aerial_flag:
        for claim in sale_claims:
            assert claim in text
        assert "not available to book today" not in text
        assert "planned" not in text
        assert len(aerial_links) == 1
        assert urlsplit(aerial_links[0].attrs["href"]).path.startswith("/book")
    else:
        for claim in sale_claims:
            assert claim not in text
        assert aerial_links == []


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
@pytest.mark.parametrize("with_reel", [False, True], ids=["empty-media", "synthetic-reel"])
def test_real_estate_aerial_band_and_film_chapters_follow_the_flag(
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
    assert "part 107" in text
    if aerial_flag:
        panel = main.one("section", **{"aria-label": "The Aerial Pass"})
        assert "faa part 107 certified pilot" in panel.text().lower()
        assert "laanc airspace clearance" in panel.text().lower()
        assert "not available to book today" not in text
    else:
        assert main.find("section", **{"aria-label": "The Aerial Pass"}) == []
        assert "not available to book today" in text
    if with_reel:
        assert main.one("video").one("source").attrs["src"] == f"/site/vid/{reel_id}"
        chapters = [
            button.text().lower() for button in main.find("button") if "data-seek" in button.attrs
        ]
        assert any("kitchen & great room" in chapter for chapter in chapters)
        aerial_chapters = [c for c in chapters if "aerial" in c or "drone" in c]
        # the player strip carries one tick per timed chapter; the aerial one (at 0.62) is
        # present only while the flag is on
        strip = main.one("i", **{"class": "sr-progress"})
        ticks = [t.attrs.get("style", "") for t in strip.find("i")]
        assert ("left: 62%;" in ticks) is aerial_flag, ticks
        if aerial_flag:
            assert len(aerial_chapters) == 1 and "aerial orbit" in aerial_chapters[0]
            assert "drone" in aerial_chapters[0]
        else:
            assert aerial_chapters == []
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


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
def test_real_estate_headline_aerial_line_follows_the_flag(client, theme, monkeypatch, aerial_flag):
    """The line under the H1 must agree with the booking card: bookable Aerial Pass
    wording when the flag is on, the planned notice only in the kill-switch state."""
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    main = rendered(client, "/real-estate").one(id="main")
    head = main.one("div", **{"class": "sr-featurehead"}).text().lower()
    bands = main.find("section", **{"aria-label": "The Aerial Pass"})
    if aerial_flag:
        assert "not available" not in head
        assert "planned" not in head
        assert f"aerial pass {specialties.aerial_pass_display()} — faa part 107" in head
        assert len(bands) == 1
    else:
        assert "planned for launch" in head
        assert "not available to book today" in head
        assert bands == []


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
def test_real_estate_live_aerial_pieces_follow_the_flag(client, theme, monkeypatch, aerial_flag):
    """Every flag-gated aerial string the live site carries renders at flag on and
    nowhere at flag off (the documented kill switch)."""
    from app.public.site_catalog import SPECIALTY_PAGES

    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    sr = SPECIALTY_PAGES["re"]["sr"]
    rate = specialties.aerial_pass_display()
    text = rendered(client, "/real-estate").one(id="main").text().lower()
    pieces = (
        sr["spec_aerial"].format(rate=rate).lower(),
        sr["agents_cut_aerial"].lower(),
        f"aerial pass {rate} on any tier",
    )
    for piece in pieces:
        assert (piece in text) is aerial_flag, piece
    # the plain agent's cut is the tail of the aerial one, so it is always present
    assert sr["agents_cut"].lower() in text


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
def test_site_wide_aerial_claims_follow_the_flag(client, theme, monkeypatch, aerial_flag):
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    home = rendered(client, "/").text().lower()
    assert ("faa part 107 certified" in home) is aerial_flag
    about = rendered(client, "/about").text().lower()
    assert ("flown" in about) is aerial_flag


@pytest.mark.parametrize("aerial_flag", [False, True], ids=["aerial-off", "aerial-on"])
def test_aerial_badges_and_labels_follow_the_kill_switch(client, theme, monkeypatch, aerial_flag):
    """The flag hides the aerial badge and label everywhere (.env.example, SPECIALTY-LAUNCH.md),
    not the photos themselves."""
    monkeypatch.setattr(config, "AERIALS_LIVE", aerial_flag)
    gallery_id = db.run(
        "INSERT INTO galleries (slug,title,pin) VALUES (?,?,?)",
        ("synthetic-aerial-badges", "Synthetic aerial badge fixture", "synthetic-only"),
    )
    ids = [
        db.run(
            """INSERT INTO assets
               (gallery_id,kind,filename,stored,status,portfolio,portfolio_tag)
               VALUES (?,'photo',?,?,'ready',1,?)""",
            (gallery_id, f"{tag.split('/')[1]}.jpg", f"{tag.split('/')[1]}.jpg", tag),
        )
        for tag in ("re/aerials", "re/aerial")
    ]
    try:
        for path in ("/real-estate", "/portfolio"):
            markup = client.get(path).text
            assert markup.count("sr-filmchip sr-aerial") == (len(ids) if aerial_flag else 0), path
            labels = re.findall(r'(?:&mdash; |<small>|aria-pressed="false">)\s*Aerials?\b', markup)
            assert bool(labels) is aerial_flag, (path, labels)
            for asset_id in ids:  # the photos stay public
                assert f"FR&middot;{asset_id:04d}" in markup, (path, asset_id)
    finally:
        db.run("DELETE FROM assets WHERE gallery_id=?", (gallery_id,))
        db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))


def _re_sections(client):
    main = rendered(client, "/real-estate").one(id="main")
    return (
        main.one("section", id="twilight"),
        main.one("section", id="short-term-rentals"),
        main,
    )


def _unprice_re(monkeypatch):
    from app.public.site_catalog import SPECIALTY_PAGES

    monkeypatch.setitem(SPECIALTY_PAGES["re"]["twilight"], "addon_price", None)
    monkeypatch.setitem(SPECIALTY_PAGES["re"]["str"], "price", None)


def test_real_estate_twilight_and_str_sections_render_without_prices(client, theme, monkeypatch):
    # Both fields carry approved figures now (ruling 1); unset, nothing price-shaped renders.
    _unprice_re(monkeypatch)
    twilight, rental, main = _re_sections(client)
    assert "dusk" in twilight.text().lower()
    assert "guest" in rental.text().lower()
    for section in (twilight, rental):
        text = section.text().lower()
        assert "$" not in text
        assert "virtual" not in text
        assert "aerial" not in text and "drone" not in text
    # the in-page link lands on the rental section, and the CTA is an inquiry
    assert any(a.attrs.get("href") == "#short-term-rentals" for a in twilight.find("a"))
    cta = [a for a in rental.find("a") if urlsplit(a.attrs.get("href", "")).path == "/contact"]
    assert len(cta) == 1
    # generic quote only: no tier is pre-selected (the rental package/price is Kevin's call)
    assert parse_qs(urlsplit(cta[0].attrs["href"]).query) == {"service": ["Real Estate"]}


def test_real_estate_rental_section_notes_multi_property_booking_without_a_figure(client, theme):
    _, rental, _ = _re_sections(client)
    text = rental.text().lower()
    # the volume rate starts at three properties (approved price-sheet line 8)
    assert "booking three or more properties together" in text
    for wording in ("more than one", "two or more", "two properties", "several"):
        assert wording not in text
    assert "single invoice" in text and "volume rate" in text
    # the note itself carries no figure; the priced line below it does
    note = [p for p in rental.find("p") if "three or more" in p.text().lower()]
    assert len(note) == 1 and "$" not in note[0].text()
    cta = [a for a in rental.find("a") if urlsplit(a.attrs.get("href", "")).path == "/contact"]
    assert "tier=" not in cta[0].attrs["href"]


def test_real_estate_rental_copy_binds_no_tier_or_deliverable(client, theme):
    _, rental, _ = _re_sections(client)
    text = rental.text().lower()
    group = catalog_group("real_estate")
    for tier in group["tiers"]:
        assert tier["name"].lower() not in text
    # no tier deliverable is presented as part of the rental set
    for word in ("reel", "film", "walkthrough", "twilight", "mls", "images"):
        assert word not in text
    for line in (inc for t in group["tiers"] for inc in t["includes"]):
        assert line.lower() not in text


def test_real_estate_twilight_and_rental_sections_sit_before_the_proof_band(client, theme):
    markup = client.get("/real-estate").text
    work = max(markup.find('id="work"'), markup.find("empty-state"))
    twilight = markup.index('id="twilight"')
    rental = markup.index('id="short-term-rentals"')
    proof = markup.index("Turnaround", rental)
    assert 0 < work < twilight < rental < proof


def test_real_estate_price_lines_appear_only_once_priced(client, theme, monkeypatch):
    from app.public.site_catalog import SPECIALTY_PAGES

    _unprice_re(monkeypatch)
    twilight, rental, _ = _re_sections(client)
    assert "added to essentials" not in twilight.text().lower()
    assert "rental set" not in rental.text().lower()
    monkeypatch.setitem(SPECIALTY_PAGES["re"]["twilight"], "addon_price", "$111")
    monkeypatch.setitem(SPECIALTY_PAGES["re"]["str"], "price", "$222")
    twilight, rental, _ = _re_sections(client)
    assert "$111" in twilight.text()
    assert "$222" in rental.text()
