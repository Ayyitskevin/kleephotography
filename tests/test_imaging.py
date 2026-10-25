"""Unit tests for imaging module (pure Pillow logic).

Start of deeper test extraction in next phase.
"""

import io
import os
import tempfile

import pytest
from PIL import Image, ImageCms

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


# ── The real Pillow calls an upload makes ───────────────────────────────────
#
# A Pillow major bump (12 → 13) is a dependency change the rest of the suite
# barely exercises: it never feeds an oriented, profiled, or non-JPEG upload
# through the pipeline. These do, end to end, through the public functions.


def _left_red_right_blue(w=400, h=300) -> Image.Image:
    """Orientation is readable from the pixels: left half red, right half blue."""
    img = Image.new("RGB", (w, h), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, w // 2, h))
    return img


def _colour_at(im: Image.Image, x: float, y: float) -> str:
    r, _, b = im.convert("RGB").getpixel((int(im.width * x), int(im.height * y)))
    return "red" if r > b else "blue"


@pytest.mark.unit
def test_orientation_is_baked_and_exif_is_stripped_from_every_output(tmp_path):
    """A phone shot stored landscape with Orientation=6 must come out portrait,
    right way up, and no derivative or crop may carry its EXIF (GPS included)."""
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display
    exif[0x010F] = "TestCam"  # Make
    exif.get_ifd(0x8825)[2] = (40.0, 26.0, 46.0)  # GPSLatitude
    src = tmp_path / "phone.jpg"
    _left_red_right_blue().save(src, "JPEG", quality=95, exif=exif)
    with Image.open(src) as im:
        assert im.getexif()[0x0112] == 6  # the fixture really carries the tag

    web, thumb = tmp_path / "w.jpg", tmp_path / "t.jpg"
    assert imaging.make_derivatives(str(src), str(web), str(thumb), 200, 100, 85) == (300, 400)

    logo = tmp_path / "logo.png"
    Image.new("RGBA", (120, 40), (255, 255, 255, 200)).save(logo, "PNG")
    overlay = {
        "path": str(logo),
        "position": "br",
        "opacity": 80,
        "scale_pct": 20,
        "margin_pct": 2,
    }
    preset = {
        "slug": "sq",
        "width": 100,
        "height": 100,
        "centering_x": 0.5,
        "centering_y": 0.5,
        "brand_overlay": 1,
    }
    crops = imaging.make_crops(str(src), tmp_path, "phone", 85, [preset], overlay=overlay)
    assert crops == ["phone_sq.jpg"]

    siblings = [tmp_path / f"{s}.{ext}" for s in ("w", "t") for ext in imaging.MODERN_FORMATS]
    for out in [web, thumb, tmp_path / "phone_sq.jpg", *siblings]:
        with Image.open(out) as im:
            assert im.height >= im.width, out.name
            # Rotated 90° clockwise: the source's left (red) half is now on top.
            assert _colour_at(im, 0.5, 0.2) == "red", out.name
            assert _colour_at(im, 0.5, 0.8) == "blue", out.name
            assert not im.getexif(), out.name
            assert "exif" not in im.info, out.name


@pytest.mark.unit
def test_an_embedded_icc_profile_goes_through_littlecms(tmp_path, monkeypatch):
    """F&B colour depends on the profile being converted, not dropped. The
    except branch ("assume sRGB") is silent about it, so prove the real
    conversion ran and its output is what was saved."""
    calls = []
    real = ImageCms.profileToProfile

    def spy(im, src, dst, **kw):
        out = real(im, src, dst, **kw)
        calls.append(out.mode)
        return out

    monkeypatch.setattr(imaging.ImageCms, "profileToProfile", spy)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    src = tmp_path / "profiled.jpg"
    Image.new("RGB", (64, 64), (200, 100, 50)).save(src, "JPEG", quality=100, icc_profile=icc)

    web, thumb = tmp_path / "w.jpg", tmp_path / "t.jpg"
    imaging.make_derivatives(str(src), str(web), str(thumb), 32, 16, 95)

    assert calls == ["RGB"]
    with Image.open(web) as im:
        assert all(abs(a - b) <= 4 for a, b in zip(im.getpixel((16, 16)), (200, 100, 50)))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ext", "pil_format", "mode"),
    [
        ("png", "PNG", "RGBA"),
        ("webp", "WEBP", "RGB"),
        ("tif", "TIFF", "RGB"),
        ("heic", "HEIF", "RGB"),
    ],
)
def test_every_accepted_still_format_becomes_a_jpeg_of_record(tmp_path, ext, pil_format, mode):
    """Each non-JPEG upload type in PHOTO_EXTS decodes (HEIC via pillow-heif)
    and is re-encoded as the JPEG plus its modern siblings."""
    assert f".{ext}" in imaging.PHOTO_EXTS
    src = tmp_path / f"upload.{ext}"
    _left_red_right_blue().convert(mode).save(src, pil_format)

    web, thumb = tmp_path / "w.jpg", tmp_path / "t.jpg"
    assert imaging.make_derivatives(str(src), str(web), str(thumb), 200, 100, 85) == (400, 300)

    for out in [web, *(tmp_path / f"w.{e}" for e in imaging.MODERN_FORMATS)]:
        with Image.open(out) as im:
            assert im.size == (200, 150), out.name
            assert _colour_at(im, 0.2, 0.5) == "red", out.name
            assert _colour_at(im, 0.8, 0.5) == "blue", out.name
    with Image.open(web) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
