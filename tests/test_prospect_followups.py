"""Manual prospect work against disposable SQLite, with no lifespan or senders.

These tests exercise the boundaries that would otherwise manufacture progress:
replayed writes, stale task forms, mismatched prospects and outgoing attempts
mistaken for replies. Existing project/general Tasks remain independently usable.
"""

import datetime as dt
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import audit, config, db, ratelimit
from app.admin import home_context, studio
from app.main import app

pytestmark = pytest.mark.integration
ROOT = "/admin/studio/prospects"
TODAY = dt.date(2037, 4, 15)


@pytest.fixture
def admin(tmp_path, monkeypatch):
    """Each case owns its DB; TestClient does not start background jobs."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "followups.db")
    monkeypatch.setattr(studio, "_today", lambda: TODAY)
    db.migrate()
    ratelimit._hits.clear()
    client = TestClient(app)
    assert client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield client
    client.close()


def prospect(label="Synthetic follow-up bistro", qualification="potential_fit"):
    return db.run(
        "INSERT INTO prospects (business, source, qualification, business_key, contact_key) "
        "VALUES (?, 'Synthetic fixture', ?, ?, '')",
        (label, qualification, label.casefold()),
    )


def schedule_form(**changes):
    return {
        "title": "Call about seasonal menu photos",
        "due_date": TODAY.isoformat(),
        "operation_key": uuid.uuid4().hex,
    } | changes


def schedule(admin, pid, **changes):
    form = schedule_form(**changes)
    response = admin.post(f"{ROOT}/{pid}/follow-ups", data=form, follow_redirects=False)
    assert response.status_code == 303, response.text
    return db.one("SELECT * FROM tasks WHERE schedule_key=?", (form["operation_key"],))


def contact_form(**changes):
    return {
        "operation_key": uuid.uuid4().hex,
        "channel": "call",
        "direction": "out",
        "outcome": "voicemail",
        "occurred_on": TODAY.isoformat(),
        "notes": "Left a manually recorded voicemail.",
        "task_action": "keep",
    } | changes


def command_form(task, **changes):
    return {
        "action": "complete",
        "version": str(task["version"]),
        "note": "Finished the planned outreach attempt.",
    } | changes


def command(admin, pid, task, **changes):
    return admin.post(
        f"{ROOT}/{pid}/follow-ups/{task['id']}",
        data=command_form(task, **changes),
        follow_redirects=False,
    )


def count(table):
    tables = {
        "tasks",
        "prospects",
        "prospect_contact_events",
        "audit_log",
        "clients",
        "inquiries",
        "projects",
        "messages",
        "jobs",
        "emails_log",
        "invoices",
        "payments",
    }
    return db.one(f"SELECT COUNT(*) n FROM {db.ident(table, tables)}")["n"]


def unrelated_counts():
    return {
        name: count(name)
        for name in (
            "clients",
            "inquiries",
            "projects",
            "messages",
            "jobs",
            "emails_log",
            "invoices",
            "payments",
        )
    }


def test_schedule_retry_reuses_task_and_rejects_changed_payload(admin):
    pid = prospect()
    form = schedule_form()
    url = f"{ROOT}/{pid}/follow-ups"
    before = unrelated_counts()
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    saved = dict(db.one("SELECT * FROM tasks WHERE prospect_id=?", (pid,)))
    audits = count("audit_log")
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    assert dict(db.one("SELECT * FROM tasks WHERE prospect_id=?", (pid,))) == saved
    assert count("audit_log") == audits
    changed = admin.post(url, data=form | {"title": "Different next step"}, follow_redirects=False)
    assert changed.status_code == 409
    assert count("tasks") == 1 and count("audit_log") == audits
    assert unrelated_counts() == before


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_schedule_has_one_open_next_action(admin, same_key):
    pid = prospect()
    form = schedule_form()
    cookies = dict(admin.cookies)
    barrier = threading.Barrier(2)

    def submit(i):
        client = TestClient(app)
        client.cookies.update(cookies)
        payload = form if same_key or i == 0 else form | {"operation_key": uuid.uuid4().hex}
        try:
            barrier.wait(timeout=10)
            return client.post(
                f"{ROOT}/{pid}/follow-ups", data=payload, follow_redirects=False
            ).status_code
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, (0, 1)))
    assert statuses == ([303, 303] if same_key else [303, 409])
    assert count("tasks") == 1
    assert count("audit_log") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"due_date": ""},
        {"due_date": "2037-02-29"},
        {"due_date": "2037-04-15T12:30:00"},
        {"due_date": "20370415"},
        {"operation_key": "short"},
    ],
)
def test_invalid_schedule_preserves_entries_without_writing(admin, changes):
    pid = prospect()
    form = schedule_form(**changes)
    response = admin.post(f"{ROOT}/{pid}/follow-ups", data=form, follow_redirects=False)
    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert response.context["values"]["operation_key"] == form["operation_key"]
    assert response.context["values"]["title"] == form["title"].strip()
    assert count("tasks") == 0 and count("audit_log") == 0


def test_database_enforces_one_open_and_exclusive_ownership(admin):
    pid = prospect()
    task = schedule(admin, pid)
    cid = db.run("INSERT INTO clients (name) VALUES ('Synthetic existing client')")
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?, 'Existing project')", (cid,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.run(
            "INSERT INTO tasks (title, due_date, prospect_id) VALUES ('Second', ?, ?)",
            (TODAY.isoformat(), pid),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.run("UPDATE tasks SET project_id=? WHERE id=?", (project_id, task["id"]))
    assert db.one("SELECT project_id FROM tasks WHERE id=?", (task["id"],))["project_id"] is None


def test_database_rejects_contact_links_to_another_prospects_task(admin):
    pid, other_pid = prospect(), prospect("Separate synthetic business")
    task = schedule(admin, pid)
    with pytest.raises(sqlite3.IntegrityError):
        db.run(
            "INSERT INTO prospect_contact_events "
            "(prospect_id, operation_key, request_digest, channel, direction, outcome, "
            "occurred_on, task_id, task_action) VALUES (?,?,?, 'call','out','voicemail',?,?, 'complete')",
            (other_pid, uuid.uuid4().hex, "synthetic-digest", TODAY.isoformat(), task["id"]),
        )
    assert count("prospect_contact_events") == 0


@pytest.mark.parametrize("action", ["complete", "cancel"])
def test_terminal_commands_are_explicit_and_replay_safe(admin, action):
    pid = prospect()
    task = schedule(admin, pid)
    note = "Completed planned attempt" if action == "complete" else "No current priority"
    assert command(admin, pid, task, action=action, note=note).status_code == 303
    after = dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],)))
    audits = count("audit_log")
    assert command(admin, pid, task, action=action, note=note).status_code == 303
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == after
    assert count("audit_log") == audits and count("prospect_contact_events") == 0
    assert after["done"] == (1 if action == "complete" else 0)
    assert bool(after["canceled_at"]) == (action == "cancel")
    assert after["completion_note" if action == "complete" else "cancel_reason"] == note
    other = "cancel" if action == "complete" else "complete"
    assert command(admin, pid, task, action=other).status_code == 409
    # A terminal action releases the one-open slot but its history remains.
    replacement = schedule(admin, pid, title="Research the next opportunity")
    assert replacement["id"] != task["id"] and count("tasks") == 2


def test_reschedule_rejects_stale_version_and_does_not_erase_history(admin):
    pid = prospect()
    task = schedule(admin, pid)
    form = command_form(
        task, action="reschedule", due_date="2037-04-19", note="Asked for next week"
    )
    url = f"{ROOT}/{pid}/follow-ups/{task['id']}"
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    latest = dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],)))
    assert latest["due_date"] == "2037-04-19" and latest["version"] > task["version"]
    history = admin.get(f"{ROOT}/{pid}").text
    assert "Asked for next week" in history
    assert TODAY.isoformat() in history and "2037-04-19" in history
    audits = count("audit_log")
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    assert count("audit_log") == audits
    stale = admin.post(
        url,
        data=form | {"due_date": "2037-05-02", "note": "Old tab proposes another date"},
        follow_redirects=False,
    )
    assert stale.status_code == 409
    assert stale.context["values"]["due_date"] == "2037-05-02"
    assert stale.context["values"]["version"] == str(task["version"])
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == latest
    assert (
        command(
            admin, pid, latest, action="reschedule", due_date="2037-04-20", note="Newer decision"
        ).status_code
        == 303
    )
    assert admin.post(url, data=form, follow_redirects=False).status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"note": ""},
        {"note": "x" * 1001},
        {"version": "-1"},
        {"action": "toggle"},
        {"action": "reschedule", "due_date": "2037-02-29"},
    ],
)
def test_invalid_task_command_leaves_state_and_history_unchanged(admin, changes):
    pid = prospect()
    task = schedule(admin, pid)
    audits = count("audit_log")
    response = command(admin, pid, task, **changes)
    assert response.status_code == 400
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)
    assert count("audit_log") == audits


def test_outgoing_attempt_and_manual_reply_are_distinct_and_do_not_complete_task(admin):
    pid = prospect()
    task = schedule(admin, pid)
    prospect_before = dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,)))
    before = unrelated_counts()
    form = contact_form(occurred_on="2020-01-02")
    url = f"{ROOT}/{pid}/contacts"
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    event = dict(db.one("SELECT * FROM prospect_contact_events"))
    assert event["direction"] == "out" and event["outcome"] == "voicemail"
    assert event["provenance"] == "manual"
    assert event["occurred_on"] == "2020-01-02"
    assert event["recorded_at"][:10] != event["occurred_on"]
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)
    reply = contact_form(
        channel="email", direction="in", outcome="reply_received", notes="Asked to discuss a shoot"
    )
    assert admin.post(url, data=reply, follow_redirects=False).status_code == 303
    assert count("prospect_contact_events") == 2
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)
    assert dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,))) == prospect_before
    assert unrelated_counts() == before


def test_contact_retry_deduplicates_and_rejects_changed_payload_or_prospect(admin):
    pid, other_pid = prospect(), prospect("Different synthetic prospect")
    form = contact_form()
    url = f"{ROOT}/{pid}/contacts"
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    event = dict(db.one("SELECT * FROM prospect_contact_events"))
    audits = count("audit_log")
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    assert (
        admin.post(url, data=form | {"notes": "Changed note"}, follow_redirects=False).status_code
        == 409
    )
    assert (
        admin.post(f"{ROOT}/{other_pid}/contacts", data=form, follow_redirects=False).status_code
        == 409
    )
    assert dict(db.one("SELECT * FROM prospect_contact_events")) == event
    assert count("prospect_contact_events") == 1 and count("audit_log") == audits


@pytest.mark.parametrize(
    "changes",
    [
        {"channel": "fax"},
        {"direction": "sideways"},
        {"outcome": "delivered"},
        {"direction": "in", "outcome": "voicemail"},
        {"direction": "out", "outcome": "reply_received"},
        {"channel": "email", "outcome": "no_answer"},
        {"channel": "call", "outcome": "sent"},
        {"occurred_on": "2037-04-16"},
        {"occurred_on": "2037-02-29"},
        {"occurred_on": "20370415"},
        {"notes": "x" * 4001},
        {"operation_key": ""},
        {"task_action": "delete"},
    ],
)
def test_invalid_contact_does_not_invent_activity(admin, changes):
    pid = prospect()
    form = contact_form(**changes)
    response = admin.post(f"{ROOT}/{pid}/contacts", data=form, follow_redirects=False)
    assert response.status_code == 400
    assert response.context["values"]["notes"] == form["notes"]
    assert response.context["values"]["operation_key"] == form["operation_key"]
    assert count("prospect_contact_events") == 0 and count("audit_log") == 0


@pytest.mark.parametrize("action", ["complete", "reschedule"])
def test_combined_contact_and_task_change_are_explicit_and_retry_safe(admin, action):
    pid = prospect()
    task = schedule(admin, pid)
    form = contact_form(
        task_action=action,
        task_id=str(task["id"]),
        version=str(task["version"]),
        task_note="Explicit operator decision",
        due_date="2037-04-20",
    )
    url = f"{ROOT}/{pid}/contacts"
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    saved = dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],)))
    assert saved["done"] == (1 if action == "complete" else 0)
    assert saved["due_date"] == ("2037-04-20" if action == "reschedule" else TODAY.isoformat())
    event = dict(db.one("SELECT * FROM prospect_contact_events"))
    assert event["task_id"] == task["id"] and event["task_action"] == action
    audits = count("audit_log")
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    assert count("prospect_contact_events") == 1 and count("audit_log") == audits
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == saved


def test_combined_stale_form_preserves_input_and_writes_nothing(admin):
    pid = prospect()
    task = schedule(admin, pid)
    assert (
        command(
            admin, pid, task, action="reschedule", due_date="2037-04-16", note="New date"
        ).status_code
        == 303
    )
    before = dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],)))
    audits = count("audit_log")
    form = contact_form(
        task_action="complete",
        task_id=str(task["id"]),
        version=str(task["version"]),
        task_note="Old tab's completion",
        notes="Keep this unsaved observation",
    )
    response = admin.post(f"{ROOT}/{pid}/contacts", data=form, follow_redirects=False)
    assert response.status_code == 409
    assert response.context["values"]["notes"] == form["notes"]
    assert response.context["values"]["operation_key"] == form["operation_key"]
    assert response.context["values"]["task_id"] == str(task["id"])
    assert response.context["values"]["version"] == str(task["version"])
    assert "Keep this unsaved observation" in response.text
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == before
    assert count("prospect_contact_events") == 0 and count("audit_log") == audits


def test_new_contact_cannot_reuse_an_already_accepted_task_command(admin):
    pid = prospect()
    task = schedule(admin, pid)
    form = contact_form(
        task_action="complete",
        task_id=str(task["id"]),
        version=str(task["version"]),
        task_note="Finished the intended call",
    )
    url = f"{ROOT}/{pid}/contacts"
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    audits = count("audit_log")
    # Same operation is a retry; a new contact must review the task's new state.
    assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    assert (
        admin.post(
            url, data=form | {"operation_key": uuid.uuid4().hex}, follow_redirects=False
        ).status_code
        == 409
    )
    assert count("prospect_contact_events") == 1 and count("audit_log") == audits


def test_audit_failure_rolls_back_contact_and_task_together(admin, monkeypatch):
    pid = prospect()
    task = schedule(admin, pid)
    audits = count("audit_log")
    original = audit.log
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("Synthetic combined audit failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "log", fail_second)
    with pytest.raises(RuntimeError, match="Synthetic combined audit failure"):
        admin.post(
            f"{ROOT}/{pid}/contacts",
            data=contact_form(
                task_action="complete",
                task_id=str(task["id"]),
                version=str(task["version"]),
                task_note="Explicit completion",
            ),
        )
    assert len(calls) == 2
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)
    assert count("prospect_contact_events") == 0 and count("audit_log") == audits


def test_task_and_contact_commands_cannot_cross_prospects(admin):
    pid, other_pid = prospect(), prospect("Another synthetic business")
    task = schedule(admin, pid)
    assert command(admin, other_pid, task).status_code == 404
    form = contact_form(
        task_action="complete",
        task_id=str(task["id"]),
        version=str(task["version"]),
        task_note="Wrong prospect's task",
    )
    assert (
        admin.post(f"{ROOT}/{other_pid}/contacts", data=form, follow_redirects=False).status_code
        == 404
    )
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)
    assert count("prospect_contact_events") == 0


def test_shared_task_controls_cannot_bypass_prospect_history(admin):
    pid = prospect()
    task = schedule(admin, pid)
    assert command(admin, pid, task).status_code == 303
    replacement = schedule(admin, pid, title="The real current next step")
    for tid in (task["id"], replacement["id"]):
        before = dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,)))
        for endpoint in ("toggle", "delete"):
            response = admin.post(f"/admin/tasks/{tid}/{endpoint}", follow_redirects=False)
            assert response.status_code == 409
            assert dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,))) == before
    board = admin.get("/admin/tasks")
    assert board.status_code == 200
    assert f"{ROOT}/{pid}" in board.text
    assert f'formaction="/admin/tasks/{task["id"]}/delete"' not in board.text
    assert f'action="/admin/tasks/{replacement["id"]}/toggle"' not in board.text


def test_general_and_project_task_controls_still_work(admin):
    cid = db.run("INSERT INTO clients (name) VALUES ('Existing client')")
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?, 'Existing shoot')", (cid,)
    )
    for project in ("", str(project_id)):
        assert (
            admin.post(
                "/admin/tasks",
                data={"title": "Existing task", "project_id": project},
                follow_redirects=False,
            ).status_code
            == 303
        )
        task = db.one("SELECT * FROM tasks ORDER BY id DESC LIMIT 1")
        assert task["prospect_id"] is None
        assert (
            admin.post(f"/admin/tasks/{task['id']}/toggle", follow_redirects=False).status_code
            == 303
        )
        assert db.one("SELECT done FROM tasks WHERE id=?", (task["id"],))["done"] == 1
        assert (
            admin.post(f"/admin/tasks/{task['id']}/toggle", follow_redirects=False).status_code
            == 303
        )
        assert db.one("SELECT done FROM tasks WHERE id=?", (task["id"],))["done"] == 0
        assert (
            admin.post(f"/admin/tasks/{task['id']}/delete", follow_redirects=False).status_code
            == 303
        )
        assert db.one("SELECT id FROM tasks WHERE id=?", (task["id"],)) is None


def test_cancellation_changes_due_counts_without_counting_as_completion(admin):
    pid = prospect()
    before = home_context._ctx_headline_counts(TODAY.isoformat())["action_items"]
    done_week = admin.get("/admin/tasks").context["done_week"]
    task = schedule(admin, pid)
    assert home_context._ctx_headline_counts(TODAY.isoformat())["action_items"] == before + 1
    assert task["id"] in {r["id"] for r in home_context._ctx_open_tasks(TODAY.isoformat())}
    assert (
        command(admin, pid, task, action="cancel", note="Not a priority this month").status_code
        == 303
    )
    assert home_context._ctx_headline_counts(TODAY.isoformat())["action_items"] == before
    assert task["id"] not in {r["id"] for r in home_context._ctx_open_tasks(TODAY.isoformat())}
    assert admin.get("/admin/tasks").context["done_week"] == done_week
    assert (
        db.one("SELECT qualification FROM prospects WHERE id=?", (pid,))["qualification"]
        == "potential_fit"
    )
    page = admin.get(f"{ROOT}/{pid}")
    assert "Not a priority this month" in page.text


def test_daily_queue_uses_studio_date_and_separates_unplanned_work(admin):
    overdue = prospect("Overdue synthetic business")
    today = prospect("Today synthetic business")
    future = prospect("Future synthetic business")
    unplanned = prospect("Unplanned synthetic business")
    not_fit = prospect("Not-fit synthetic business", qualification="not_fit")
    canceled = prospect("Canceled synthetic business")
    schedule(admin, overdue, due_date="2037-04-14")
    schedule(admin, today)
    schedule(admin, future, due_date="2037-04-16")
    canceled_task = schedule(admin, canceled)
    assert (
        command(
            admin, canceled, canceled_task, action="cancel", note="No current priority"
        ).status_code
        == 303
    )
    due = admin.get(ROOT + "/follow-ups")
    assert due.status_code == 200
    assert [row["id"] for row in due.context["prospects"]] == [overdue, today]
    assert due.context["total"] == 2
    upcoming = admin.get(ROOT + "/follow-ups?filter=upcoming")
    assert [row["id"] for row in upcoming.context["prospects"]] == [future]
    missing = admin.get(ROOT + "/follow-ups?filter=unplanned")
    assert {row["id"] for row in missing.context["prospects"]} == {unplanned, canceled}
    assert "No current priority" in missing.text
    assert not_fit not in {row["id"] for row in missing.context["prospects"]}
    # The all-prospects view is explicit; not-fit records remain discoverable.
    all_rows = admin.get(ROOT + "/follow-ups?filter=all").context["prospects"]
    assert {row["id"] for row in all_rows} == {overdue, today, future, unplanned, not_fit, canceled}
    assert [row["id"] for row in admin.get(ROOT + "/follow-ups?q=Today").context["prospects"]] == [
        today
    ]


def test_latest_completed_action_does_not_inherit_older_cancellation_reason(admin):
    pid = prospect()
    canceled = schedule(admin, pid)
    assert (
        command(admin, pid, canceled, action="cancel", note="Old cancellation reason").status_code
        == 303
    )
    newer = schedule(admin, pid, title="A later explicit attempt")
    assert command(admin, pid, newer, note="Finished the later attempt").status_code == 303
    row = admin.get(ROOT + "/follow-ups?filter=unplanned").context["prospects"][0]
    assert not row["last_cancel_reason"]
    history = admin.get(f"{ROOT}/{pid}").text
    assert "Old cancellation reason" in history and "Finished the later attempt" in history


def test_attempt_and_reply_clocks_are_ordered_by_occurrence_and_survive_research_edits(admin):
    pid = prospect()
    task = schedule(admin, pid)
    url = f"{ROOT}/{pid}/contacts"
    for form in (
        contact_form(occurred_on="2037-04-10"),
        contact_form(
            channel="email", direction="in", outcome="reply_received", occurred_on="2037-04-09"
        ),
        contact_form(occurred_on="2037-04-01"),
        contact_form(
            channel="other",
            direction="in",
            outcome="other",
            occurred_on="2037-04-14",
            notes="Unclassified incoming activity is not an established reply",
        ),
    ):
        assert admin.post(url, data=form, follow_redirects=False).status_code == 303
    original = dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,)))
    research = {
        key: original[key]
        for key in (
            "business",
            "contact_name",
            "email",
            "phone",
            "specialty",
            "source",
            "qualification",
            "notes",
        )
    } | {"notes": "Updated research without contacting the prospect"}
    assert admin.post(f"{ROOT}/{pid}", data=research, follow_redirects=False).status_code == 303
    row = admin.get(ROOT + "/follow-ups").context["prospects"][0]
    assert row["last_attempt_on"] == "2037-04-10"
    assert row["last_reply_on"] == "2037-04-09"
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))) == dict(task)


def test_admin_auth_and_cross_origin_protection_cover_all_new_writes(admin):
    pid = prospect()
    task = schedule(admin, pid)
    forms = (
        (f"{ROOT}/{pid}/follow-ups", schedule_form()),
        (f"{ROOT}/{pid}/follow-ups/{task['id']}", command_form(task)),
        (f"{ROOT}/{pid}/contacts", contact_form()),
    )
    before = (count("tasks"), count("prospect_contact_events"), count("audit_log"))
    anonymous = TestClient(app)
    try:
        assert anonymous.get(ROOT + "/follow-ups", follow_redirects=False).status_code == 303
        for url, data in forms:
            assert anonymous.post(url, data=data, follow_redirects=False).status_code == 303
            assert (
                admin.post(
                    url,
                    data=data,
                    headers={"Origin": "https://other.example"},
                    follow_redirects=False,
                ).status_code
                == 403
            )
    finally:
        anonymous.close()
    assert (count("tasks"), count("prospect_contact_events"), count("audit_log")) == before


@pytest.mark.parametrize("screening_room", [True, False])
def test_escaped_detail_queue_and_error_render_in_both_styles(admin, monkeypatch, screening_room):
    monkeypatch.setattr(config, "SCREENING_ROOM", screening_room)
    pid = prospect("Synthetic <script>business()</script>")
    task = schedule(admin, pid, title="Call <script>attempt()</script>")
    form = contact_form(notes="</textarea><script>history()</script>")
    assert (
        admin.post(f"{ROOT}/{pid}/contacts", data=form, follow_redirects=False).status_code == 303
    )
    before = (count("tasks"), count("prospect_contact_events"), count("audit_log"))
    for url in (f"{ROOT}/{pid}", ROOT + "/follow-ups"):
        response = admin.get(url)
        assert response.status_code == 200
        assert "<script>business()</script>" not in response.text
        assert "<script>attempt()</script>" not in response.text
        assert "&lt;script&gt;business()&lt;/script&gt;" in response.text
    detail = admin.get(f"{ROOT}/{pid}")
    assert "&lt;/textarea&gt;&lt;script&gt;history()&lt;/script&gt;" in detail.text
    assert "Recorded manually" in detail.text
    error = command(admin, pid, task, version="0", note="</textarea><script>error()</script>")
    assert error.status_code == 400
    assert "<script>error()</script>" not in error.text
    assert "&lt;/textarea&gt;" in error.text
    assert (count("tasks"), count("prospect_contact_events"), count("audit_log")) == before


def test_upgrade_preserves_legacy_tasks_and_migration_is_idempotent(tmp_path, monkeypatch):
    original = db.MIGRATIONS_DIR
    old = tmp_path / "before-followups"
    old.mkdir()
    for path in original.glob("*.sql"):
        if path.name < "085_":
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "upgrade.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    db.migrate()
    cid = db.run("INSERT INTO clients (name) VALUES ('Legacy task owner')")
    project_id = db.run(
        "INSERT INTO projects (client_id, title) VALUES (?, 'Legacy shoot')", (cid,)
    )
    db.run("INSERT INTO tasks (title) VALUES ('Undated general task')")
    db.run(
        "INSERT INTO tasks (title, due_date, project_id) VALUES ('Future project task', '2037-05-01', ?)",
        (project_id,),
    )
    db.run(
        "INSERT INTO tasks (title, done, done_at) VALUES ('Finished general task', 1, '2026-01-01 12:00:00')"
    )
    legacy = [dict(r) for r in db.all_("SELECT * FROM tasks ORDER BY id")]
    monkeypatch.setattr(db, "MIGRATIONS_DIR", original)
    db.migrate()
    db.migrate()
    after = [dict(r) for r in db.all_("SELECT * FROM tasks ORDER BY id")]
    for before, current in zip(legacy, after, strict=True):
        assert {key: current[key] for key in before} == before
        assert current["prospect_id"] is None and current["canceled_at"] is None
    assert count("prospect_contact_events") == 0
    assert (
        db.one("SELECT COUNT(*) n FROM schema_migrations WHERE name='085_prospect_followups.sql'")[
            "n"
        ]
        == 1
    )
