import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, ops_monitor, scheduler
from app.main import app
from tests.jobtest import elapse_backoff, freeze_job_pool

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client(client):
    r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
    assert r.status_code == 303
    return client


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
    freeze_job_pool(monkeypatch)
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
            elapse_backoff(job_id)
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
    freeze_job_pool(monkeypatch)
    gallery_id, asset_id = _make_asset("video", "ready")
    job_id = None

    def fail_renditions(payload):
        raise RuntimeError("rendition renderer failed")

    try:
        monkeypatch.setitem(jobs.HANDLERS, "video_renditions", fail_renditions)
        job_id = jobs.enqueue("video_renditions", {"asset_id": asset_id})
        for _ in range(jobs.MAX_ATTEMPTS):
            elapse_backoff(job_id)
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
    freeze_job_pool(monkeypatch)
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
            elapse_backoff(job_id)
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


def test_stale_zip_build_leaves_the_newer_archive_alone(monkeypatch, tmp_path):
    """A late rev-N job must not rebuild itself over rev N+1's archive.

    _h_zip prunes every g{id}-r*.zip but its own, so a job parked behind a
    backoff (or queued behind a transcode batch) that finally runs after another
    upload bumped content_rev would delete the archive clients are downloading.
    """
    freeze_job_pool(monkeypatch)
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "zips")
    config.ZIP_DIR.mkdir()
    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin, content_rev) VALUES (?,?,?,?)",
        ("stale-zip-rev", "Stale zip fixture", "1234", 2),
    )
    current = jobs.zip_path(gallery_id, 2)
    current.write_bytes(b"newer archive")
    job_id = None
    try:
        job_id = jobs.enqueue("zip_build", {"gallery_id": gallery_id, "rev": 1})
        jobs._execute(job_id)

        assert db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
        assert current.read_bytes() == b"newer archive"
        assert not jobs.zip_path(gallery_id, 1).exists()
    finally:
        _delete_fixture(gallery_id, job_id)


# ── subset ZIP: a parked job must not outlive its selection ────────────────


def _fav_zip_name(gallery_id: int, asset_ids: list[int]) -> str:
    """The name public/downloads.download_favorites mints for this fav set."""
    key = hashlib.sha256(",".join(str(i) for i in asset_ids).encode()).hexdigest()[:8]
    return f"g{gallery_id}-fav-{key}.zip"


def test_stale_favorites_zip_job_leaves_the_current_bundle_alone(monkeypatch, tmp_path):
    """A subset job parked behind a retry backoff can outlive the selection that
    named it. Building it then prunes the sibling matching what the client
    picked now — so they either download photos they dropped, or watch a ready
    download vanish mid-poll."""
    freeze_job_pool(monkeypatch)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "zips")
    config.ZIP_DIR.mkdir()
    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("zip-subset-stale", "Subset fixture", "1234"),
    )
    try:
        originals = config.MEDIA_DIR / str(gallery_id) / "original"
        originals.mkdir(parents=True)
        asset_ids = []
        for filename in ("one.jpg", "two.jpg"):
            (originals / filename).write_bytes(b"fixture bytes")
            asset_ids.append(
                db.run(
                    "INSERT INTO assets (gallery_id, kind, filename, stored, status) "
                    "VALUES (?,?,?,?,'ready')",
                    (gallery_id, "photo", filename, filename),
                )
            )
        visitor_id = db.run(
            "INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
            (gallery_id, "zip-subset-stale-t1"),
        )
        for asset_id in asset_ids:
            db.run(
                "INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
                (visitor_id, asset_id),
            )

        # On disk: the bundle for the client's live favorites. Queued earlier:
        # the bundle for the single favorite they had before adding two.jpg.
        current = config.ZIP_DIR / _fav_zip_name(gallery_id, asset_ids)
        current.write_bytes(b"current bundle")
        stale_name = _fav_zip_name(gallery_id, asset_ids[:1])

        jobs._h_zip_subset(
            {
                "gallery_id": gallery_id,
                "name": stale_name,
                "prune": f"g{gallery_id}-fav-*.zip",
                "asset_ids": asset_ids[:1],
            }
        )

        assert current.read_bytes() == b"current bundle"
        assert not (config.ZIP_DIR / stale_name).exists()
    finally:
        _delete_fixture(gallery_id, None)


# ── retry backoff + stranded-job sweep ─────────────────────────────────────


def _seconds_until_retry(job_id: int) -> int:
    row = db.one(
        "SELECT CAST((julianday(next_attempt_at) - julianday('now')) * 86400 AS INTEGER) AS s "
        "FROM jobs WHERE id=?",
        (job_id,),
    )
    return row["s"]


def _fail_handler(payload) -> None:
    raise RuntimeError("vendor 502")


def test_failed_attempt_waits_out_backoff_before_the_next_one(monkeypatch):
    """A transient vendor outage must not burn every attempt inside a second."""
    freeze_job_pool(monkeypatch)
    monkeypatch.setitem(jobs.HANDLERS, "zip_build", _fail_handler)
    job_id = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 0})
    try:
        jobs._execute(job_id)
        job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "queued", "attempts": 1}
        assert 0 < _seconds_until_retry(job_id) <= jobs.RETRY_BACKOFF_SECONDS[0]

        # Re-offering it inside the wait is refused, not run: attempts stay put.
        jobs._execute(job_id)
        assert db.one("SELECT attempts FROM jobs WHERE id=?", (job_id,))["attempts"] == 1

        elapse_backoff(job_id)
        jobs._execute(job_id)
        job = db.one("SELECT status, attempts FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "queued", "attempts": 2}
        # Backoff widens, and stays bounded by the last configured step.
        assert (
            jobs.RETRY_BACKOFF_SECONDS[0]
            < _seconds_until_retry(job_id)
            <= jobs.RETRY_BACKOFF_SECONDS[-1]
        )

        elapse_backoff(job_id)
        jobs._execute(job_id)
        job = db.one("SELECT status, attempts, next_attempt_at FROM jobs WHERE id=?", (job_id,))
        assert dict(job) == {"status": "failed", "attempts": 3, "next_attempt_at": None}
    finally:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))


class _RecordingPool:
    """Stand-in for the live pool: records what dispatch offers, executes nothing."""

    def __init__(self) -> None:
        self.offered: list[int] = []

    def submit(self, fn, job_id: int) -> None:
        self.offered.append(job_id)


def test_corrupt_payload_fails_the_job_instead_of_wedging_it(monkeypatch):
    """A payload json can't parse must take the normal failure path.

    Parsed outside the try, it raised past the recorder: the row stayed
    'running' forever — queue_health summed only failed/queued, sweep re-offered
    only queued, and the pool future's exception was never observed. Nothing
    short of a restart moved it, and nothing anywhere said so.
    """
    freeze_job_pool(monkeypatch)
    job_id = db.run("INSERT INTO jobs (kind, payload) VALUES (?,?)", ("zip_build", "{not json"))
    try:
        for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
            elapse_backoff(job_id)
            jobs._execute(job_id)
            job = db.one("SELECT status, attempts, error FROM jobs WHERE id=?", (job_id,))
            assert job["status"] == ("failed" if attempt == jobs.MAX_ATTEMPTS else "queued")
            assert job["attempts"] == attempt
            assert job["error"]
    finally:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))


def test_a_job_wedged_in_running_is_visible_to_healthz_and_the_heartbeat(client, monkeypatch):
    """'running' is the one state no sweep re-offers, so it has to be reported."""
    freeze_job_pool(monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(ops_monitor.alerts, "is_enabled", lambda: True)
    monkeypatch.setattr(ops_monitor.alerts, "ops_alert", lambda sig, msg: sent.append((sig, msg)))

    before = client.get("/healthz").json()
    working = db.run(
        "INSERT INTO jobs (kind, payload, status, updated_at) "
        "VALUES (?,?, 'running', datetime('now'))",
        ("video_transcode", "{}"),
    )
    wedged = db.run(
        "INSERT INTO jobs (kind, payload, status, updated_at) "
        "VALUES (?,?, 'running', datetime('now'))",
        ("zip_build", "{}"),
    )
    try:
        ops_monitor.sweep()
        assert not [s for s, _ in sent if s == "jobs_running_stale"]  # a worker mid-job is fine

        db.run(
            "UPDATE jobs SET updated_at=datetime('now', ?) WHERE id=?",
            (f"-{config.JOB_STUCK_AFTER_SECONDS + 60} seconds", wedged),
        )
        after = client.get("/healthz").json()
        assert after["jobs_running_stale"] == before["jobs_running_stale"] + 1  # not `working`
        assert after["ok"] is True  # visibility, not a 503 — the site still serves

        sent.clear()
        ops_monitor.sweep()
        alerted = [m for s, m in sent if s == "jobs_running_stale"]
        assert len(alerted) == 1
        assert "running for over" in alerted[0]
    finally:
        db.run("DELETE FROM jobs WHERE id IN (?,?)", (working, wedged))


def test_sweep_redispatches_stranded_jobs_and_honors_backoff(monkeypatch):
    """The row a dead pool dropped, and the row a failure parked, both get re-offered."""
    freeze_job_pool(monkeypatch)
    # enqueue with no pool = dispatch drops the offer; the durable row stays queued
    stranded = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 0})
    waiting = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 1})
    try:
        db.run(
            "UPDATE jobs SET next_attempt_at=datetime('now', '+300 seconds') WHERE id=?",
            (waiting,),
        )
        pool = _RecordingPool()
        monkeypatch.setattr(jobs, "_pool", pool)

        jobs.sweep()
        assert stranded in pool.offered
        assert waiting not in pool.offered

        # Once the wait elapses the same sweep picks the retry up.
        elapse_backoff(waiting)
        pool.offered.clear()
        jobs.sweep()
        assert waiting in pool.offered
    finally:
        db.run("DELETE FROM jobs WHERE id IN (?,?)", (stranded, waiting))


def test_sweep_does_not_re_offer_a_job_the_pool_already_holds(monkeypatch):
    """Every tick used to re-submit the WHOLE due backlog.

    200 transcodes behind 2 workers meant ~200 duplicate futures a minute, each
    running _claim — a write transaction that no-ops — against the same lock the
    request path needs, plus an executor deque that only grew.
    """
    freeze_job_pool(monkeypatch)
    queued = [jobs.enqueue("zip_build", {"gallery_id": 0, "rev": n}) for n in range(3)]
    try:
        pool = _RecordingPool()
        monkeypatch.setattr(jobs, "_pool", pool)

        jobs.sweep()
        assert [j for j in pool.offered if j in queued] == queued
        jobs.sweep()
        assert [j for j in pool.offered if j in queued] == queued  # N, not 2N

        # The set is released by the worker, not left to expire: once a job has
        # actually run, a later sweep is free to offer it again.
        monkeypatch.setitem(jobs.HANDLERS, "zip_build", lambda p: None)
        jobs._execute(queued[0])
        assert queued[0] not in jobs._inflight
    finally:
        db.run("DELETE FROM jobs WHERE id IN (?,?,?)", tuple(queued))
        with jobs._inflight_lock:
            jobs._inflight.clear()


def test_queue_runs_its_own_clock_fine_enough_to_honor_the_backoff(monkeypatch):
    """The sweeper is a real thread on the queue's OWN tick, not the hourly one.

    Riding scheduler.RECURRING_TICK_SECONDS (3600) would round every backoff up
    to "within the hour" and make RETRY_BACKOFF_SECONDS decorative, so the tick
    has to be finer than the first backoff step — assert that, not just that a
    sweep happens.
    """
    assert config.JOB_SWEEP_TICK_SECONDS <= jobs.RETRY_BACKOFF_SECONDS[0]
    assert config.JOB_SWEEP_TICK_SECONDS < config.RECURRING_TICK_SECONDS

    swept = threading.Event()
    monkeypatch.setattr(config, "JOB_SWEEP_TICK_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "sweep", swept.set)
    jobs._stop_sweeper()  # a lifespan from an earlier test may still own one
    jobs._start_sweeper()
    try:
        assert swept.wait(2), "sweeper thread never ticked"
    finally:
        jobs._stop_sweeper()
    assert jobs._sweeper is None


def test_admin_can_force_a_job_parked_behind_its_backoff(admin_client, monkeypatch):
    """Kevin's only hands-on control over a stuck queue must work DURING a backoff.

    The backoff parks a failed job as queued-with-a-future-next_attempt_at. If
    retry() still only matched status='failed', the Jobs button would 404 for
    exactly the window in which someone is most likely to press it.
    """
    freeze_job_pool(monkeypatch)
    monkeypatch.setitem(jobs.HANDLERS, "zip_build", _fail_handler)
    job_id = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 0})
    try:
        jobs._execute(job_id)
        parked = db.one("SELECT status, attempts, next_attempt_at FROM jobs WHERE id=?", (job_id,))
        assert parked["status"] == "queued" and parked["next_attempt_at"] is not None
        assert jobs._claim(job_id) is None  # nothing else can move it

        # The page offers the affordance for a parked row, not just a failed one.
        page = admin_client.get("/admin/jobs")
        assert page.status_code == 200
        assert f"/admin/jobs/{job_id}/retry" in page.text
        assert "Waiting to retry" in page.text

        r = admin_client.post(f"/admin/jobs/{job_id}/retry", follow_redirects=False)
        assert r.status_code == 303
        row = db.one(
            "SELECT status, attempts, error, next_attempt_at FROM jobs WHERE id=?", (job_id,)
        )
        assert dict(row) == {
            "status": "queued",
            "attempts": 0,
            "error": None,
            "next_attempt_at": None,
        }

        # Unparked for real: the claim gate now lets it run instead of skipping it.
        monkeypatch.setitem(jobs.HANDLERS, "zip_build", lambda p: None)
        jobs._execute(job_id)
        assert db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "done"
        # And an already-running/queued-clean job is still not retryable.
        assert jobs.retry(job_id) is False
    finally:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))


def test_parked_and_wedged_jobs_are_visible_to_healthz(client, monkeypatch):
    """The limbo the backoff created must be reportable, not silent (R12)."""
    freeze_job_pool(monkeypatch)
    before = client.get("/healthz").json()
    parked = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 0})
    wedged = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 1})
    # A never-attempted job, however old, is queue DEPTH, not breakage: two
    # workers grinding a video batch leave fresh work queued for an hour.
    backlog = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 2})
    try:
        db.run(
            "UPDATE jobs SET next_attempt_at=datetime('now', '+300 seconds') WHERE id=?", (parked,)
        )
        db.run(
            "UPDATE jobs SET next_attempt_at=datetime('now', ?) WHERE id=?",
            (f"-{config.JOB_STUCK_AFTER_SECONDS + 60} seconds", wedged),
        )
        db.run("UPDATE jobs SET created_at=datetime('now', '-1 day') WHERE id=?", (backlog,))

        after = client.get("/healthz").json()
        assert after["jobs_waiting_retry"] == before["jobs_waiting_retry"] + 2
        assert after["jobs_stuck"] == before["jobs_stuck"] + 1  # only the wedged retry
        assert after["jobs_pending"] == before["jobs_pending"] + 3
        assert after["ok"] is True  # visibility, not a 503 — the site still serves
    finally:
        db.run("DELETE FROM jobs WHERE id IN (?,?,?)", (parked, wedged, backlog))


def test_healthz_answers_fast_when_the_database_wedges(client, monkeypatch):
    """The wedged-DB case is the one /healthz exists for — it must not join it.

    db.one carries a 30s busy_timeout. Awaited inline on the event loop, a
    wedged database took /healthz down with it AND froze every other in-flight
    response for the same 30s, turning a degraded app into a dead one.
    """
    release = threading.Event()

    def wedged(*args, **kwargs):
        release.wait(30)
        return {"ok": 1, "n": 0, "failed": 0, "waiting_retry": 0, "stuck": 0, "running_stale": 0}

    monkeypatch.setattr(db, "one", wedged)
    try:
        started = time.monotonic()
        r = client.get("/healthz")
        elapsed = time.monotonic() - started

        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        assert body["db_probe"] == "timeout"
        assert body["db_connected"] is False
        # Storage facts still answered — only the DB half was abandoned.
        assert body["disk_free_gb"] is not None
        assert elapsed < 5, f"/healthz waited {elapsed:.1f}s on the wedged database"
    finally:
        release.set()


def test_ops_heartbeat_alerts_on_a_wedged_queue(monkeypatch):
    """A queue that stopped draining pushes an alert; before, failed=0 read as fine."""
    freeze_job_pool(monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(ops_monitor.alerts, "is_enabled", lambda: True)
    monkeypatch.setattr(ops_monitor.alerts, "ops_alert", lambda sig, msg: sent.append((sig, msg)))

    job_id = jobs.enqueue("zip_build", {"gallery_id": 0, "rev": 0})
    try:
        db.run("UPDATE jobs SET created_at=datetime('now', '-1 day') WHERE id=?", (job_id,))
        ops_monitor.sweep()
        assert not [s for s, _ in sent if s == "jobs_stuck"]  # deep backlog != wedged

        db.run(
            "UPDATE jobs SET next_attempt_at=datetime('now', ?) WHERE id=?",
            (f"-{config.JOB_STUCK_AFTER_SECONDS + 60} seconds", job_id),
        )
        sent.clear()
        ops_monitor.sweep()
        stuck = [m for s, m in sent if s == "jobs_stuck"]
        assert len(stuck) == 1
        assert "past its retry time" in stuck[0]
    finally:
        db.run("DELETE FROM jobs WHERE id=?", (job_id,))


# ── retention: the jobs table used to grow forever ─────────────────────────


def _aged_job(status: str, age_days: int, *, stamped: bool = True) -> int:
    """A job row of `status` last touched `age_days` ago. stamped=False leaves
    updated_at NULL, the shape of a row no worker ever claimed."""
    job_id = db.run(
        "INSERT INTO jobs (kind, payload, status) VALUES ('zip_build','{}',?)", (status,)
    )
    age = f"-{age_days} days"
    db.run("UPDATE jobs SET created_at=datetime('now', ?) WHERE id=?", (age, job_id))
    if stamped:
        db.run("UPDATE jobs SET updated_at=datetime('now', ?) WHERE id=?", (age, job_id))
    return job_id


def test_retention_prunes_old_done_jobs_and_only_those(monkeypatch):
    """Done rows are dead weight under queue_health's table-wide aggregates, but
    every other status is either live work or a human's evidence: a failed job
    is what tells Kevin something needs him, and age says nothing about a
    running worker or a retry parked behind its backoff."""
    freeze_job_pool(monkeypatch)
    old_done = _aged_job("done", jobs.DONE_JOB_RETENTION_DAYS + 1)
    unclaimed_done = _aged_job("done", jobs.DONE_JOB_RETENTION_DAYS + 1, stamped=False)
    recent_done = _aged_job("done", jobs.DONE_JOB_RETENTION_DAYS - 1)
    old_failed = _aged_job("failed", 400)
    old_queued = _aged_job("queued", 400)
    old_running = _aged_job("running", 400)
    seeded = (old_done, unclaimed_done, recent_done, old_failed, old_queued, old_running)
    try:
        assert jobs.prune_done() >= 2
        ph = ",".join("?" * len(seeded))
        alive = {r["id"] for r in db.all_(f"SELECT id FROM jobs WHERE id IN ({ph})", seeded)}
        assert alive == {recent_done, old_failed, old_queued, old_running}
    finally:
        db.run(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(seeded))})", seeded)


def test_hourly_scheduler_runs_the_retention_sweep(monkeypatch):
    """It rides the existing hourly loop — no cron, no second process — and a
    sweep that never runs is the same unbounded table we started with."""
    for module, name in (
        (scheduler.recurring, "run_due_plans"),
        (scheduler.booking_reminders, "sweep"),
        (scheduler.gallery_reminders, "sweep"),
        (scheduler.contract_reminders, "sweep"),
        (scheduler.ops_monitor, "sweep"),
    ):
        monkeypatch.setattr(module, name, lambda: None)
    pruned = []

    def _prune() -> int:
        pruned.append(True)
        scheduler._stop.set()  # one pass is the whole assertion
        return 0

    monkeypatch.setattr(scheduler.jobs, "prune_done", _prune)
    monkeypatch.setattr(config, "RECURRING_TICK_SECONDS", 0.01)
    scheduler._stop.clear()
    try:
        scheduler._loop()
    finally:
        scheduler._stop.clear()
    assert pruned == [True]
