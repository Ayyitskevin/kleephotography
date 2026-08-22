"""Public-surface rendering contracts: crawlability, responsive image metadata,
tile dimensions (CLS) and structured data."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db
from app.main import app
from app.render import ROOT

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    """An isolated media tree plus a starred gallery, torn down after the test.

    The suite shares one database, so the seeded rows have to go again — a
    lingering starred asset would leak into every other public-site render.
    """
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type) VALUES (?,?,?,1,'gallery')",
        ("seo-tiles", "SEO Tiles", "1234"),
    )
    try:
        yield gid
    finally:
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def _derivative(gid: int, variant: str, stem: str, size: tuple[int, int]) -> None:
    directory = config.MEDIA_DIR / str(gid) / variant
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 60, 90)).save(directory / f"{stem}.jpg", "JPEG")


def _seed_photo(gid: int, stem: str, tag: str, derivatives: bool = True) -> int:
    if derivatives:
        _derivative(gid, "thumb", stem, (80, 60))
        _derivative(gid, "web", stem, (320, 240))
    return db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio,
                               portfolio_tag)
           VALUES (?,'photo',?,?,'ready',1,?)""",
        (gid, f"{stem}.jpg", f"{stem}.jpg", tag),
    )


def _seed_video(gid: int, stem: str, tag: str) -> int:
    _derivative(gid, "thumb", stem, (90, 51))
    _derivative(gid, "web", f"{stem}_poster", (360, 203))
    (config.MEDIA_DIR / str(gid) / "web" / f"{stem}.mp4").write_bytes(b"video fixture")
    return db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio,
                               portfolio_tag)
           VALUES (?,'video',?,?,'ready',1,?)""",
        (gid, f"{stem}.mp4", f"{stem}.mp4", tag),
    )


def _img_tags(markup: str) -> list[str]:
    return re.findall(r"<img\b[^>]*>", markup, re.S)


def _tags_for(markup: str, url: str) -> list[str]:
    return [tag for tag in _img_tags(markup) if f'src="{url}"' in tag]


def _assert_no_inert_sizes(markup: str) -> None:
    """`sizes` without `srcset` is dead markup — the browser ignores it."""
    for tag in _img_tags(markup):
        assert "sizes=" not in tag or "srcset=" in tag, tag


def _assert_responsive(tag: str) -> None:
    assert 'srcset="/site/img/' in tag and " 80w, " in tag and " 320w" in tag, tag
    assert 'sizes="' in tag, tag
    assert 'width="80" height="60"' in tag, tag


def test_home_archive_tiles_carry_srcset_and_dimensions(client, portfolio):
    framed = _seed_photo(portfolio, "archive-framed", "fb/dishes")
    bare = _seed_photo(portfolio, "archive-bare", "fb/dishes", derivatives=False)

    page = client.get("/")
    assert page.status_code == 200
    _assert_no_inert_sizes(page.text)

    filmstrip = page.text.split('class="sr-filmstrip"', 1)[1]
    (framed_tag,) = _tags_for(filmstrip, f"/site/img/{framed}?variant=thumb")
    _assert_responsive(framed_tag)

    (bare_tag,) = _tags_for(filmstrip, f"/site/img/{bare}?variant=thumb")
    assert "srcset=" not in bare_tag and "sizes=" not in bare_tag and "width=" not in bare_tag


def test_specialty_grid_and_card_tiles_carry_dimensions(client, portfolio):
    _seed_photo(portfolio, "plate-one", "fb/dishes")
    lead = _seed_photo(portfolio, "plate-two", "fb/dishes")
    reel = _seed_video(portfolio, "plate-reel", "fb/motion")
    db.run(
        "UPDATE galleries SET cs_published=1, cs_tagline=? WHERE id=?",
        ("Plated", portfolio),
    )

    page = client.get("/food-beverage")
    assert page.status_code == 200
    _assert_no_inert_sizes(page.text)

    # The lead still renders twice: appetite grid tile and case-study card.
    lead_tags = _tags_for(page.text, f"/site/img/{lead}?variant=thumb")
    assert len(lead_tags) == 2
    for tag in lead_tags:
        _assert_responsive(tag)

    # A video tile keeps its lightweight thumb — dimensions, honestly no srcset.
    (reel_tag,) = _tags_for(page.text, f"/site/img/{reel}?variant=thumb")
    assert 'width="90" height="51"' in reel_tag
    assert "srcset=" not in reel_tag and "sizes=" not in reel_tag


def test_aerial_pass_tile_carries_srcset_and_dimensions(client, portfolio, monkeypatch):
    monkeypatch.setattr(config, "AERIALS_LIVE", True)
    aerial = _seed_photo(portfolio, "aerial-lead", "re/aerials")

    page = client.get("/real-estate")
    assert page.status_code == 200
    _assert_no_inert_sizes(page.text)

    band = page.text.split('class="sr-aerialpass-grid"', 1)[1]
    (tag,) = _tags_for(band, f"/site/img/{aerial}?variant=web")
    assert 'srcset="/site/img/' in tag and " 80w, " in tag and " 320w" in tag
    assert 'width="320" height="240"' in tag


def _seed_portfolio_video(slug: str) -> int:
    """A public reel with the derivatives /site/vid and /site/poster serve."""
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type) VALUES (?,?,?,1,'gallery')",
        (slug, "SEO Test", "1234"),
    )
    web = config.MEDIA_DIR / str(gid) / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "reel.mp4").write_bytes(b"fake-mp4-bytes")
    (web / "reel_poster.jpg").write_bytes(b"fake-jpeg-bytes")
    return db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio) "
        "VALUES (?,'video','reel.mp4','reel.mp4','ready',1)",
        (gid,),
    )


def test_video_asset_urls_stay_crawlable(client):
    """The /reels VideoObject points contentUrl/thumbnailUrl at these routes."""
    asset_id = _seed_portfolio_video("seo-reel-crawlable")

    video = client.get(f"/site/vid/{asset_id}")
    poster = client.get(f"/site/poster/{asset_id}")
    assert video.status_code == 200
    assert poster.status_code == 200
    assert "X-Robots-Tag" not in video.headers
    assert "X-Robots-Tag" not in poster.headers

    private = client.get("/g/seo-reel-crawlable")
    assert private.headers["X-Robots-Tag"] == "noindex, nofollow"


@pytest.fixture
def delivery():
    """Client-delivery rows (gallery, drop, portal) removed after the test."""
    made: dict[str, list[int]] = {"galleries": [], "portals": [], "clients": []}
    try:
        yield made
    finally:
        for gid in made["galleries"]:
            db.run(
                "DELETE FROM favorites WHERE asset_id IN "
                "(SELECT id FROM assets WHERE gallery_id=?)",
                (gid,),
            )
            db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
            db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
            db.run("DELETE FROM galleries WHERE id=?", (gid,))
        for pid in made["portals"]:
            db.run("DELETE FROM portals WHERE id=?", (pid,))
        for cid in made["clients"]:
            db.run("DELETE FROM clients WHERE id=?", (cid,))


def _seed_delivery_asset(gid: int, stem: str, kind: str, size: tuple[int, int] | None) -> int:
    width, height = size or (None, None)
    return db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status, width, height)
           VALUES (?,?,?,?,'ready',?,?)""",
        (gid, kind, f"{stem}.jpg", f"{stem}.jpg", width, height),
    )


def test_gallery_and_drop_tiles_carry_stored_dimensions(client, delivery):
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type) VALUES (?,?,?,1,'gallery')",
        ("cls-premiere", "CLS Premiere", "1234"),
    )
    drop_id = db.run(
        """INSERT INTO galleries (slug, title, pin, published, type, require_pin)
           VALUES (?,?,?,1,'drop',0)""",
        ("cls-dailies", "CLS Dailies", "5678"),
    )
    delivery["galleries"] += [gid, drop_id]
    measured = _seed_delivery_asset(gid, "measured", "photo", (4000, 3000))
    unmeasured = _seed_delivery_asset(gid, "unmeasured", "photo", None)
    dropped = _seed_delivery_asset(drop_id, "dailies", "photo", (1600, 1200))

    assert client.post("/g/cls-premiere/pin", data={"pin": "1234"}).status_code == 200
    premiere = client.get("/g/cls-premiere")
    assert premiere.status_code == 200
    (measured_tag,) = _tags_for(premiere.text, f"/media/cls-premiere/thumb/{measured}")
    assert 'width="4000" height="3000"' in measured_tag
    # Pre-ingest rows have no stored dimensions — no zeros, no guesses.
    (unmeasured_tag,) = _tags_for(premiere.text, f"/media/cls-premiere/thumb/{unmeasured}")
    assert "width=" not in unmeasured_tag and "height=" not in unmeasured_tag

    dailies = client.get("/g/cls-dailies")
    assert dailies.status_code == 200
    (drop_tag,) = _tags_for(dailies.text, f"/media/cls-dailies/thumb/{dropped}")
    assert 'width="1600" height="1200"' in drop_tag


def test_portal_crop_and_motion_tiles_carry_stored_dimensions(client, delivery):
    cid = db.run(
        "INSERT INTO clients (name, company, email) VALUES (?,?,?)",
        ("CLS Client", "CLS Co", "cls@example.test"),
    )
    delivery["clients"].append(cid)
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, client_id) VALUES (?,?,?,1,?)",
        ("cls-portal-gallery", "CLS Portal Gallery", "1234", cid),
    )
    delivery["galleries"].append(gid)
    pid = db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (cid, "cls-portal", "2468"),
    )
    delivery["portals"].append(pid)
    crop = _seed_delivery_asset(gid, "crop", "photo", (5000, 4000))
    reel = _seed_delivery_asset(gid, "reel", "video", (1080, 1920))
    visitor = db.run(
        "INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, "cls-portal-visitor")
    )
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (visitor, crop))

    assert client.post("/portal/cls-portal/pin", data={"pin": "2468"}).status_code == 200
    page = client.get("/portal/cls-portal")
    assert page.status_code == 200
    (crop_tag,) = _tags_for(page.text, f"/portal/cls-portal/thumb/{crop}")
    assert 'width="5000" height="4000"' in crop_tag
    (reel_tag,) = _tags_for(page.text, f"/portal/cls-portal/thumb/{reel}")
    assert 'width="1080" height="1920"' in reel_tag


def _control(markup: str, name: str) -> str:
    return re.search(rf'<(?:input|textarea)\b[^>]*name="{name}"[^>]*>', markup, re.S).group(0)


def test_email_gate_input_is_labelled(client, delivery):
    # A gallery (not a drop) email-gates its downloads — downloads._email_required.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type) VALUES (?,?,?,1,'gallery')",
        ("gate-label", "Gate Label", "1234"),
    )
    delivery["galleries"].append(gid)
    _seed_delivery_asset(gid, "framed", "photo", (2000, 1500))

    assert client.post("/g/gate-label/pin", data={"pin": "1234"}).status_code == 200
    gate = client.get("/g/gate-label/download")
    assert gate.status_code == 200
    field = _control(gate.text, "email")
    control_id = re.search(r'id="([^"]+)"', field).group(1)
    assert f'<label class="sr-only" for="{control_id}">' in gate.text


def test_contact_errors_mark_the_offending_control(client):
    blank_name = client.post(
        "/contact", data={"name": "  ", "email": "real@example.test", "message": "Hi"}
    )
    assert blank_name.status_code == 400
    assert 'aria-invalid="true" aria-describedby="contact-error"' in _control(
        blank_name.text, "name"
    )
    for field in ("email", "message"):
        assert "aria-invalid" not in _control(blank_name.text, field)

    bad_email = client.post(
        "/contact", data={"name": "Real Person", "email": "nope", "message": "Hi"}
    )
    assert bad_email.status_code == 400
    assert 'aria-invalid="true"' in _control(bad_email.text, "email")
    assert "aria-invalid" not in _control(bad_email.text, "name")

    # A throttle blames no field — the summary line carries it alone.
    assert "aria-invalid" not in client.get("/contact").text


def test_honeypots_hide_a_wrapper_not_the_control(client):
    form_id = db.run(
        "INSERT INTO forms (slug, title, kind, active) VALUES (?,?,'lead',1)",
        ("hp-wrapper-form", "Honeypot Wrapper"),
    )
    try:
        rendered = (client.get("/contact").text, client.get("/forms/hp-wrapper-form").text)
    finally:
        db.run("DELETE FROM forms WHERE id=?", (form_id,))
    for markup in rendered:
        assert '<span class="hp-field" aria-hidden="true">' in markup
        assert "aria-hidden" not in _control(markup, "website")

    # _book_card's honeypot rides a booking step that needs a live slot to
    # render, so its contract is pinned at the source.
    book_card = (ROOT / "templates/public/_book_card.html").read_text()
    assert '<span class="hp-field" aria-hidden="true">' in book_card
    assert "aria-hidden" not in _control(book_card, "website")


def test_reels_video_schema_omits_a_missing_upload_date(client, portfolio):
    dated = _seed_video(portfolio, "dated-reel", "fb/motion")
    undated = _seed_video(portfolio, "undated-reel", "fb/motion")
    # assets.created_at is NOT NULL but not non-empty — an absent date has to
    # drop the property, never publish "uploadDate": "".
    db.run("UPDATE assets SET created_at='' WHERE id=?", (undated,))

    page = client.get("/reels")
    assert page.status_code == 200
    payload = json.loads(
        re.search(
            r'<script type="application/ld\+json">\s*(\[.*?\])\s*</script>', page.text, re.S
        ).group(1)
    )
    by_url = {entry["contentUrl"].rsplit("/", 1)[-1]: entry for entry in payload}
    assert by_url[str(dated)]["uploadDate"]
    assert "uploadDate" not in by_url[str(undated)]


def test_portfolio_tiles_publish_the_generated_description(client, portfolio):
    """Argus writes a description per frame; the grid must ship it, not the tag.

    Before this, every starred F&B photo on the public site carried the byte-
    identical alt string — the description in `assets.argus_alt_text` was read
    only by an admin hover overlay (templates/admin/_gd_tile.html).
    """
    described = _seed_photo(portfolio, "described", "fb/dishes")
    db.run(
        "UPDATE assets SET argus_alt_text=? WHERE id=?",
        ("Seared scallops with brown butter on slate", described),
    )
    _seed_photo(portfolio, "plain", "fb/dishes")

    markup = client.get("/portfolio").text
    tags = _tags_for(markup, f"/site/img/{described}?variant=thumb")
    assert tags, "the described frame is missing from /portfolio"
    assert 'alt="Seared scallops with brown butter on slate"' in tags[0], tags[0]
    # The undescribed frame keeps the craft-phrase fallback, so the two tiles
    # no longer share one string.
    assert "food &amp; beverage photography by" in markup.lower()


def test_gallery_frames_publish_the_generated_description(client, delivery):
    """The client gallery's positional alt is a fallback, not the answer."""
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type) VALUES (?,?,?,1,'gallery')",
        ("alt-premiere", "Alt Premiere", "1234"),
    )
    delivery["galleries"].append(gid)
    described = _seed_delivery_asset(gid, "described", "photo", (4000, 3000))
    plain = _seed_delivery_asset(gid, "plain", "photo", (4000, 3000))
    db.run(
        "UPDATE assets SET argus_alt_text=? WHERE id=?",
        ("Corner banquette under a skylight", described),
    )

    assert client.post("/g/alt-premiere/pin", data={"pin": "1234"}).status_code == 200
    markup = client.get("/g/alt-premiere").text
    (described_tag,) = _tags_for(markup, f"/media/alt-premiere/thumb/{described}")
    assert 'alt="Corner banquette under a skylight"' in described_tag, described_tag
    assert "frame 00" not in described_tag, described_tag
    # An unanalyzed frame keeps the positional fallback.
    (plain_tag,) = _tags_for(markup, f"/media/alt-premiere/thumb/{plain}")
    assert f'alt="Alt Premiere &mdash; frame {plain:04d}"' in plain_tag or (
        f"frame {plain:04d}" in plain_tag
    ), plain_tag


def test_sitemap_carries_image_and_video_children(client, portfolio):
    """The frames ARE the product; a photographer absent from Google Images is
    invisible in half the search surface. Each page carries exactly the assets
    it renders, so the sitemap never promises a crawler a frame it cannot see."""
    fb = _seed_photo(portfolio, "plated", "fb/dishes")
    re_ = _seed_photo(portfolio, "kitchen", "re/interiors")
    reel = _seed_video(portfolio, "walkthrough", "re/interiors")

    xml = client.get("/sitemap.xml").text
    assert 'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"' in xml
    assert 'xmlns:video="http://www.google.com/schemas/sitemap-video/1.1"' in xml

    def block(path: str) -> str:
        marker = f"<loc>{config.BASE_URL}{path}</loc>"
        start = xml.index(marker)
        return xml[start : xml.index("</url>", start)]

    # /portfolio renders every starred photo, so it carries every one.
    assert f"/site/img/{fb}" in block("/portfolio")
    assert f"/site/img/{re_}" in block("/portfolio")
    # A spoke carries only its own specialty — the F&B frame must not appear
    # under /real-estate.
    assert f"/site/img/{re_}" in block("/real-estate")
    assert f"/site/img/{fb}" not in block("/real-estate")
    # Videos ride /reels with the fields Google requires.
    reels = block("/reels")
    assert f"<video:content_loc>{config.BASE_URL}/site/vid/{reel}</video:content_loc>" in reels
    assert f"<video:thumbnail_loc>{config.BASE_URL}/site/poster/{reel}</video:thumbnail_loc>" in (
        reels
    )
    assert "<video:title>" in reels and "<video:description>" in reels
    # Photos are not videos and vice versa.
    assert "<video:" not in block("/portfolio")
    assert "<image:" not in reels


def test_sitemap_video_titles_match_the_reels_json_ld(client, portfolio):
    """Google cross-checks a <video:video> against the page's VideoObject.

    Both read app/render.py, so this asserts the two cannot drift apart.
    """
    _seed_video(portfolio, "match", "fb/plating")
    xml = client.get("/sitemap.xml").text
    title = re.search(r"<video:title>(.*?)</video:title>", xml).group(1)
    description = re.search(r"<video:description>(.*?)</video:description>", xml).group(1)

    page = client.get("/reels").text
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    video_ld = [json.loads(b) for b in blocks if "VideoObject" in b][0]
    assert video_ld[0]["name"] == title
    assert video_ld[0]["description"] == description


def test_sitemap_omits_an_out_of_range_video_duration(client, portfolio):
    """Google rejects video:duration outside 1..28800s — omit it, don't lie."""
    reel = _seed_video(portfolio, "overlong", "re/interiors")
    db.run("UPDATE assets SET duration=? WHERE id=?", (99999, reel))
    assert "<video:duration>" not in client.get("/sitemap.xml").text

    db.run("UPDATE assets SET duration=? WHERE id=?", (42.4, reel))
    assert "<video:duration>42</video:duration>" in client.get("/sitemap.xml").text


def test_share_debugger_previews_the_og_image_the_pages_actually_emit(client, portfolio):
    """The debugger's whole contract is 'what shows here matches what the
    socials will scrape' (app/admin/share.py:_build_urls). It was previewing
    ORDER BY id (the OLDEST starred frame) while render._og_image_id emits
    ORDER BY id DESC (the newest), so the two disagreed the moment a second
    photo was starred."""
    from app.admin.share import _build_urls
    from app.render import _og_image_id

    _seed_photo(portfolio, "oldest", "fb/dishes")
    newest = _seed_photo(portfolio, "newest", "fb/dishes")

    assert _og_image_id() == newest
    home = [u for u in _build_urls() if u["path"] == "/"][0]
    assert home["og_image_id"] == newest, "the debugger is previewing a different frame"
    # And the live page agrees.
    assert f"/site/img/{newest}" in client.get("/").text


def _local_business_payload(markup: str) -> dict:
    """The ProfessionalService JSON-LD block a marketing page carries.

    json.loads is part of the assertion: the template splices optional
    properties with hand-placed commas, so a conditional that mis-nests
    breaks the whole payload, not just one field.
    """
    for block in re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>', markup, re.S
    ):
        payload = json.loads(block)
        if isinstance(payload, dict) and payload.get("@type") == "ProfessionalService":
            return payload
    raise AssertionError("no ProfessionalService JSON-LD on the page")


def test_local_business_schema_has_an_entity_id_and_omits_unconfigured_facts(client):
    payload = _local_business_payload(client.get("/contact").text)
    # A stable @id lets other structured data (reviews, articles) point at the
    # business as one entity across pages.
    assert payload["@id"] == config.BASE_URL + "/#business"
    # No MISE_BUSINESS_* is set in the test env: each fact must DROP its
    # property — never publish "", null, or an invented value.
    for absent in ("telephone", "openingHours", "geo"):
        assert absent not in payload


def test_local_business_schema_publishes_the_configured_facts(client, monkeypatch):
    monkeypatch.setattr(config, "BUSINESS_PHONE", "+18285550100")
    monkeypatch.setattr(config, "BUSINESS_HOURS", ("Mo-Fr 09:00-17:00", "Sa 10:00-14:00"))
    monkeypatch.setattr(config, "BUSINESS_GEO", (35.5, -82.5))

    payload = _local_business_payload(client.get("/contact").text)
    assert payload["telephone"] == "+18285550100"
    assert payload["openingHours"] == ["Mo-Fr 09:00-17:00", "Sa 10:00-14:00"]
    assert payload["geo"] == {
        "@type": "GeoCoordinates",
        "latitude": 35.5,
        "longitude": -82.5,
    }
