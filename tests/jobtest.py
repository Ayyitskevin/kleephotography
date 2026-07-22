"""Shared job-pool freeze + drain helpers for inquiry / jobs tests.

The process-global jobs._pool is started by every TestClient lifespan. Nulling
_pool alone orphans still-running workers. Prefer freeze_job_pool: stop + block
lifespan restart + clear the handle, then drain with jobs._execute under test.
"""

from __future__ import annotations

from app import db, jobs


def freeze_job_pool(monkeypatch) -> None:
    """Stop process-wide workers; block lifespan from restarting them this test."""
    jobs.stop()
    monkeypatch.setattr(jobs, "start", lambda: None)
    monkeypatch.setattr(jobs, "_pool", None)


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
    jobs._execute(job_id)


def drain_owner_email(inquiry_id: int) -> None:
    row = owner_email_job(inquiry_id)
    assert row is not None
    drain_job(row["id"])
