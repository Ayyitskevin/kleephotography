"""Eviction for ZIP_DIR — the one durable directory that only ever grew.

Every other prune in the codebase is per-gallery and only fires on a rebuild or
a delete: `jobs._h_zip` drops superseded revisions of the gallery it just built
(`g{id}-r*.zip`), `public/downloads` and `jobs._h_zip_subset` drop the superseded
favourites/section bundles, and `admin.galleries.delete_gallery` cleans up after
itself. Nothing ever looked at ZIP_DIR as a whole. So a delivered gallery's full
archive — roughly the byte size of its originals — stayed on disk forever, and a
studio's disk usage quietly approached twice its media library.

Deleting them is safe because they are a cache, not a record, and the repo
already says so in two places: `ops/backup.sh` excludes `/zips/` as "rebuildable
derivatives", and `downloads.download_zip` re-enqueues a `zip_build` and shows
the wait page whenever the archive it wants is absent. An evicted archive costs
the next requester a rebuild, nothing more.

Kill switch: MISE_ZIP_CACHE_TTL_DAYS=0 disables eviction entirely.
"""

import logging
import time
from pathlib import Path

from . import config

log = logging.getLogger("mise.zip_cache")

# A crashed build leaves a `.part` behind (jobs.build_zip writes tmp-then-rename).
# Nothing ever reads one, but they were never cleaned up either. The window is
# deliberately generous — far longer than any real build — so a sweep can never
# race a build that is simply slow.
PART_MAX_AGE_SECONDS = 24 * 3600


def _evict(path: Path, now: float, max_age: float) -> int:
    """Delete `path` if it is older than max_age; return the bytes reclaimed."""
    try:
        st = path.stat()
    except OSError:  # vanished under us — someone else's prune got there first
        return 0
    if now - st.st_mtime <= max_age:
        return 0
    try:
        path.unlink()
    except OSError:
        log.warning("could not evict %s", path.name, exc_info=True)
        return 0
    return st.st_size


def sweep() -> dict:
    """Evict archives untouched for longer than the TTL, plus orphaned partials.

    mtime is the clock, and `touch()` below keeps it meaning "last built or last
    served" — so an archive a client is still coming back to never ages out
    while they are using it.
    """
    ttl_days = config.ZIP_CACHE_TTL_DAYS
    if ttl_days <= 0:
        return {"evicted": 0, "bytes": 0, "partials": 0}
    if not config.ZIP_DIR.is_dir():
        return {"evicted": 0, "bytes": 0, "partials": 0}

    now = time.time()
    max_age = ttl_days * 86400
    evicted = freed = partials = 0

    for path in config.ZIP_DIR.glob("*.zip"):
        size = _evict(path, now, max_age)
        if size:
            evicted += 1
            freed += size
    for path in config.ZIP_DIR.glob("*.part"):
        if _evict(path, now, PART_MAX_AGE_SECONDS):
            partials += 1

    if evicted or partials:
        log.info(
            "zip cache swept: %s archive(s) / %.1f MB reclaimed, %s orphaned partial(s)",
            evicted,
            freed / 1e6,
            partials,
        )
    return {"evicted": evicted, "bytes": freed, "partials": partials}


def touch(path: Path) -> None:
    """Mark an archive as used, so the sweep measures idleness and not age.

    Best-effort: a read-only or vanished file must never fail a download that is
    otherwise about to succeed.
    """
    try:
        path.touch()
    except OSError:
        pass
