"""Payment is the gate into retainer_paid (migration 031). A hand move — the
board's drag/quick-move or the full project form — must not put an unpaid
project there, or the board's `booked_value` counts money that never landed.
"""

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.admin import studio_context
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303
        yield client


@pytest.fixture
def project_id():
    cid = db.run("INSERT INTO clients (name) VALUES ('Gate Client')")
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?, 'Gate Shoot', 'contract_signed')",
        (cid,),
    )
    yield pid
    db.run("DELETE FROM invoices WHERE project_id=?", (pid,))
    db.run("DELETE FROM audit_log WHERE entity_type='project' AND entity_id=?", (pid,))
    db.run("DELETE FROM projects WHERE id=?", (pid,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def _invoice(pid: int, status: str) -> None:
    db.run(
        "INSERT INTO invoices (project_id, slug, title, status, total_cents) VALUES (?,?,?,?,?)",
        (pid, f"gate-{pid}-{status}", "Retainer", status, 50000),
    )


def _status(pid: int) -> str:
    return db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"]


def _booked() -> int:
    return studio_context._ctx_pipeline(studio_context.dt.date.today().isoformat())["booked_value"]


def test_dragging_an_unpaid_project_into_retainer_paid_is_refused(admin_client, project_id):
    _invoice(project_id, "sent")
    booked_before = _booked()

    response = admin_client.post(
        f"/admin/studio/projects/{project_id}/status",
        data={"status": "retainer_paid"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert _status(project_id) == "contract_signed"
    assert _booked() == booked_before


def test_the_full_project_form_is_gated_too(admin_client, project_id):
    response = admin_client.post(
        f"/admin/studio/projects/{project_id}",
        data={"title": "Gate Shoot", "status": "retainer_paid"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert _status(project_id) == "contract_signed"


@pytest.mark.parametrize("paid_status", ["deposit_paid", "paid"])
def test_a_project_with_money_in_can_move_to_retainer_paid(admin_client, project_id, paid_status):
    _invoice(project_id, paid_status)

    response = admin_client.post(
        f"/admin/studio/projects/{project_id}/status",
        data={"status": "retainer_paid"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _status(project_id) == "retainer_paid"


def test_resaving_a_project_already_in_retainer_paid_is_not_a_move(admin_client, project_id):
    db.run("UPDATE projects SET status='retainer_paid' WHERE id=?", (project_id,))

    response = admin_client.post(
        f"/admin/studio/projects/{project_id}",
        data={"title": "Gate Shoot renamed", "status": "retainer_paid"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _status(project_id) == "retainer_paid"


def test_moving_out_of_retainer_paid_is_never_gated(admin_client, project_id):
    db.run("UPDATE projects SET status='retainer_paid' WHERE id=?", (project_id,))

    response = admin_client.post(
        f"/admin/studio/projects/{project_id}/status",
        data={"status": "session_planning"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _status(project_id) == "session_planning"
