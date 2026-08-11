"""Shared job-pool freeze + drain helpers for inquiry / jobs tests.

The process-global jobs._pool is started by every TestClient lifespan. Nulling
_pool alone orphans still-running workers, and so does stopping without waiting.
Prefer freeze_job_pool: drain + block lifespan restart + clear the handle, then
run jobs._execute directly under the test's control.
"""

from __future__ import annotations

from app import db, jobs


def freeze_job_pool(monkeypatch) -> None:
    """Stop process-wide workers; block lifespan from restarting them this test."""
    # wait=True is what makes the docstring above true. shutdown(wait=False)
    # signals and returns, so a worker already inside a job keeps going, and it
    # re-reads config.DB_PATH at its next db.connect() — following the test that
    # swaps that path onto the new database. Drain, then null the handle.
    jobs.stop(wait=True)
    monkeypatch.setattr(jobs, "start", lambda: None)
    monkeypatch.setattr(jobs, "_pool", None)


def elapse_backoff(job_id: int) -> None:
    """Stand in for the sweeper thread waiting out a failed job's retry backoff.

    jobs._claim refuses a queued job until next_attempt_at passes, so a test that
    drives attempt after attempt back-to-back has to clear the wait each round.
    """
    db.run("UPDATE jobs SET next_attempt_at=NULL WHERE id=?", (job_id,))


def owner_email_job(inquiry_id: int):
    return db.one(
        "SELECT * FROM jobs WHERE kind='inquiry_owner_email' "
        "AND payload LIKE ? ORDER BY id DESC LIMIT 1",
        (f'%"inquiry_id": {inquiry_id}%',),
    )


def drain_job(job_id: int) -> None:
    row = db.one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    assert row is not None
    if row["status"] == "done":
        return
    if row["status"] != "queued":
        db.run(
            "UPDATE jobs SET status='queued', attempts=0, error=NULL WHERE id=?",
            (job_id,),
        )
    elapse_backoff(job_id)
    jobs._execute(job_id)


def drain_owner_email(inquiry_id: int) -> None:
    row = owner_email_job(inquiry_id)
    assert row is not None
    drain_job(row["id"])
