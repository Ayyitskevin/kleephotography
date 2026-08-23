"""Missing-row 404s on admin routes that had no coverage for the absent case.

These routes all resolve a single row by id and 404 when it is gone. They are
pinned here because the happy paths are covered elsewhere but the absent-row
branch was not, so a lookup regression would surface as a 500 rather than a
failing test.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

MISSING = 999999


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


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "url", "data"),
    [
        ("get", f"/admin/thumb/{MISSING}/{MISSING}", None),
        ("post", f"/admin/galleries/{MISSING}/assets/{MISSING}/move", {"dir": "left"}),
        ("get", f"/admin/studio/clients/{MISSING}/brand/{MISSING}", None),
        ("post", f"/admin/studio/clients/{MISSING}/portal/publish", {}),
        ("post", f"/admin/studio/shots/{MISSING}/delete", {}),
        ("post", f"/admin/inbox/{MISSING}/notion-orphan/relink", {}),
        (
            "post",
            f"/admin/studio/email-templates/{MISSING}",
            {"name": "n", "subject": "s", "body": "b"},
        ),
        (
            "post",
            f"/admin/studio/testimonials/{MISSING}",
            {"quote": "q", "attribution_name": "a"},
        ),
    ],
)
def test_missing_row_returns_404(admin_client, method, url, data):
    response = getattr(admin_client, method)(
        url, **({"data": data} if data is not None else {}), follow_redirects=False
    )
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("action", ["toggle", "delete"])
def test_missing_task_keeps_its_detail_message(admin_client, action):
    """The task routes set a specific detail; a generic "Not found" is a regression."""
    response = admin_client.post(f"/admin/tasks/{MISSING}/{action}", follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["detail"] == "no such task"
