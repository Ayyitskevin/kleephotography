"""Public marketing media specs: derivative metadata for already public-gated assets.

Moved out of ``site.py`` unchanged (plan R-3); ``site`` re-exports each name."""

import logging
from pathlib import Path

from .. import config, imaging

log = logging.getLogger("mise.public.site")


def _public_photo_spec(asset) -> dict:
    """Actual derivative metadata for an already public-gated photo row.

    The database stores source dimensions, while generated JPEG sizes depend
    on the settings in force when the asset was processed. Probe only trusted
    ``thumb``/``web`` derivative headers so responsive descriptors describe
    the bytes visitors will really receive. Callers must pass rows already
    gated by ``portfolio=1 AND status='ready' AND kind='photo'``.
    """
    asset_id = asset["id"]
    gallery_id = asset["gallery_id"]
    stem = Path(asset["stored"]).stem

    def candidate(variant: str) -> dict:
        path = config.MEDIA_DIR / str(gallery_id) / variant / f"{stem}.jpg"
        dimensions = imaging.image_dimensions(path)
        return {
            "url": f"/site/img/{asset_id}?variant={variant}",
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        }

    thumb = candidate("thumb")
    web = candidate("web")

    def preferred(primary: dict, fallback: dict) -> dict:
        if primary["width"]:
            return primary
        if fallback["width"]:
            return fallback
        return primary

    # Width descriptors are valid only when both encoded candidates exist,
    # differ in width, and retain the same aspect ratio (allowing resize
    # rounding). A stale/cropped derivative stays usable as a plain src but is
    # never mixed into a misleading srcset.
    srcset = None
    if thumb["width"] and web["width"] and thumb["width"] != web["width"]:
        thumb_ratio = thumb["width"] / thumb["height"]
        web_ratio = web["width"] / web["height"]
        if abs(thumb_ratio - web_ratio) / web_ratio <= 0.01:
            candidates = sorted((thumb, web), key=lambda item: item["width"])
            srcset = ", ".join(f"{item['url']} {item['width']}w" for item in candidates)
        else:
            log.warning(
                "Public asset %s has mismatched derivative ratios: thumb=%sx%s web=%sx%s",
                asset_id,
                thumb["width"],
                thumb["height"],
                web["width"],
                web["height"],
            )

    return {
        "available": bool(thumb["width"] or web["width"]),
        "thumb": preferred(thumb, web),
        "web": preferred(web, thumb),
        "srcset": srcset,
    }


def _public_video_poster_spec(asset) -> dict:
    """Actual poster metadata for an already public-gated video row.

    Prefer the full poster rendition. If it is missing or corrupt, the
    independently public-gated video thumbnail is a truthful lower-resolution
    fallback for both ``poster`` and its matching preload. Callers must pass
    rows already gated by ``portfolio=1 AND status='ready' AND kind='video'``.
    """
    asset_id = asset["id"]
    gallery_id = asset["gallery_id"]
    stem = Path(asset["stored"]).stem
    candidates = (
        (
            config.MEDIA_DIR / str(gallery_id) / "web" / f"{stem}_poster.jpg",
            f"/site/poster/{asset_id}",
        ),
        (
            config.MEDIA_DIR / str(gallery_id) / "thumb" / f"{stem}.jpg",
            f"/site/img/{asset_id}?variant=thumb",
        ),
    )
    for path, url in candidates:
        dimensions = imaging.image_dimensions(path)
        if dimensions:
            return {
                "available": True,
                "url": url,
                "width": dimensions[0],
                "height": dimensions[1],
            }
    return {"available": False, "url": None, "width": None, "height": None}


def _public_portfolio_tile_spec(asset) -> dict:
    """Actual preview metadata for one already public-gated masonry asset.

    Photos reuse the responsive derivative contract. Video tiles deliberately
    keep their lightweight thumbnail rather than pulling the larger lightbox
    poster into the initial grid; a separately validated poster spec is only
    carried into the on-demand lightbox. Missing or corrupt thumbnails remain
    the existing lazy fallback and are never promoted as known-good media.
    """
    if asset["kind"] == "photo":
        return _public_photo_spec(asset)
    if asset["kind"] != "video":
        raise ValueError(f"Unsupported portfolio asset kind: {asset['kind']}")

    asset_id = asset["id"]
    stem = Path(asset["stored"]).stem
    path = config.MEDIA_DIR / str(asset["gallery_id"]) / "thumb" / f"{stem}.jpg"
    dimensions = imaging.image_dimensions(path)
    return {
        "available": bool(dimensions),
        "thumb": {
            "url": f"/site/img/{asset_id}?variant=thumb",
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        },
        "poster": _public_video_poster_spec(asset),
        "srcset": None,
    }
