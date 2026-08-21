"""ZIP_DIR eviction.

Every other prune in the codebase is per-gallery and fires only on a rebuild or
a delete. Nothing ever looked at ZIP_DIR as a whole, so a delivered gallery's
full archive — roughly the byte size of its originals — stayed forever and a
studio's disk crept toward twice its media library.

Deleting them is safe because they are a cache: ops/backup.sh already excludes
/zips/ as "rebuildable derivatives", and downloads.download_zip re-enqueues the
build whenever the archive it wants is absent.
"""

import os
import time

import pytest

from app import config, zip_cache

pytestmark = pytest.mark.unit


@pytest.fixture
def zips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path)
    monkeypatch.setattr(config, "ZIP_CACHE_TTL_DAYS", 30)
    return tmp_path


def _aged(path, days):
    path.write_bytes(b"PK\x03\x04" + b"z" * 500)
    old = time.time() - days * 86400
    os.utime(path, (old, old))
    return path


def test_an_idle_archive_is_evicted_and_a_fresh_one_is_not(zips):
    stale = _aged(zips / "g1-r3.zip", 45)
    fresh = _aged(zips / "g2-r1.zip", 2)

    result = zip_cache.sweep()

    assert not stale.exists()
    assert fresh.exists()
    assert result["evicted"] == 1
    assert result["bytes"] == 504


def test_serving_an_archive_resets_its_clock(zips):
    """Idleness, not age. A gallery a client keeps returning to must not age
    out from under them just because it was built months ago."""
    archive = _aged(zips / "g3-r1.zip", 90)
    zip_cache.touch(archive)

    assert zip_cache.sweep()["evicted"] == 0
    assert archive.exists()


def test_every_archive_shape_is_swept(zips):
    """Full-gallery, favourites, section and portal bundles all leak the same."""
    for name in ("g1-r2.zip", "g1-fav-a1b2c3d4.zip", "g1-s7-a1b2c3d4.zip", "p4-deadbeef.zip"):
        _aged(zips / name, 60)

    assert zip_cache.sweep()["evicted"] == 4
    assert list(zips.glob("*.zip")) == []


def test_orphaned_partials_are_cleaned_up_but_a_live_build_is_not(zips):
    """jobs.build_zip writes tmp-then-rename; a crash strands the .part.

    The window is deliberately far longer than any real build, so the sweep can
    never race one that is merely slow.
    """
    stranded = _aged(zips / "g9-r1.part", 3)
    building = _aged(zips / "g8-r1.part", 0)

    result = zip_cache.sweep()

    assert not stranded.exists()
    assert building.exists(), "swept a build that may still be running"
    assert result["partials"] == 1
    # Partials are not archives — they must not be counted as reclaimed cache.
    assert result["evicted"] == 0


def test_the_kill_switch_is_honest(zips, monkeypatch):
    monkeypatch.setattr(config, "ZIP_CACHE_TTL_DAYS", 0)
    archive = _aged(zips / "g1-r1.zip", 365)

    assert zip_cache.sweep() == {"evicted": 0, "bytes": 0, "partials": 0}
    assert archive.exists()


def test_a_missing_zip_dir_is_not_an_error(tmp_path, monkeypatch):
    """The sweep runs on the recurring loop; it must never take the loop down."""
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "nope")
    monkeypatch.setattr(config, "ZIP_CACHE_TTL_DAYS", 30)
    assert zip_cache.sweep()["evicted"] == 0


def test_nothing_but_archives_and_partials_is_touched(zips):
    _aged(zips / "g1-r1.zip", 60)
    keep = zips / "README"
    keep.write_text("not ours")
    old = time.time() - 400 * 86400
    os.utime(keep, (old, old))

    zip_cache.sweep()
    assert keep.exists()
