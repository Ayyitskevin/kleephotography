import pytest

from app import config, db, jobs

pytestmark = pytest.mark.integration


def _make_asset(kind: str, status: str) -> tuple[int, int]:
    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        (f"job-failure-{kind}-{status}", "Job failure fixture", "1234"),
    )
    asset_id = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (
            gallery_id,
            kind,
            f"source.{'jpg' if kind == 'photo' else 'mp4'}",
            f"{kind}-source",
            status,
        ),
    )
    return gallery_id, asset_id


def _delete_fixture(gallery_id: int, job_id: int | None) -> None:
    if job_id is not None:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))
    db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))


def test_social_crop_exhaustion_preserves_ready_asset_and_retry_runs(monkeypatch, tmp_path):
    """An optional crop failure must not remove the delivered source photo."""
    monkeypatch.setattr(jobs, "_pool", None)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    gallery_id, asset_id = _make_asset("photo", "ready")
    source = config.MEDIA_DIR / str(gallery_id) / "original" / "photo-source"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source fixture")
    job_id = None

    def fail_crops(*args, **kwargs):
        raise RuntimeError("crop renderer failed")

    try:
        monkeypatch.setattr(jobs.imaging, "make_crops", fail_crops)
        job_id = jobs.enqueue("social_crops", {"asset_id": asset_id})

        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            jobs._execute(job_id)
            job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (job_id,))
            expected = "failed" if attempt == jobs.MAX_ATTEMPTS else "queued"
            assert job["status"] == expected
            assert job["attempts"] == attempt

        assert db.one("SELECT status FROM assets WHERE id=?", (asset_id,))["status"] == "ready"

        recovered = []

        def succeed_crops(*args, **kwargs):
            recovered.append(True)

        monkeypatch.setattr(jobs.imaging, "make_crops", succeed_crops)
        assert jobs.retry(job_id)
        jobs._execute(job_id)

        job = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "done", "attempts": 1, "error": None}
        assert recovered == [True]
        assert db.one("SELECT status FROM assets WHERE id=?", (asset_id,))["status"] == "ready"
    finally:
        _delete_fixture(gallery_id, job_id)


def test_video_rendition_exhaustion_preserves_ready_asset(monkeypatch):
    """A failed optional video rendition must not hide the delivered source."""
    monkeypatch.setattr(jobs, "_pool", None)
    gallery_id, asset_id = _make_asset("video", "ready")
    job_id = None

    def fail_renditions(payload):
        raise RuntimeError("rendition renderer failed")

    try:
        monkeypatch.setitem(jobs.HANDLERS, "video_renditions", fail_renditions)
        job_id = jobs.enqueue("video_renditions", {"asset_id": asset_id})
        for _ in range(jobs.MAX_ATTEMPTS):
            jobs._execute(job_id)

        job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "failed", "attempts": jobs.MAX_ATTEMPTS}
        assert db.one("SELECT status FROM assets WHERE id=?", (asset_id,))["status"] == "ready"
    finally:
        _delete_fixture(gallery_id, job_id)


@pytest.mark.parametrize(
    ("job_kind", "asset_kind"),
    (("image_derivatives", "photo"), ("video_transcode", "video")),
)
def test_primary_processing_exhaustion_marks_asset_failed(monkeypatch, job_kind, asset_kind):
    """Primary ingest failures still make unusable assets unavailable."""
    monkeypatch.setattr(jobs, "_pool", None)
    gallery_id, asset_id = _make_asset(asset_kind, "pending")
    sentinel_id = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (gallery_id, "photo", "sentinel.jpg", "sentinel-source", "ready"),
    )
    job_id = None

    def fail_primary(payload):
        raise RuntimeError("primary renderer failed")

    try:
        monkeypatch.setitem(jobs.HANDLERS, job_kind, fail_primary)
        job_id = jobs.enqueue(job_kind, {"asset_id": asset_id})
        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            jobs._execute(job_id)
            terminal = attempt == jobs.MAX_ATTEMPTS
            job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (job_id,))
            assert dict(job) == {
                "status": "failed" if terminal else "queued",
                "attempts": attempt,
            }
            expected_asset = "failed" if terminal else "pending"
            assert (
                db.one("SELECT status FROM assets WHERE id=?", (asset_id,))["status"]
                == expected_asset
            )
            assert (
                db.one("SELECT status FROM assets WHERE id=?", (sentinel_id,))["status"] == "ready"
            )
    finally:
        _delete_fixture(gallery_id, job_id)
