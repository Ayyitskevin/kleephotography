"""Crawlability and structured-data contracts for the public marketing surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


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
