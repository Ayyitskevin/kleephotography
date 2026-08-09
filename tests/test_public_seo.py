"""Crawlability, responsive-metadata and structured-data contracts for the
public marketing surface."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db
from app.main import app

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
