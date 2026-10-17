"""Sitemap XML builders for the public marketing site.

Moved out of ``site.py`` unchanged (plan R-3); ``site`` re-exports each name and keeps the
``/sitemap.xml`` route, so it still reads its data through ``site``'s own names."""

from datetime import UTC, datetime

from .. import config
from ..render import _reel_description, _reel_title


def _sitemap_day(value) -> str:
    """W3C Datetime date portion (YYYY-MM-DD) for <lastmod>."""
    if value is None or value == "":
        return datetime.now(UTC).strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%d")
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m-%d")
        )
    except ValueError:
        return datetime.now(UTC).strftime("%Y-%m-%d")


# Google caps a sitemap URL at 1000 images. The portfolio is nowhere near that,
# but an unbounded loop over a growing table is how a sitemap silently becomes
# invalid years later.
SITEMAP_MAX_IMAGES = 1000


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _image_entries(assets) -> str:
    """<image:image> children for one <url>, capped at Google's per-URL limit."""
    return "".join(
        f"<image:image><image:loc>{config.BASE_URL}/site/img/{a['id']}</image:loc></image:image>"
        for a in assets[:SITEMAP_MAX_IMAGES]
    )


def _video_entries(reels) -> str:
    """<video:video> children for one <url>.

    Title and description come from app/render.py, the same helpers /reels uses
    for its VideoObject JSON-LD — Google cross-checks the two and a forked
    formula would make them disagree.
    """
    out = []
    for r in reels[:SITEMAP_MAX_IMAGES]:
        duration = ""
        if r["duration"]:
            seconds = int(round(float(r["duration"])))
            # Google rejects a video:duration outside 1..28800 seconds.
            if 1 <= seconds <= 28800:
                duration = f"<video:duration>{seconds}</video:duration>"
        out.append(
            "<video:video>"
            f"<video:thumbnail_loc>{config.BASE_URL}/site/poster/{r['id']}</video:thumbnail_loc>"
            f"<video:title>{_xml_escape(_reel_title(r))}</video:title>"
            f"<video:description>{_xml_escape(_reel_description(r))}</video:description>"
            f"<video:content_loc>{config.BASE_URL}/site/vid/{r['id']}</video:content_loc>"
            f"{duration}"
            "</video:video>"
        )
    return "".join(out)


def _sitemap_url(path: str, lastmod: str, children: str = "") -> str:
    return f"<url><loc>{config.BASE_URL}{path}</loc><lastmod>{lastmod}</lastmod>{children}</url>"
