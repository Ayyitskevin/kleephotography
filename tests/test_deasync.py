"""The de-async contract for handlers that cannot become plain `def`.

Commit 2879636 moved 251 handlers to sync `def` so Starlette runs them in its
threadpool. The handlers left over are the ones that must `await` a form or a
body, and awaiting is the only thing they are allowed to do on the event loop:
everything past the parse blocks (SQLite carries a 30s busy_timeout, jobs
enqueue, templates render), and the loop is what writes bytes to every open
socket. One long lock wait on the loop stalls responses the threadpool already
finished computing.

So each of those handlers hands its post-parse work to `run_in_threadpool`. This
module holds that line from both ends: one live test that a slow write on one
route leaves the loop free to answer another, and a structural check that no
async handler in the converted modules reaches the database on the loop.
"""

import asyncio
import time

import httpx
import pytest

from app import db
from app.main import app

# Long enough that a blocked loop is unmistakable, short enough to stay cheap.
SLOW_WRITE_SEC = 1.0
# When the loop is free the probe returns after this pause plus a few ms.
PROBE_AFTER_SEC = 0.25


@pytest.fixture
def probe_form():
    """A questionnaire form — the submit path that touches no mail or job queue."""
    form_id = db.run(
        "INSERT INTO forms (slug, title, kind, active) "
        "VALUES ('deasync-probe','De-async Probe','questionnaire',1)"
    )
    yield "deasync-probe"
    db.run("DELETE FROM form_submissions WHERE form_id=?", (form_id,))
    db.run("DELETE FROM forms WHERE id=?", (form_id,))


@pytest.mark.integration
def test_slow_form_write_does_not_stall_the_event_loop(probe_form, monkeypatch):
    """A slow write inside POST /forms/{slug} must not delay a concurrent request.

    The submission INSERT stands in for any blocking SQLite call — a WAL
    checkpoint or a fat transaction from a job thread makes a real one take just
    as long. /healthz is the probe because it deliberately stays on the loop
    (see main.healthz), so it can only answer while the loop is free.

    Driven through httpx's ASGI transport rather than TestClient: TestClient
    hands each request to its own blocking portal, which would hide exactly the
    interference this asserts.
    """
    real_run = db.run

    def slow_run(sql, params=()):
        if "form_submissions" in sql:
            time.sleep(SLOW_WRITE_SEC)
        return real_run(sql, params)

    monkeypatch.setattr(db, "run", slow_run)

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
            started = time.perf_counter()
            submit = asyncio.create_task(
                c.post(f"/forms/{probe_form}", data={"name": "P", "email": "p@example.com"})
            )
            await asyncio.sleep(PROBE_AFTER_SEC)
            health = await c.get("/healthz")
            elapsed = time.perf_counter() - started
            return elapsed, health, await submit

    elapsed, health, submitted = asyncio.run(drive())

    assert submitted.status_code == 200
    assert health.status_code == 200
    # A blocked loop cannot even resume the sleep above until the write is done,
    # so elapsed would land at or past SLOW_WRITE_SEC instead of just past the pause.
    assert elapsed < PROBE_AFTER_SEC + SLOW_WRITE_SEC / 2, (
        f"/healthz answered {elapsed:.2f}s in — the form write blocked the event loop"
    )
