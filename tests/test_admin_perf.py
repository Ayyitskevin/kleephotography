"""Cost contracts for the admin gallery surface.

These pin how a page is BUILT, not only what it renders: the library strips must
not stat-walk the media tree, and a per-tile HTMX action must not rebuild the
whole bench. A regression here shows up as latency on a large archive, which no
output assertion would catch — so the spies are the point.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import common
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
