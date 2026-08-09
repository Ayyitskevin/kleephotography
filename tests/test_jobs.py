import threading

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, ops_monitor
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
