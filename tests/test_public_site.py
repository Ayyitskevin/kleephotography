"""Public marketing image metadata contracts."""

import pytest
from PIL import Image

from app.public import site

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("thumb_size", "web_size", "warns"),
    [
        ((80, 120), (320, 200), True),
        ((80, 120), (80, 120), False),
    ],
)
def test_public_photo_spec_omits_invalid_srcsets(
    tmp_path, monkeypatch, caplog, thumb_size, web_size, warns
):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    asset = {"id": 17, "gallery_id": 9, "stored": "frame.jpg"}

    for variant, size in (("thumb", thumb_size), ("web", web_size)):
        directory = tmp_path / "9" / variant
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (20, 40, 60)).save(directory / "frame.jpg", "JPEG")

    spec = site._public_photo_spec(asset)

    assert spec["available"] is True
    assert spec["srcset"] is None
    assert (spec["thumb"]["width"], spec["thumb"]["height"]) == thumb_size
    assert (spec["web"]["width"], spec["web"]["height"]) == web_size
    assert ("mismatched derivative ratios" in caplog.text) is warns


def test_public_video_poster_spec_prefers_poster_then_thumb(tmp_path, monkeypatch):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    asset = {"id": 23, "gallery_id": 9, "stored": "reel.mp4"}
    web = tmp_path / "9" / "web"
    thumb = tmp_path / "9" / "thumb"
    web.mkdir(parents=True)
    thumb.mkdir(parents=True)
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "reel_poster.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "reel.jpg", "JPEG")

    poster = site._public_video_poster_spec(asset)
    assert poster == {
        "available": True,
        "url": "/site/poster/23",
        "width": 320,
        "height": 180,
    }

    (web / "reel_poster.jpg").unlink()
    fallback = site._public_video_poster_spec(asset)
    assert fallback == {
        "available": True,
        "url": "/site/img/23?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "reel.jpg").unlink()
    assert site._public_video_poster_spec(asset) == {
        "available": False,
        "url": None,
        "width": None,
        "height": None,
    }


def test_public_specs_reject_corrupt_derivatives(tmp_path, monkeypatch):
    monkeypatch.setattr(site.config, "MEDIA_DIR", tmp_path)
    gallery = tmp_path / "9"
    web = gallery / "web"
    thumb = gallery / "thumb"
    web.mkdir(parents=True)
    thumb.mkdir(parents=True)

    photo = {"id": 31, "gallery_id": 9, "stored": "frame.jpg"}
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "frame.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "frame.jpg", "JPEG")
    (web / "frame.jpg").write_bytes(b"not an image")

    photo_fallback = site._public_photo_spec(photo)
    assert photo_fallback["available"] is True
    assert photo_fallback["srcset"] is None
    assert photo_fallback["web"] == {
        "url": "/site/img/31?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "frame.jpg").write_bytes(b"also not an image")
    assert site._public_photo_spec(photo)["available"] is False

    video = {"id": 32, "gallery_id": 9, "stored": "reel.mp4"}
    Image.new("RGB", (320, 180), (20, 40, 60)).save(web / "reel_poster.jpg", "JPEG")
    Image.new("RGB", (80, 45), (20, 40, 60)).save(thumb / "reel.jpg", "JPEG")
    (web / "reel_poster.jpg").write_bytes(b"not an image")

    assert site._public_video_poster_spec(video) == {
        "available": True,
        "url": "/site/img/32?variant=thumb",
        "width": 80,
        "height": 45,
    }

    (thumb / "reel.jpg").write_bytes(b"also not an image")
    assert site._public_video_poster_spec(video) == {
        "available": False,
        "url": None,
        "width": None,
        "height": None,
    }
