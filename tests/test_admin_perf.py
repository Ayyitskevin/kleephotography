"""Cost contracts for the admin gallery surface.

These pin how a page is BUILT, not only what it renders: the library strips must
not stat-walk the media tree, and a per-tile HTMX action must not rebuild the
whole bench. A regression here shows up as latency on a large archive, which no
output assertion would catch — so the spies are the point.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, gallery_comments
from app.admin import common, galleries
from app.main import app


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert response.status_code == 303
        yield client


def _gallery(slug: str, title: str, gallery_type: str = "gallery") -> int:
    return db.run(
        "INSERT INTO galleries (slug, title, pin, type, published) VALUES (?,?,'1234',?,1)",
        (slug, title, gallery_type),
    )


def _asset(gallery_id: int, kind: str, size: int | None = None, section_id: int | None = None):
    return db.run(
        """INSERT INTO assets (gallery_id, section_id, kind, filename, stored, status, bytes)
           VALUES (?,?,?,?,?,'ready',?)""",
        (gallery_id, section_id, kind, f"{kind}.bin", f"{kind}-stored.bin", size),
    )


def _derivative_on_disk(gallery_id: int, size: int) -> None:
    """A file the media tree carries but no row records — the gap between
    "originals" and on-disk usage that the strips must not blur."""
    sub = config.MEDIA_DIR / str(gallery_id) / "thumb"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "derivative.jpg").write_bytes(b"\0" * size)


def _walk_spy(monkeypatch) -> list[str]:
    """Record every media-tree walk a render attempts, by either route."""
    walks: list[str] = []
    real_rglob = Path.rglob

    def spy_rglob(self, *args, **kwargs):
        walks.append(str(self))
        return real_rglob(self, *args, **kwargs)

    def spy_dir_size(path: Path) -> int:
        walks.append(str(path))
        return 0

    monkeypatch.setattr(Path, "rglob", spy_rglob)
    monkeypatch.setattr(common, "dir_size", spy_dir_size)
    return walks


def _connection_spy(monkeypatch) -> list[str]:
    """Count new db.connect() calls after the spy is installed.

    Reads reuse a per-thread connection, so this is no longer "queries issued".
    Writes and tx() still open their own. The star-toggle tests compare two
    benches of different sizes: the count must not scale with the bench.
    """
    opened: list[str] = []
    real_connect = db.connect

    def spy_connect():
        opened.append("db")
        return real_connect()

    monkeypatch.setattr(db, "connect", spy_connect)
    return opened


def _section(gallery_id: int, name: str) -> int:
    return db.run(
        "INSERT INTO sections (gallery_id, name, position) VALUES (?,?,0)", (gallery_id, name)
    )


def _note(gallery_id: int, asset_id: int, body: str) -> int:
    return db.run(
        """INSERT INTO video_comments (asset_id, gallery_id, author_role, timecode, body)
           VALUES (?,?,'admin',0,?)""",
        (asset_id, gallery_id, body),
    )


def _bench_gallery(
    slug: str, n_photos: int, n_videos: int
) -> tuple[int, int, list[int], list[int]]:
    """A gallery big enough that a whole-bench rebuild is visibly more
    expensive than a one-tile render: photos, videos, and a note per video."""
    gallery_id = _gallery(slug, slug)
    section_id = _section(gallery_id, "Hero Dishes")
    photos = [_asset(gallery_id, "photo", 100, section_id) for _ in range(n_photos)]
    videos = [_asset(gallery_id, "video", 200, section_id) for _ in range(n_videos)]
    for asset_id in videos:
        _note(gallery_id, asset_id, f"note on {asset_id}")
    return gallery_id, section_id, photos, videos


def _originals_total(gallery_type: str) -> int:
    return db.one(
        """SELECT COALESCE(SUM(a.bytes), 0) AS bytes FROM assets a
           JOIN galleries g ON g.id = a.gallery_id WHERE g.type = ?""",
        (gallery_type,),
    )["bytes"]


@pytest.mark.integration
def test_gallery_library_strip_sums_assets_bytes_without_walking_media(admin_client, monkeypatch):
    gallery_id = _gallery("perf-library", "Perf library")
    _asset(gallery_id, "photo", 1_500_000)
    _asset(gallery_id, "photo", 2_500_000)
    _derivative_on_disk(gallery_id, 3_000_000)

    walks = _walk_spy(monkeypatch)
    page = admin_client.get("/admin/galleries")

    assert page.status_code == 200
    assert walks == []
    assert f"{common.fmt_size(_originals_total('gallery'))} originals" in page.text


@pytest.mark.integration
def test_transfer_sizes_come_from_assets_bytes_without_walking_media(admin_client, monkeypatch):
    drop_id = _gallery("perf-drop", "Perf drop", "drop")
    _asset(drop_id, "photo", 3_000_000)
    _derivative_on_disk(drop_id, 4_000_000)

    walks = _walk_spy(monkeypatch)
    page = admin_client.get("/admin/transfers")

    assert page.status_code == 200
    assert walks == []
    assert common.fmt_size(3_000_000) in page.text
    assert f"{common.fmt_size(_originals_total('drop'))} of originals" in page.text


@pytest.mark.integration
def test_bench_reads_every_video_thread_in_one_query(admin_client, monkeypatch):
    gallery_id, _, _, videos = _bench_gallery("perf-threads", 4, 3)

    calls: list[list[int]] = []
    real_threads = galleries.video_comment_threads

    def spy_threads(asset_ids):
        ids = list(asset_ids)
        calls.append(ids)
        return real_threads(ids)

    monkeypatch.setattr(galleries, "video_comment_threads", spy_threads)
    page = admin_client.get(f"/admin/galleries/{gallery_id}")

    assert page.status_code == 200
    # one batched call for every video on the bench, not one call per video
    assert len(calls) == 1 and sorted(calls[0]) == sorted(videos)
    for asset_id in videos:
        assert f"note on {asset_id}" in page.text


@pytest.mark.integration
def test_video_comment_threads_groups_and_matches_the_single_asset_call():
    gallery_id, _, _, videos = _bench_gallery("perf-threads-shape", 0, 3)
    with_reply, hidden_only, quiet = videos
    root = db.one("SELECT id FROM video_comments WHERE asset_id=?", (with_reply,))["id"]
    db.run(
        """INSERT INTO video_comments (asset_id, gallery_id, parent_id, author_role, timecode, body)
           VALUES (?,?,?,'admin',0,'reply')""",
        (with_reply, gallery_id, root),
    )
    db.run("UPDATE video_comments SET deleted_at=datetime('now') WHERE asset_id=?", (hidden_only,))

    batch = gallery_comments.video_comment_threads(videos)

    assert list(batch) == videos  # every requested id present, in order
    assert batch[with_reply] == gallery_comments.video_comment_thread(with_reply)
    assert [c["body"] for c in batch[with_reply]] == [f"note on {with_reply}", "reply"]
    assert batch[hidden_only] == []  # soft-deleted stays invisible in both views
    assert len(batch[quiet]) == 1
    assert "asset_id" not in batch[quiet][0]  # same dict shape the client JSON ships
    assert gallery_comments.video_comment_threads([]) == {}


def _star(admin_client, monkeypatch, gallery_id: int, asset_id: int) -> tuple[int, str]:
    """One star toggle over HX; returns (queries issued, response body)."""
    with monkeypatch.context() as patched:
        opened = _connection_spy(patched)
        response = admin_client.post(
            f"/admin/galleries/{gallery_id}/assets/{asset_id}/portfolio",
            headers={"hx-request": "true"},
        )
    assert response.status_code == 200
    return len(opened), response.text


@pytest.mark.integration
def test_star_toggle_cost_does_not_scale_with_the_bench(admin_client, monkeypatch):
    small_id, _, small_photos, _ = _bench_gallery("perf-star-small", 2, 1)
    big_id, section_id, big_photos, _ = _bench_gallery("perf-star-big", 17, 3)

    small_queries, _ = _star(admin_client, monkeypatch, small_id, small_photos[0])
    big_queries, body = _star(admin_client, monkeypatch, big_id, big_photos[0])

    # A 20-asset/3-video bench costs exactly what a 3-asset one does: the write,
    # the gallery, the one asset, its sections and the two grouped count
    # queries, plus the two the admin session gate spends on every request.
    # Connection reuse means this counts new connect() calls, not statements.
    assert big_queries == small_queries
    assert big_queries <= 8, f"{big_queries} queries for one tile"

    target = big_photos[0]
    # primary swap: the tile keeps every hook the bench behaviors depend on
    assert f'id="asset-{target}"' in body
    assert 'class="gd-tile"' in body
    assert f'data-section="{section_id}"' in body
    assert 'data-fav="0"' in body and "data-filter-item" in body
    assert f"/admin/galleries/{big_id}/assets/{target}/portfolio" in body
    assert f"/admin/galleries/{big_id}/assets/{target}/cover" in body
    assert f"/admin/galleries/{big_id}/assets/{target}/delete" in body
    assert f"/admin/galleries/{big_id}/assets/{target}/section" in body
    assert "&#9733; portfolio" in body  # starred now, so the filled star
    # out-of-band siblings still swap, and still carry the true bench counts
    assert 'id="gd-pills"' in body and 'id="gd-stats"' in body
    assert body.count('hx-swap-oob="outerHTML"') == 2
    assert "All <span>20</span>" in body
    assert "Hero Dishes <span>20</span>" in body
    # The stat tile names the two kinds separately — the section pills count
    # every asset, the tile must not call the 3 films photos.
    assert '<span class="gd-stat-n">17</span>' in body
    assert "3 films" in body


@pytest.mark.integration
def test_video_tile_fragment_keeps_its_notes_and_social_cuts(admin_client):
    """The slim context still has to reach the video-only corners of the tile —
    an absent key would render empty instead of raising."""
    gallery_id, section_id, _, videos = _bench_gallery("perf-video-tile", 2, 2)
    target, other = videos
    db.run(
        """INSERT INTO asset_renditions (asset_id, preset, stored, status)
           VALUES (?,'9x16','cut_9x16.mp4','ready')""",
        (target,),
    )

    response = admin_client.post(
        f"/admin/galleries/{gallery_id}/assets/{target}/section",
        data={"section_id": section_id},
        headers={"hx-request": "true"},
    )

    assert response.status_code == 200
    body = response.text
    assert f'id="asset-{target}"' in body
    assert "Notes (1)" in body and f"note on {target}" in body
    assert "Social cuts" in body and "9:16" in body
    assert "rebuild" in body  # a rendition already exists, so not "build"
    assert f"note on {other}" not in body  # no other tile rode along


@pytest.mark.integration
def test_delete_fragment_reswaps_the_counts_without_rebuilding_the_bench(admin_client, monkeypatch):
    gallery_id, _, photos, _ = _bench_gallery("perf-delete", 6, 2)

    opened = _connection_spy(monkeypatch)
    response = admin_client.post(
        f"/admin/galleries/{gallery_id}/assets/{photos[0]}/delete",
        headers={"hx-request": "true"},
    )

    assert response.status_code == 200
    assert len(opened) <= 8, f"{len(opened)} queries for one delete"
    body = response.text
    assert body.count('hx-swap-oob="outerHTML"') == 2
    assert "All <span>7</span>" in body  # 5 photos + 2 videos left
    assert '<span class="gd-stat-n">5</span>' in body
    assert "2 films" in body
