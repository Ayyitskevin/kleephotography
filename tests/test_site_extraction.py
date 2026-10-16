"""R-3: the media-spec and sitemap helpers moved out of ``site.py`` but stay reachable there.

Existing tests and other modules (``public/scheduling.py``) import these names from ``site``;
a monkeypatch on ``site.config`` must still steer the moved code (one shared module object)."""

import pytest

from app.public import site, site_media, sitemap

pytestmark = pytest.mark.unit

MEDIA_NAMES = ("_public_photo_spec", "_public_video_poster_spec", "_public_portfolio_tile_spec")
SITEMAP_NAMES = (
    "SITEMAP_MAX_IMAGES",
    "_image_entries",
    "_sitemap_day",
    "_sitemap_url",
    "_video_entries",
    "_xml_escape",
)


@pytest.mark.parametrize("name", MEDIA_NAMES)
def test_every_moved_media_helper_is_the_same_object_on_site(name):
    assert getattr(site, name) is getattr(site_media, name)
    assert getattr(site_media, name).__module__ == "app.public.site_media"


@pytest.mark.parametrize("name", SITEMAP_NAMES)
def test_every_moved_sitemap_helper_is_the_same_object_on_site(name):
    assert getattr(site, name) is getattr(sitemap, name)


def test_the_moved_code_reads_config_through_the_module_site_exposes():
    assert site.config is site_media.config is sitemap.config
