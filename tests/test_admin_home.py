"""Home dashboard rollups (app/admin/activity.home).

The dashboard is read-only, but it is the page Kevin acts from, so the numbers
have to be honest: the reply queue must surface the longest-waiting leads, and
every "collected" figure must agree with Financials/Reports.
"""

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"password": "test-pw"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client


def _open_inquiries() -> int:
    return db.one(
        "SELECT COUNT(*) AS n FROM inquiries WHERE converted_at IS NULL AND dismissed_at IS NULL"
    )["n"]


@pytest.mark.integration
def test_reply_queue_shows_the_oldest_leads_and_counts_all_of_them(admin_client):
    """Eight open inquiries: the six oldest fill the queue (the oldest first),
    and the headline reports all eight — not the length of the slice."""
    baseline = _open_inquiries()
    ids = []
    try:
        for i in range(8):
            ids.append(
                db.run(
                    """INSERT INTO inquiries (name, email, message, created_at)
                       VALUES (?,?,?, datetime('now', ?))""",
                    (
                        f"Queue Lead {i}",
                        f"queue-lead-{i}@example.com",
                        "waiting on a reply",
                        f"-{400 - i} days",
                    ),
                )
            )

        response = admin_client.get("/admin/home")
        assert response.status_code == 200
        ctx = response.context

        assert ctx["new_inquiries"] == baseline + 8
        queue = ctx["queue"]
        assert len(queue) == 6
        assert queue[0]["name"] == "Queue Lead 0"
        assert [q["name"] for q in queue] == [f"Queue Lead {i}" for i in range(6)]
        assert ctx["oldest_wait_days"] == queue[0]["age_days"] >= 399

        # the On Deck reply lane deals the same corrected rows
        replies = [c for c in ctx["on_deck"] if c["lane"] == "reply"]
        assert replies and replies[0]["title"] == "Reply to Queue Lead 0"

        page = response.text
        assert f"{baseline + 8} inquir" in page
        assert "Convert Queue Lead 0" in page
    finally:
        for inquiry_id in ids:
            db.run("DELETE FROM inquiries WHERE id=?", (inquiry_id,))
