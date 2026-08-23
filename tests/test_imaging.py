"""Unit tests for imaging module (pure Pillow logic).

Start of deeper test extraction in next phase.
"""

import io
import os
import tempfile

import pytest
from PIL import Image

from app import imaging

pytestmark = pytest.mark.unit


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


def test_image_dimensions_reads_actual_header_and_invalidates_cache(tmp_path):
    derivative = tmp_path / "derivative.jpg"
    Image.new("RGB", (73, 109), (10, 20, 30)).save(derivative, "JPEG")

    assert imaging.image_dimensions(derivative) == (73, 109)

    # Replacing a derivative at the same path must not leave stale cached
    # metadata; its filesystem identity is part of the cache key.
    Image.new("RGB", (41, 29), (30, 20, 10)).save(derivative, "JPEG")
    assert imaging.image_dimensions(derivative) == (41, 29)


def test_image_dimensions_handles_missing_and_corrupt_files(tmp_path, caplog):
    missing = tmp_path / "missing.jpg"
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not a jpeg")

    assert imaging.image_dimensions(missing) is None
    assert imaging.image_dimensions(corrupt) is None
    assert imaging.image_dimensions(corrupt) is None
    assert caplog.text.count("Could not read image dimensions") == 1


def test_to_srgb_no_icc():
    # Simple test that non-icc image converts to RGB
    img = Image.new("RGB", (10, 10), (255, 0, 0))
    result = imaging._to_srgb(img)
    assert result.mode == "RGB"


# ── Modern still formats (AVIF / WebP alongside the JPEG) ────────────────────


@pytest.mark.unit
def test_accepted_formats_requires_an_exact_type_match():
    """`image/*` and `*/*` must NOT qualify.

    Every browser sends one of those; only the ones that actually decode AVIF
    name it. Treating a wildcard as consent is how you serve an AVIF to a
    browser that renders a broken image.
    """
    assert imaging.accepted_formats("image/avif,image/webp,image/*,*/*;q=0.8") == {"avif", "webp"}
    assert imaging.accepted_formats("image/webp,image/*,*/*;q=0.8") == {"webp"}
    assert imaging.accepted_formats("image/*,*/*") == set()
    assert imaging.accepted_formats("*/*") == set()
    assert imaging.accepted_formats("") == set()
    assert imaging.accepted_formats(None) == set()


@pytest.mark.unit
def test_accepted_formats_honours_q_zero():
    """q=0 means 'not acceptable', not 'least preferred'."""
    assert imaging.accepted_formats("image/avif;q=0,image/webp") == {"webp"}
    assert imaging.accepted_formats("image/avif;q=0.0,image/webp;q=0") == set()
    # A malformed q is treated as a refusal rather than silently accepted.
    assert imaging.accepted_formats("image/avif;q=banana") == set()
    assert imaging.accepted_formats("image/avif; q=0.5") == {"avif"}


@pytest.mark.unit
def test_make_derivatives_writes_siblings_without_touching_the_jpeg():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src.jpg")
        web = os.path.join(d, "w.jpg")
        thumb = os.path.join(d, "t.jpg")
        Image.new("RGB", (1200, 800), (90, 120, 60)).save(src, "JPEG", quality=92)

        w, h = imaging.make_derivatives(src, web, thumb, 800, 200, 85)
        assert (w, h) == (1200, 800)
        # The JPEG is still the file of record, at the right size.
        assert os.path.exists(web) and os.path.exists(thumb)
        assert Image.open(web).size == (800, 533)
        for ext in imaging.MODERN_FORMATS:
            assert os.path.exists(os.path.join(d, f"w.{ext}")), ext
            assert os.path.exists(os.path.join(d, f"t.{ext}")), ext
            # A sibling is the same picture at the same size, not a re-crop.
            assert Image.open(os.path.join(d, f"w.{ext}")).size == (800, 533)


@pytest.mark.unit
def test_negotiate_falls_back_whenever_the_sibling_is_missing():
    """An old gallery with no siblings on disk must behave exactly as before —
    that is what makes this safe to deploy with no backfill."""
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        jpeg = Path(d) / "only.jpg"
        Image.new("RGB", (40, 30)).save(jpeg, "JPEG")

        path, mime = imaging.negotiate(jpeg, "image/avif,image/webp,*/*")
        assert path == jpeg and mime == "image/jpeg"

        # Sibling present but not accepted -> still the JPEG.
        Image.new("RGB", (40, 30)).save(Path(d) / "only.webp", "WEBP")
        path, mime = imaging.negotiate(jpeg, "image/*,*/*")
        assert path == jpeg and mime == "image/jpeg"

        path, mime = imaging.negotiate(jpeg, "image/webp")
        assert path.name == "only.webp" and mime == "image/webp"


@pytest.mark.unit
def test_modern_formats_kill_switch_is_honest(monkeypatch):
    """MISE_MODERN_IMAGE_FORMATS= must serve JPEG even with siblings on disk."""
    from pathlib import Path

    from app import config

    with tempfile.TemporaryDirectory() as d:
        jpeg = Path(d) / "k.jpg"
        Image.new("RGB", (40, 30)).save(jpeg, "JPEG")
        Image.new("RGB", (40, 30)).save(Path(d) / "k.webp", "WEBP")

        monkeypatch.setattr(config, "MODERN_IMAGE_FORMATS", ())
        path, mime = imaging.negotiate(jpeg, "image/avif,image/webp")
        assert path == jpeg and mime == "image/jpeg"

        # and nothing new is written
        out = Path(d) / "fresh.jpg"
        Image.new("RGB", (60, 40)).save(out, "JPEG")
        assert imaging.write_modern_siblings(Image.open(out), out) == []


@pytest.mark.unit
def test_a_codec_failure_is_survivable(monkeypatch, caplog):
    """A host whose Pillow lacks libavif must still finish the upload.

    The JPEG is the file of record precisely so a missing codec is a degraded
    experience, not a failed ingest and a stuck 'processing' asset.
    """
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "s.jpg")
        Image.new("RGB", (300, 200), (10, 20, 30)).save(src, "JPEG")

        real_save = Image.Image.save

        def explode(self, fp, fmt=None, **kw):
            if fmt in ("AVIF", "WEBP"):
                raise OSError("encoder not available")
            return real_save(self, fp, fmt, **kw)

        monkeypatch.setattr(Image.Image, "save", explode)
        w, h = imaging.make_derivatives(
            src, os.path.join(d, "w.jpg"), os.path.join(d, "t.jpg"), 200, 80, 85
        )

    assert (w, h) == (300, 200)  # the asset still gets its dimensions
