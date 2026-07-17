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
