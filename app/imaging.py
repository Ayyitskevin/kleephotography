"""Pillow pipeline: sRGB convert, orientation bake, metadata strip, derivatives.

F&B work is color-critical — embedded ICC profiles are converted to sRGB, not
dropped. Saved derivatives carry no EXIF (GPS and camera data stripped by
re-encoding clean).
"""

import io
import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageCms, ImageFilter, ImageOps, UnidentifiedImageError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("mise.imaging")

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

_SRGB = ImageCms.createProfile("sRGB")


@lru_cache(maxsize=2048)
def _image_header_dimensions(
    path: str, identity: tuple[int, int, int, int, int]
) -> tuple[int, int] | None:
    """Read one image header, caching success or failure by file identity."""
    del identity  # cache-key material; the path is all Pillow needs
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        log.warning("Could not read image dimensions for %s: %s", path, exc)
        return None


def image_dimensions(path: str | Path) -> tuple[int, int] | None:
    """Return actual encoded dimensions without decoding the image payload.

    Missing derivatives are a supported legacy state and return ``None``.
    Corrupt files fail visibly in logs while callers can still render their
    existing placeholder/fallback behavior.
    """
    candidate = Path(path)
    try:
        stat = candidate.stat()
    except OSError:
        return None
    identity = (
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
    )
    return _image_header_dimensions(str(candidate), identity)


def _to_srgb(img: Image.Image) -> Image.Image:
    icc = img.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(img, src, _SRGB, outputMode="RGB")
        except (ImageCms.PyCMSError, Exception) as e:  # pragma: no cover
            log.warning("ICC convert failed (%s) — assuming sRGB", e)
            img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


_ANCHORS = {  # 9-grid → (x, y) fractions of the *free* space after the logo + margin
    "tl": (0.0, 0.0),
    "tc": (0.5, 0.0),
    "tr": (1.0, 0.0),
    "ml": (0.0, 0.5),
    "c": (0.5, 0.5),
    "mr": (1.0, 0.5),
    "bl": (0.0, 1.0),
    "bc": (0.5, 1.0),
    "br": (1.0, 1.0),
}


def _apply_overlay(crop: Image.Image, overlay: dict) -> Image.Image:
    """Composite a brand logo onto a crop. Pure: the logo path + placement params
    are passed in (caller resolves the brand kit). `overlay` keys: path, position,
    opacity (0-100), scale_pct (logo width as % of crop width), margin_pct."""
    cw, ch = crop.size
    with Image.open(overlay["path"]) as raw:
        logo = raw.convert("RGBA")
    target_w = max(1, round(cw * overlay["scale_pct"] / 100))
    target_h = max(1, round(logo.height * target_w / logo.width))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    opacity = overlay["opacity"] / 100
    if opacity < 1:
        alpha = logo.getchannel("A").point(lambda a: round(a * opacity))
        logo.putalpha(alpha)

    margin = round(cw * overlay["margin_pct"] / 100)
    fx, fy = _ANCHORS.get(overlay["position"], _ANCHORS["br"])
    x = round((cw - target_w - 2 * margin) * fx) + margin
    y = round((ch - target_h - 2 * margin) * fy) + margin

    base = crop.convert("RGBA")
    # Contrast scrim: a soft dark halo derived from the logo's own alpha,
    # composited under the logo so a light wordmark stays legible on a bright
    # dish (steam, white plates, sunlit tabletops). Blur scales with logo size;
    # it tracks the logo's post-opacity alpha, so it never appears where the
    # logo is transparent and fades with a faded logo. A small downward offset
    # reads as a drop shadow; on a dark background it's invisible (no harm).
    blur = max(2, round(target_w * 0.03))
    pad = blur * 2
    sh = Image.new("L", (target_w + 2 * pad, target_h + 2 * pad), 0)
    sh.paste(logo.getchannel("A"), (pad, pad))
    sh = sh.filter(ImageFilter.GaussianBlur(blur)).point(lambda a: round(a * 0.7))
    shadow = Image.new("RGBA", sh.size, (0, 0, 0, 0))
    shadow.putalpha(sh)
    off = max(1, round(target_h * 0.02))
    base.alpha_composite(shadow, (x - pad, y - pad + off))
    base.alpha_composite(logo, (x, y))
    return base.convert("RGB")


def make_crops(
    src_path: str, out_dir, stem: str, quality: int, presets, overlay: dict | None = None
) -> list[str]:
    """Center-crop the original to each preset. ONE generic render path: every
    preset row is rendered the same way — a new format is a new row, not new
    code. `presets` is a list of crop_presets rows (see app/presets.py); this
    module stays pure (no DB), the caller owns loading. Returns written filenames.

    Brand overlay is a branch INSIDE this one path, not a parallel one: it fires
    only when the preset opts in (brand_overlay=1) AND an `overlay` spec is passed.
    With brand_overlay=0 or overlay=None the output is byte-identical to no overlay.
    """
    written = []
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        im = _to_srgb(im)
        for ps in presets:
            crop = ImageOps.fit(
                im,
                (ps["width"], ps["height"]),
                Image.LANCZOS,
                centering=(ps["centering_x"], ps["centering_y"]),
            )
            if ps["brand_overlay"] and overlay:
                crop = _apply_overlay(crop, overlay)
            out = out_dir / f"{stem}_{ps['slug']}.jpg"
            crop.save(out, "JPEG", quality=quality, progressive=True, optimize=True)
            written.append(out.name)
    return written


def make_derivatives(
    src_path: str, web_path: str, thumb_path: str, web_max: int, thumb_max: int, quality: int
) -> tuple[int, int]:
    """Returns original (width, height) after orientation bake."""
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        im = _to_srgb(im)

        web = im.copy()
        web.thumbnail((web_max, web_max), Image.LANCZOS)
        web.save(web_path, "JPEG", quality=quality, progressive=True, optimize=True)

        im.thumbnail((thumb_max, thumb_max), Image.LANCZOS)
        im.save(thumb_path, "JPEG", quality=quality, progressive=True, optimize=True)
    return w, h
