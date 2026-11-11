"""Guided navigation is read-only; existing commands remain explicit and retry-safe."""

import datetime as dt
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import config, db, ratelimit
from app.admin import prospect_followups, studio
from app.main import app

pytestmark = pytest.mark.integration
ROOT = "/admin/studio/prospects"
DAY = "2037-04-15"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(studio, "_today", lambda: dt.date.fromisoformat(DAY))
    db.migrate()
    ratelimit._hits.clear()
    client = TestClient(app)
    assert client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield client
    client.close()


def prospect(label, due=DAY):
    pid = db.run(
        "INSERT INTO prospects (business,source,qualification,business_key,contact_key) "
        "VALUES (?, 'Synthetic research', 'potential_fit', ?, '')",
        (label, label.casefold()),
    )
    if due:
        db.run(
            "INSERT INTO tasks (title,due_date,prospect_id) VALUES ('Review request',?,?)",
            (due, pid),
        )
    return pid


def counts():
    return tuple(
        db.one(f"SELECT COUNT(*) n FROM {table}")["n"]
        for table in ("audit_log", "prospect_contact_events", "tasks", "jobs", "messages")
    )


def test_auth_read_only_order_and_navigation(admin):
    first = prospect("First", "2037-04-14")
    second = prospect("Second")
    before = counts()
    anonymous = TestClient(app)
    try:
        assert anonymous.get(f"{ROOT}/review-work", follow_redirects=False).status_code in (
            302,
            303,
        )
    finally:
        anonymous.close()
    page = admin.get(f"{ROOT}/review-work")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.context["prospect_id"] == first and page.context["total"] == 2
    page = admin.get(page.context["next_url"])
    assert page.context["prospect_id"] == second
    assert counts() == before


@pytest.mark.parametrize(
    "mode,until,blocked", [("stopped", None, 0), ("snoozed", DAY, 0), ("active", None, 1)]
)
def test_held_prospect_cannot_be_selected(admin, mode, until, blocked):
    pid = prospect("Held")
    db.run(
        "INSERT INTO prospect_outreach_preferences "
        "(prospect_id,mode,snoozed_until,email_blocked,sms_blocked,call_blocked,reason,version,last_command_digest) "
        "VALUES (?,?,?,?,?,?,'Fixture hold',1,'fixture')",
        (pid, mode, until, blocked, blocked, blocked),
    )
    page = admin.get(f"{ROOT}/review-work?prospect_id={pid}")
    assert page.context["total"] == 0 and not page.context.get("prospect")
    assert "data-contact-form" not in page.text


def test_live_context_after_completion_and_exact_retry(admin):
    pid = prospect("Cafe & Bar")
    task = db.one("SELECT * FROM tasks WHERE prospect_id=?", (pid,))
    url = f"{ROOT}/{pid}/follow-ups/{task['id']}?review=due&q=Cafe%20%26%20Bar"
    data = {"action": "complete", "version": str(task["version"]), "note": "Reviewed manually"}
    response = admin.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == f"{ROOT}/review-work"
    assert parse_qs(target.query) == {
        "review": ["due"],
        "q": ["Cafe & Bar"],
        "prospect_id": [str(pid)],
    }
    before = counts()
    assert admin.post(url, data=data, follow_redirects=False).status_code == 303
    assert counts() == before
    page = admin.get(response.headers["location"])
    assert page.context["total"] == 0 and not page.context.get("prospect")


def test_schedule_and_contact_keep_context_and_errors(admin):
    pid = prospect("Research", due=None)
    context = "?review=unplanned&q=Research"
    page = admin.get(f"{ROOT}/review-work" + context)
    assert "review=unplanned&amp;q=Research" in page.text
    data = {"operation_key": uuid.uuid4().hex, "title": "Next call", "due_date": "bad"}
    error = admin.post(f"{ROOT}/{pid}/follow-ups" + context, data=data)
    assert error.status_code == 400 and error.context["values"]["due_date"] == "bad"
    assert context.replace("&", "&amp;") in error.text
    saved = admin.post(
        f"{ROOT}/{pid}/follow-ups" + context, data=data | {"due_date": DAY}, follow_redirects=False
    )
    assert saved.status_code == 303 and "/review-work?" in saved.headers["location"]
    contact = {
        "operation_key": uuid.uuid4().hex,
        "channel": "call",
        "direction": "out",
        "outcome": "voicemail",
        "occurred_on": DAY,
        "task_action": "keep",
    }
    result = admin.post(
        f"{ROOT}/{pid}/contacts?review=due&q=Research", data=contact, follow_redirects=False
    )
    assert result.status_code == 303 and "/review-work?" in result.headers["location"]
    assert db.one("SELECT done FROM tasks WHERE prospect_id=?", (pid,))["done"] == 0


def test_search_escaping_unplanned_and_no_page_boundary(admin):
    for i in range(51):
        last = prospect(f"Bistro {i}", due=None)
    page = admin.get(f"{ROOT}/review-work?review=unplanned&prospect_id={last}")
    assert page.context["total"] == 51 and page.context["position"] == 51
    assert admin.get(page.context["next_url"]).context["position"] == 1
    page = admin.get(
        f"{ROOT}/review-work",
        params={"q": "<script>alert(1)</script>", "review": "https://evil.example"},
    )
    assert "<script>alert(1)</script>" not in page.text
    assert page.context["review"] == "due" and page.context["total"] == 0


def test_stale_task_error_and_invalid_return_context(admin):
    pid = prospect("Stale")
    task = db.one("SELECT * FROM tasks WHERE prospect_id=?", (pid,))
    db.run("UPDATE tasks SET version=version+1 WHERE id=?", (task["id"],))
    data = {"action": "complete", "version": str(task["version"]), "note": "Original entry"}
    error = admin.post(f"{ROOT}/{pid}/follow-ups/{task['id']}?review=due&q=Stale", data=data)
    assert error.status_code == 409 and error.context["values"]["version"] == str(task["version"])
    assert "Back to guided review" in error.text
    data["version"] = str(task["version"] + 1)
    result = admin.post(
        f"{ROOT}/{pid}/follow-ups/{task['id']}?review=//evil.example",
        data=data,
        follow_redirects=False,
    )
    assert result.headers["location"] == f"{ROOT}/{pid}"


def test_hold_arriving_between_queue_and_detail_hides_forms(admin, monkeypatch):
    prospect("Race")
    original = prospect_followups.detail_context

    def changed(pid):
        context = original(pid)
        context["outreach"]["held"] = True
        return context

    monkeypatch.setattr(prospect_followups, "detail_context", changed)
    page = admin.get(f"{ROOT}/review-work")
    assert not page.context.get("prospect") and "data-contact-form" not in page.text
