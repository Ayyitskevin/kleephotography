"""The sales queue is advisory: never a promise of engagement or a send task."""

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin.studio_context import _sales_review
from app.main import app


def opportunity(pid, age, status="proposal_sent", value=0):
    return {
        "id": pid,
        "days_quiet": age,
        "status": status,
        "value_cents": value,
    }


@pytest.mark.unit
def test_queue_excludes_booked_recent_and_unknown_age_opportunities():
    rows = [
        opportunity(1, 40, "retainer_paid"),
        opportunity(2, 40, "project_closed"),
        opportunity(3, 40, "contract_signed"),
        opportunity(4, 6),
        opportunity(5, None),
        opportunity(6, 7),
    ]
    result = _sales_review(rows)
    assert [r["project"]["id"] for r in result["sales_review"]] == [6]
    assert result["sales_review_total"] == 1


@pytest.mark.unit
def test_oldest_first_value_breaks_ties_and_limit_preserves_total():
    rows = [opportunity(i, 8, value=100) for i in range(1, 8)]
    rows += [opportunity(8, 8, value=200), opportunity(9, 9, value=None)]
    result = _sales_review(reversed(rows))
    assert [r["project"]["id"] for r in result["sales_review"]] == [9, 8, 1, 2, 3]
    assert result["sales_review_total"] == 9


@pytest.mark.integration
def test_queue_is_private_read_only_and_updates_with_the_board():
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Synthetic sales review",))
    pid = db.run(
        """INSERT INTO projects (client_id, title, created_at, stage_changed_at)
           VALUES (?, ?, datetime('now','-20 days'), datetime('now','-20 days'))""",
        (cid, "<Sales review fixture>"),
    )
    # No lifespan: these route checks need SQLite, not background job/sender threads.
    client = TestClient(app)
    try:
        assert client.get("/admin/studio", follow_redirects=False).status_code == 303
        client.post("/admin/login", data={"password": config.ADMIN_PASSWORD})
        before = tuple(db.one("SELECT * FROM projects WHERE id=?", (pid,)))
        response = client.get("/admin/studio")
        assert response.status_code == 200
        queue = response.text.split('id="sales-review-title"', 1)[1].split("</section>", 1)[0]
        assert "&lt;Sales review fixture&gt;" in queue
        assert "No client activity recorded" in queue
        assert "since stage entry / creation" in queue
        assert "Value not recorded" in queue
        assert tuple(db.one("SELECT * FROM projects WHERE id=?", (pid,))) == before
        assert 'id="sales-review-title"' not in client.get("/").text
        response = client.post(
            f"/admin/studio/projects/{pid}/status",
            data={"status": "consultation_call"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        queue = response.text.split('id="sales-review-title"', 1)[1].split("</section>", 1)[0]
        # The existing fallback clock resets on the move; the refreshed queue
        # must remove the row rather than leaving a stale follow-up suggestion.
        assert "Sales review fixture" not in queue
    finally:
        client.close()
        db.run("DELETE FROM audit_log WHERE entity_type='project' AND entity_id=?", (pid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))
