"""Unit tests for imaging module (pure Pillow logic).

Start of deeper test extraction in next phase.
"""

import io
import os
import tempfile

import pytest
from PIL import Image

from app import imaging


def _make_test_image(w=400, h=300, color=(100, 150, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    buf.seek(0)
    return buf.read()


@pytest.mark.unit
def test_make_derivatives_basic():
    src_bytes = _make_test_image(400, 300)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.jpg")
        with open(src, "wb") as f:
            f.write(src_bytes)
        web = os.path.join(tmp, "web.jpg")
        thumb = os.path.join(tmp, "thumb.jpg")
        w, h = imaging.make_derivatives(src, web, thumb, 200, 100, 85)
        assert w == 400
        assert h == 300
        assert os.path.exists(web)
        assert os.path.exists(thumb)
        # Check sizes roughly
        with Image.open(web) as im:
            assert im.size[0] <= 200
        with Image.open(thumb) as im:
            assert im.size[0] <= 100


def test_to_srgb_no_icc():
    # Simple test that non-icc image converts to RGB
    img = Image.new("RGB", (10, 10), (255, 0, 0))
    result = imaging._to_srgb(img)
    assert result.mode == "RGB"
