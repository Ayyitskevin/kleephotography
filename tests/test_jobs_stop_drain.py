"""jobs.stop(wait=True) — drain, don't just signal.

`shutdown(wait=False)` returns while a worker is still inside a job. That is
correct for app shutdown (a restart must not block behind a long transcode, and
start() re-queues anything left in 'running'), and wrong for a caller that is
about to move the ground under the workers.

Tests do exactly that: they repoint `config.DB_PATH` at a fresh database between
cases. A worker that outlived stop() re-reads that module global at its next
db.connect(), so it stops talking to the database it was started against and
starts talking to the NEXT test's — mid-migrate. That is the intermittent
"database is locked" in test_smoke_argus.
"""

import threading
import time

import pytest

from app import jobs

pytestmark = pytest.mark.integration


@pytest.fixture
def slow_job():
    running = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def handler(payload):
        running.set()
        release.wait(10)
        finished.set()

    jobs.HANDLERS["_test_slow"] = handler
    try:
        yield running, release, finished
    finally:
        jobs.HANDLERS.pop("_test_slow", None)
        release.set()


def test_stop_without_wait_leaves_the_worker_running(slow_job):
    """Documents the default, and why it is not safe for a path swap."""
    running, release, finished = slow_job
    jobs.start()
    jobs.enqueue("_test_slow", {})
    assert running.wait(5), "job never started"

    jobs.stop()
    assert not finished.is_set(), "job finished too fast to prove anything"
    release.set()
    assert finished.wait(5)


def test_stop_with_wait_drains_before_returning(slow_job):
    """The fix: no worker is still inside a job when stop() returns."""
    running, release, finished = slow_job
    jobs.start()
    jobs.enqueue("_test_slow", {})
    assert running.wait(5), "job never started"

    def release_soon():
        time.sleep(0.3)
        release.set()

    threading.Thread(target=release_soon, daemon=True).start()
    t0 = time.monotonic()
    jobs.stop(wait=True)
    elapsed = time.monotonic() - t0

    assert finished.is_set(), "stop(wait=True) returned with a job still running"
    assert elapsed >= 0.25, f"returned in {elapsed:.2f}s — it did not actually wait"


def test_wait_is_bounded_so_a_wedged_job_cannot_hang_the_suite(slow_job):
    """A stuck job should cost a warning and a late return, never a hung run."""
    running, release, finished = slow_job
    jobs.start()
    jobs.enqueue("_test_slow", {})
    assert running.wait(5), "job never started"

    t0 = time.monotonic()
    jobs.stop(wait=True, timeout=0.5)
    elapsed = time.monotonic() - t0

    assert elapsed < 3, f"stop() blocked {elapsed:.2f}s past its timeout"
    assert not finished.is_set()
    release.set()
    assert finished.wait(5)


def test_stop_clears_pool_and_tracking(slow_job):
    running, release, finished = slow_job
    jobs.start()
    jobs.enqueue("_test_slow", {})
    assert running.wait(5)
    release.set()
    jobs.stop(wait=True)
    assert jobs._pool is None
    assert not jobs._inflight
    assert not jobs._futures
