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
    "review,due", [("due", DAY), ("upcoming", "2037-04-16"), ("unplanned", None)]
)
def test_navigation_wraps_both_ways_and_preserves_search(admin, review, due):
    search = "Café & Bar / menu"
    ids = [prospect(f"{search} {letter}", due) for letter in "ABC"]
    prospect("Outside this search", due)
    before = counts()
    tasks = [dict(row) for row in db.all_("SELECT * FROM tasks ORDER BY id")]
    page = admin.get(f"{ROOT}/review-work", params={"review": review, "q": search})
    assert page.context["total"] == 3 and page.context["prospect_id"] == ids[0]
    for direction, selected, position in [
        ("previous_url", ids[2], 3),
        ("previous_url", ids[1], 2),
        ("next_url", ids[2], 3),
        ("next_url", ids[0], 1),
    ]:
        target = urlsplit(page.context[direction])
        assert target.path == f"{ROOT}/review-work"
        assert parse_qs(target.query) == {
            "review": [review],
            "q": [search],
            "prospect_id": [str(selected)],
        }
        page = admin.get(page.context[direction])
        assert page.context["prospect_id"] == selected and page.context["position"] == position
        assert "Previous prospect</a>" in page.text and "Next prospect</a>" in page.text
    assert counts() == before
    assert [dict(row) for row in db.all_("SELECT * FROM tasks ORDER BY id")] == tasks


@pytest.mark.parametrize(
    "review,due", [("due", DAY), ("upcoming", "2037-04-16"), ("unplanned", None)]
)
def test_single_and_empty_queues_do_not_offer_self_navigation(admin, review, due):
    page = admin.get(f"{ROOT}/review-work", params={"review": review})
    assert page.context["total"] == 0 and page.context["position"] is None
    assert page.context["previous_url"] is None and page.context["next_url"] is None
    assert "No eligible prospects in this queue" in page.text
    pid = prospect("Only match", due)
    before = counts()
    page = admin.get(f"{ROOT}/review-work", params={"review": review})
    assert page.context["prospect_id"] == pid and page.context["position"] == 1
    assert page.context["total"] == 1
    assert page.context["previous_url"] is None and page.context["next_url"] is None
    assert "Previous prospect</a>" not in page.text and "Next prospect</a>" not in page.text
    assert "Position 1" in page.text and counts() == before


def test_removed_selection_requires_explicit_navigation_without_previous(admin):
    removed = prospect("Complete me")
    remaining = prospect("Still due")
    task = db.one("SELECT * FROM tasks WHERE prospect_id=?", (removed,))
    response = admin.post(
        f"{ROOT}/{removed}/follow-ups/{task['id']}?review=due",
        data={"action": "complete", "version": task["version"], "note": "Reviewed manually"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    before = counts()
    page = admin.get(response.headers["location"])
    assert page.context["prospect_id"] == removed and not page.context.get("prospect")
    assert page.context["position"] is None and page.context["previous_url"] is None
    assert page.context["total"] == 1 and "This prospect is no longer in this queue" in page.text
    assert "Review next eligible prospect</a>" in page.text and "data-contact-form" not in page.text
    page = admin.get(page.context["next_url"])
    assert page.context["prospect_id"] == remaining
    assert page.context["previous_url"] is None and page.context["next_url"] is None
    assert counts() == before


def test_navigation_uses_due_order_and_skips_holds_but_keeps_partial_preferences(admin):
    last = prospect("Due today")
    first = prospect("Oldest due", "2037-04-12")
    held = prospect("Held oldest", "2037-04-11")
    middle = prospect("Partially restricted", "2037-04-13")
    for pid, mode in [(held, "stopped"), (middle, "active")]:
        db.run(
            "INSERT INTO prospect_outreach_preferences "
            "(prospect_id,mode,email_blocked,reason,version,last_command_digest) "
            "VALUES (?,?,1,'Fixture restriction',1,'fixture')",
            (pid, mode),
        )
    before = counts()
    page = admin.get(f"{ROOT}/review-work")
    assert page.context["prospect_id"] == first and page.context["total"] == 3
    assert admin.get(page.context["previous_url"]).context["prospect_id"] == last
    page = admin.get(page.context["next_url"])
    assert page.context["prospect_id"] == middle and "Excluded channels: email" in page.text
    assert admin.get(page.context["previous_url"]).context["prospect_id"] == first
    page = admin.get(page.context["next_url"])
    assert page.context["prospect_id"] == last
    assert admin.get(page.context["next_url"]).context["prospect_id"] == first
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
    assert admin.get(page.context["previous_url"]).context["position"] == 50
    first = admin.get(page.context["next_url"])
    assert first.context["position"] == 1
    assert admin.get(first.context["previous_url"]).context["prospect_id"] == last
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


@pytest.mark.parametrize("peers", [False, True])
def test_hold_arriving_between_queue_and_detail_hides_forms(admin, monkeypatch, peers):
    first = prospect("Still eligible first") if peers else None
    held = prospect("Race")
    if peers:
        prospect("Still eligible last")
    original = prospect_followups.detail_context

    def changed(pid):
        context = original(pid)
        if pid == held:
            context["outreach"]["held"] = True
        return context

    monkeypatch.setattr(prospect_followups, "detail_context", changed)
    before = counts()
    page = admin.get(f"{ROOT}/review-work?prospect_id={held}")
    assert not page.context.get("prospect") and "data-contact-form" not in page.text
    assert page.context["previous_url"] is None and page.context["position"] is None
    assert page.context["total"] == (2 if peers else 0)
    if peers:
        assert admin.get(page.context["next_url"]).context["prospect_id"] == first
    else:
        assert page.context["next_url"] is None
        assert "No eligible prospects in this queue" in page.text
    assert counts() == before


def test_research_detour_keeps_search_errors_and_changed_eligibility(admin):
    from html import unescape

    pid = prospect("Cafe & Bar", due=None)
    context = f"?review=unplanned&q=Cafe+%26+Bar&review_origin={pid}"
    page = admin.get(f"{ROOT}/review-work?review=unplanned&q=Cafe+%26+Bar")
    assert f'href="{ROOT}/{pid}{context}"' in unescape(page.text)
    before = counts()
    edit = admin.get(f"{ROOT}/{pid}{context}")
    assert f'action="{ROOT}/{pid}{context}"' in unescape(edit.text)
    assert "Back to guided review" in edit.text and counts() == before
    data = dict(edit.context["values"])
    invalid = admin.post(f"{ROOT}/{pid}{context}", data=data | {"source": ""})
    assert invalid.status_code == 400 and invalid.context["values"]["source"] == ""
    assert invalid.context["review_query"] == context
    saved = admin.post(
        f"{ROOT}/{pid}{context}", data=data | {"qualification": "not_fit"}, follow_redirects=False
    )
    assert saved.status_code == 303
    target = urlsplit(saved.headers["location"])
    assert target.path == f"{ROOT}/review-work"
    assert parse_qs(target.query)["q"] == ["Cafe & Bar"]
    assert parse_qs(target.query)["prospect_id"] == [str(pid)]
    assert not admin.get(saved.headers["location"]).context.get("prospect")
    assert (
        db.one("SELECT qualification FROM prospects WHERE id=?", (pid,))["qualification"]
        == "not_fit"
    )


def test_duplicate_research_error_preserves_detour(admin):
    pid = prospect("Original")
    other = prospect("Existing")
    context = f"?review=due&q=Original&review_origin={pid}"
    data = dict(admin.get(f"{ROOT}/{pid}{context}").context["values"])
    error = admin.post(f"{ROOT}/{pid}{context}", data=data | {"business": "Existing"})
    assert error.status_code == 409 and error.context["review_query"] == context
    assert error.context["values"]["business"] == "Existing"
    assert db.one("SELECT business FROM prospects WHERE id=?", (pid,))["business"] == "Original"
    assert any(match["href"] == f"{ROOT}/{other}" for match in error.context["matches"])


def test_preference_detour_hold_errors_and_retry_return_to_review(admin):
    from html import unescape

    pid = prospect("Preferences")
    context = f"?review=due&q=Preferences&review_origin={pid}"
    before = counts()
    page = admin.get(f"{ROOT}/{pid}/outreach{context}")
    assert counts() == before
    assert f'action="{ROOT}/{pid}/outreach{context}"' in unescape(page.text)
    data = {"token": page.context["token"], "mode": "stopped", "reason": ""}
    error = admin.post(f"{ROOT}/{pid}/outreach{context}", data=data)
    assert error.status_code == 400 and error.context["token"] == data["token"]
    assert error.context["review_query"] == context
    data["reason"] = "Synthetic request to stop"
    saved = admin.post(f"{ROOT}/{pid}/outreach{context}", data=data, follow_redirects=False)
    assert saved.status_code == 303
    assert urlsplit(saved.headers["location"]).path == f"{ROOT}/review-work"
    before = counts()
    retry = admin.post(f"{ROOT}/{pid}/outreach{context}", data=data, follow_redirects=False)
    assert retry.headers["location"] == saved.headers["location"] and counts() == before
    returned = admin.get(saved.headers["location"])
    assert not returned.context.get("prospect") and "data-contact-form" not in returned.text
    assert db.one("SELECT done FROM tasks WHERE prospect_id=?", (pid,))["done"] == 0


def test_source_preference_detour_returns_to_original_and_preserves_stale_token(admin):
    from html import unescape

    origin = prospect("Review origin")
    source = prospect("Other source")
    context = f"?review=due&q=Review&review_origin={origin}"
    # Shared summary links carry the origin even when a linked source is edited.
    from app.render import templates

    state = {
        "label": "Synthetic linked scope",
        "members": [{"id": source, "business": "Other source"}],
        "sources": [{"prospect_id": source, "mode": "active", "reason": "Fixture"}],
    }
    markup = templates.get_template("admin/_outreach_preferences.html").module.summary(
        state, origin, context
    )
    assert f'href="{ROOT}/{source}/outreach{context}"' in unescape(markup)
    url = f"{ROOT}/{source}/outreach{context}"
    page = admin.get(url)
    data = {
        "token": page.context["token"],
        "mode": "active",
        "reason": "Synthetic preference",
        "email_blocked": "1",
    }
    saved = admin.post(url, data=data, follow_redirects=False)
    assert parse_qs(urlsplit(saved.headers["location"]).query)["prospect_id"] == [str(origin)]
    assert admin.get(saved.headers["location"]).context["prospect_id"] == origin
    assert (
        db.one(
            "SELECT email_blocked FROM prospect_outreach_preferences WHERE prospect_id=?", (source,)
        )["email_blocked"]
        == 1
    )
    assert not db.one("SELECT * FROM prospect_outreach_preferences WHERE prospect_id=?", (origin,))
    stale = admin.post(url, data=data | {"reason": "A different command"})
    assert stale.status_code == 409 and stale.context["token"] == data["token"]
    assert stale.context["review_query"] == context
    assert f'href="{ROOT}/{source}/outreach{context}"' in unescape(stale.text)


@pytest.mark.parametrize("origin", ["//evil.example", "-1", "0", "1/../2", "9" * 19])
def test_invalid_detour_origin_falls_back_to_standalone(admin, origin):
    from urllib.parse import urlencode

    pid = prospect("Local only")
    context = "?" + urlencode(
        {
            "review": "due",
            "q": "Local",
            "review_origin": origin,
            "return_url": "https://evil.example",
        }
    )
    page = admin.get(f"{ROOT}/{pid}{context}")
    assert "review_query" not in page.context
    data = dict(page.context["values"])
    saved = admin.post(f"{ROOT}/{pid}{context}", data=data, follow_redirects=False)
    assert saved.headers["location"] == f"{ROOT}/{pid}"
    preferences = admin.get(f"{ROOT}/{pid}/outreach{context}")
    result = admin.post(
        f"{ROOT}/{pid}/outreach{context}",
        data={
            "token": preferences.context["token"],
            "mode": "active",
            "reason": "Synthetic review",
        },
        follow_redirects=False,
    )
    assert result.headers["location"] == f"{ROOT}/{pid}/outreach"
