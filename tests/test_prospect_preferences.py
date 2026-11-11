"""Outreach holds on isolated SQLite; no providers or background workers."""

import datetime as dt
import json
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import audit, config, db, outreach, ratelimit, security
from app.admin import home_context, prospect_preferences, studio
from app.main import app

pytestmark = pytest.mark.integration
ROOT = "/admin/studio/prospects"
DAY = "2030-04-15"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "preferences.db")
    monkeypatch.setattr(config, "BRAND_DIR", tmp_path / "brand")
    monkeypatch.setattr(studio, "_today", lambda: dt.date.fromisoformat(DAY))
    db.migrate()
    ratelimit._hits.clear()
    c = TestClient(app)
    assert c.post("/admin/login", data={"password": config.ADMIN_PASSWORD}).status_code == 200
    yield c
    c.close()


def prospect(label="Synthetic bistro"):
    return db.run(
        "INSERT INTO prospects (business,contact_name,email,phone,source,qualification,business_key,contact_key) "
        "VALUES (?, 'Fixture chef', 'chef@fixture.example', '8285550123', 'Synthetic research', 'potential_fit', ?, 'fixture chef')",
        (label, label.casefold()),
    )


def payload(admin, pid, **changes):
    page = admin.get(f"{ROOT}/{pid}/outreach")
    assert page.status_code == 200
    values = dict(page.context["values"])
    values = {k: str(v) if v else "" for k, v in values.items()}
    return (
        values | {"token": page.context["token"], "reason": "Fixture operator decision"} | changes
    )


def save(admin, pid, **changes):
    result = admin.post(
        f"{ROOT}/{pid}/outreach", data=payload(admin, pid, **changes), follow_redirects=False
    )
    assert result.status_code == 303, result.text
    return result


def state(pid):
    return outreach.read_index(DAY)["prospects"][pid]


def preview(admin, pid, channel="email"):
    return admin.post(
        f"{ROOT}/{pid}/drafts/preview",
        data={
            "channel": channel,
            "purpose": "introduction",
            "operation_key": str(uuid.uuid4()),
        },
    )


def message(page):
    return {
        "token": page.context["token"],
        "subject": page.context["values"]["subject"],
        "body": page.context["values"]["body"],
    }


def task(pid, day=DAY):
    return db.run(
        "INSERT INTO tasks (prospect_id,title,due_date) VALUES (?, 'Discuss photography', ?)",
        (pid, day),
    )


def handoff(admin, pid, cid="new", *, commit=True):
    values = dict(admin.get(f"{ROOT}/{pid}/handoff?client_id={cid}").context["values"])
    values.update(
        title="Menu photographs", photography_need="Seasonal menu", interest_confirmed="yes"
    )
    if values["task_id"]:
        values["task_disposition"] = "keep"
    review = admin.post(f"{ROOT}/{pid}/handoff/review", data=values)
    assert review.status_code == 200, review.text
    body = values | {"review_token": review.context["review_token"]}
    if not commit:
        return review, body
    result = admin.post(f"{ROOT}/{pid}/handoff", data=body, follow_redirects=False)
    assert result.status_code == 303, result.text
    return dict(db.one("SELECT * FROM prospect_handoffs WHERE prospect_id=?", (pid,)))


def audit_rows():
    return db.all_("SELECT * FROM audit_log WHERE action='outreach_preferences'")


def test_default_is_read_only_and_is_not_consent(admin):
    pid = prospect()
    for url in (f"{ROOT}/{pid}/outreach", f"{ROOT}/{pid}"):
        page = admin.get(url)
        assert page.status_code == 200
        assert "No outreach hold" in page.text and "does not establish consent" in page.text
    assert not state(pid)["held"] and not state(pid)["sources"]
    assert not db.all_("SELECT * FROM prospect_outreach_preferences") and not audit_rows()
    assert page.context["outreach"]["members"][0]["id"] == pid


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "stopped"},
        {"mode": "snoozed", "snoozed_until": DAY},
        {"email_blocked": "1", "sms_blocked": "1", "call_blocked": "1"},
    ],
)
def test_holds_hide_all_active_work_without_changing_it(admin, changes):
    pid = prospect()
    task_id = task(pid)
    before = dict(db.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
    save(admin, pid, **changes)
    assert state(pid)["held"]
    for filter_name in ("due", "upcoming", "unplanned"):
        assert admin.get(f"{ROOT}/follow-ups?filter={filter_name}").context["total"] == 0
    for filter_name in ("held", "all"):
        assert admin.get(f"{ROOT}/follow-ups?filter={filter_name}").context["total"] == 1
    assert admin.get("/admin/tasks").context["held_count"] == 1
    assert admin.get("/admin/tasks").context["open_count"] == 0
    assert not home_context._ctx_open_tasks(DAY)
    assert home_context._ctx_headline_counts(DAY)["action_items"] == 0
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (task_id,))) == before
    assert preview(admin, pid).status_code == 409
    assert not db.all_("SELECT * FROM prospect_contact_events")


def test_new_schedule_blocked_but_exact_previous_retry_and_history_work(admin):
    pid = prospect()
    schedule = {"title": "Phone discussion", "due_date": DAY, "operation_key": str(uuid.uuid4())}
    url = f"{ROOT}/{pid}/follow-ups"
    assert admin.post(url, data=schedule, follow_redirects=False).status_code == 303
    tid = db.one("SELECT id FROM tasks WHERE prospect_id=?", (pid,))["id"]
    save(admin, pid, mode="stopped", email_blocked="1")
    assert admin.post(url, data=schedule, follow_redirects=False).status_code == 303
    assert admin.post(url, data=schedule | {"operation_key": str(uuid.uuid4())}).status_code == 409
    # Recording something that already happened remains possible, even on a blocked channel.
    result = admin.post(
        f"{ROOT}/{pid}/contacts",
        data={
            "operation_key": str(uuid.uuid4()),
            "channel": "email",
            "direction": "out",
            "outcome": "sent",
            "occurred_on": DAY,
            "notes": "Historical manual contact",
            "task_action": "keep",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    result = admin.post(
        url + f"/{tid}",
        data={"version": "1", "action": "cancel", "note": "Stopped outreach"},
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    assert db.one("SELECT canceled_at FROM tasks WHERE id=?", (tid,))["canceled_at"]
    assert len(db.all_("SELECT * FROM prospect_contact_events")) == 1


def test_snooze_inclusive_boundary_returns_original_due_date_without_writes(admin, monkeypatch):
    pid = prospect()
    tid = task(pid)
    save(admin, pid, mode="snoozed", snoozed_until=DAY)
    assert state(pid)["held"]
    before = dict(db.one("SELECT * FROM prospect_outreach_preferences WHERE prospect_id=?", (pid,)))
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2030, 4, 16))
    page = admin.get(f"{ROOT}/follow-ups")
    assert page.context["total"] == 1 and "Overdue" in page.text
    assert preview(admin, pid).status_code == 200
    assert (
        dict(db.one("SELECT * FROM prospect_outreach_preferences WHERE prospect_id=?", (pid,)))
        == before
    )
    assert db.one("SELECT due_date FROM tasks WHERE id=?", (tid,))["due_date"] == DAY
    assert len(audit_rows()) == 1


@pytest.mark.parametrize("channel", ["email", "sms"])
def test_channel_exclusion_and_advisory_preference(admin, channel):
    pid = prospect()
    other = "sms" if channel == "email" else "email"
    save(admin, pid, **{channel + "_blocked": "1", "preferred_channel": other})
    assert not state(pid)["held"]
    assert preview(admin, pid, channel).status_code == 409
    assert preview(admin, pid, other).status_code == 200
    page = admin.get(f"{ROOT}/{pid}/drafts")
    assert page.context["values"]["channel"] == other
    assert "Excluded channels" in page.text


def test_preferences_invalidate_preview_saved_edit_and_copy_after_resume(admin):
    pid = prospect()
    prepared = preview(admin, pid)
    saved = admin.post(f"{ROOT}/{pid}/drafts", data=message(prepared), follow_redirects=False)
    url = saved.headers["location"]
    editor = admin.get(url)
    pending_preview = preview(admin, pid)
    save(admin, pid, mode="stopped")
    assert admin.post(f"{ROOT}/{pid}/drafts", data=message(pending_preview)).status_code == 409
    for suffix in ("", "/copy"):
        assert admin.post(url + suffix, data=message(editor)).status_code == 409
    save(admin, pid, mode="active", reason="Explicit review of renewed interest")
    assert preview(admin, pid).status_code == 200
    # Stop/resume cannot revive a previously prepared text without a new context review.
    assert admin.post(url + "/copy", data=message(editor)).status_code == 409
    assert len(db.all_("SELECT * FROM prospect_message_drafts")) == 1


def test_recorded_not_interested_is_not_cleared_by_resume(admin):
    pid = prospect()
    db.run(
        "INSERT INTO prospect_contact_events (prospect_id,operation_key,request_digest,channel,direction,outcome,occurred_on) VALUES (?,?, 'fixture', 'email', 'in', 'not_interested', ?)",
        (pid, str(uuid.uuid4()), DAY),
    )
    save(admin, pid, mode="stopped")
    save(admin, pid, mode="active")
    assert preview(admin, pid).status_code == 409
    assert "Not interested" in preview(admin, pid).text


def test_client_wide_union_future_project_and_separate_sources(admin):
    first, second, unrelated = prospect("First"), prospect("Second"), prospect("Unrelated")
    link = handoff(admin, first)
    handoff(admin, second, str(link["client_id"]))
    future = db.run(
        "INSERT INTO projects (client_id,title,status,created_at) VALUES (?, 'Future work', 'inquiry_received','2030-01-01')",
        (link["client_id"],),
    )
    save(admin, first, mode="stopped", email_blocked="1", preferred_channel="sms")
    save(admin, second, sms_blocked="1", preferred_channel="call")
    combined = state(second)
    assert combined["held"] and combined["blocked"] == ["email", "sms"]
    assert combined["preference_conflict"] and not combined["preferred_channel"]
    assert not state(unrelated)["held"]  # same email/phone alone never expands scope
    assert preview(admin, second).status_code == 409
    for url in (f"/admin/studio/clients/{link['client_id']}", f"/admin/studio/projects/{future}"):
        page = admin.get(url)
        assert page.status_code == 200
        assert page.context["outreach"]["held"] and "across this client" in page.text
    assert not admin.get("/admin/studio").context["sales_review"]
    assert (
        db.one("SELECT status FROM projects WHERE id=?", (future,))["status"] == "inquiry_received"
    )
    save(admin, second, mode="active")
    assert state(second)["held"]  # resuming second cannot clear first's stop
    save(admin, first, mode="active", email_blocked="", preferred_channel="")
    assert not state(second)["held"] and state(second)["blocked"] == ["sms"]
    assert preview(admin, second).status_code == 200
    assert preview(admin, second, "sms").status_code == 409


def test_handoff_reviews_combined_scope_and_refuses_preference_changes(admin):
    first, second = prospect("First"), prospect("Second")
    link = handoff(admin, first)
    save(admin, first, mode="stopped")
    review, submitted = handoff(admin, second, str(link["client_id"]), commit=False)
    assert review.context["review"]["outreach"]["held"]
    assert "restrictions from this prospect" in review.text
    save(admin, first, mode="active")
    result = admin.post(f"{ROOT}/{second}/handoff", data=submitted)
    assert result.status_code == 409
    assert not db.one("SELECT * FROM prospect_handoffs WHERE prospect_id=?", (second,))


def test_new_client_handoff_carries_own_stop_and_stales_old_preference_form(admin):
    pid = prospect()
    save(admin, pid, mode="stopped")
    old = payload(admin, pid, mode="active")
    link = handoff(admin, pid)
    assert admin.get(f"/admin/studio/projects/{link['project_id']}").context["outreach"]["held"]
    assert admin.post(f"{ROOT}/{pid}/outreach", data=old).status_code == 409
    assert state(pid)["held"]


def test_family_membership_change_refuses_an_already_open_form(admin):
    first, second = prospect("First"), prospect("Second")
    link = handoff(admin, first)
    old = payload(admin, first, mode="stopped")
    handoff(admin, second, str(link["client_id"]))
    result = admin.post(f"{ROOT}/{first}/outreach", data=old)
    assert result.status_code == 409 and result.context["values"]["mode"] == "stopped"
    assert not state(first)["held"]


def test_exact_retry_stale_conflict_and_audited_reason(admin):
    pid = prospect()
    values = payload(admin, pid, mode="stopped", reason="Requested no further sales outreach")
    for _ in range(2):
        assert (
            admin.post(f"{ROOT}/{pid}/outreach", data=values, follow_redirects=False).status_code
            == 303
        )
    conflict = admin.post(f"{ROOT}/{pid}/outreach", data=values | {"mode": "active"})
    assert conflict.status_code == 409 and conflict.context["token"] == values["token"]
    assert len(audit_rows()) == 1 and state(pid)["sources"][0]["version"] == 1
    record = json.loads(audit_rows()[0]["diff_json"])
    assert record["before"] is None and record["after"]["reason"] == values["reason"]
    save(admin, pid, mode="active", reason="Reviewed renewed interest")
    assert len(audit_rows()) == 2
    assert json.loads(audit_rows()[1]["diff_json"])["before"]["mode"] == "stopped"


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "lost"},
        {"mode": "snoozed"},
        {"mode": "snoozed", "snoozed_until": "2030-02-30"},
        {"mode": "snoozed", "snoozed_until": "2030-04-14"},
        {"preferred_channel": "fax"},
        {"email_blocked": "yes"},
        {"email_blocked": "1", "preferred_channel": "email"},
        {"reason": ""},
        {"reason": "x" * 1001},
        {"reason": "bad\x00value"},
    ],
)
def test_invalid_controls_preserve_entries_and_do_not_write(admin, changes):
    pid = prospect()
    values = payload(admin, pid, **changes)
    result = admin.post(f"{ROOT}/{pid}/outreach", data=values)
    assert result.status_code == 400
    assert result.context["values"]["mode"] == values["mode"]
    assert not state(pid)["sources"] and not audit_rows()


@pytest.mark.parametrize("kind", ["missing", "tampered", "expired", "wrong_record", "malformed"])
def test_review_receipt_binding(admin, monkeypatch, kind):
    pid = prospect()
    values = payload(admin, pid, mode="stopped")
    if kind == "missing":
        values["token"] = ""
    elif kind == "tampered":
        values["token"] += "x"
    elif kind == "expired":
        monkeypatch.setattr(prospect_preferences, "TOKEN_AGE", -1)
    elif kind == "wrong_record":
        values["token"] = payload(admin, prospect("Other"))["token"]
    else:
        values["token"] = security.sign("[")
    assert admin.post(f"{ROOT}/{pid}/outreach", data=values).status_code == 409
    assert not state(pid)["sources"]


def test_concurrent_updates_do_not_overwrite_each_other(admin):
    pid = prospect()
    first = payload(admin, pid, mode="stopped")
    second = first | {"mode": "snoozed", "snoozed_until": DAY}
    barrier = threading.Barrier(2)

    def post(values):
        c = TestClient(app)
        c.cookies.update(admin.cookies)
        try:
            barrier.wait(timeout=10)
            return c.post(f"{ROOT}/{pid}/outreach", data=values, follow_redirects=False).status_code
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, [first, second]))
    assert sorted(results) == [303, 409]
    assert len(audit_rows()) == 1 and state(pid)["sources"][0]["version"] == 1


def test_audit_failure_rolls_back_create_and_edit(admin, monkeypatch):
    pid = prospect()

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    with monkeypatch.context() as m:
        m.setattr(audit, "log", fail)
        with pytest.raises(RuntimeError, match="Synthetic audit"):
            save(admin, pid, mode="stopped")
    assert not state(pid)["sources"]
    save(admin, pid, mode="stopped")
    before = state(pid)
    monkeypatch.setattr(audit, "log", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit"):
        save(admin, pid, mode="active")
    assert state(pid) == before and len(audit_rows()) == 1


def test_pagination_filters_before_count_and_limit_and_keeps_general_tasks(admin):
    for i in range(55):
        pid = prospect(f"Held {i:02}")
        task(pid)
        db.run(
            "INSERT INTO prospect_outreach_preferences (prospect_id,mode,reason,version,last_command_digest) VALUES (?, 'stopped', 'Fixture stop', 1, 'fixture')",
            (pid,),
        )
    active = prospect("Active after held")
    task(active)
    db.run("INSERT INTO tasks (title,due_date) VALUES ('General work', ?)", (DAY,))
    page = admin.get(f"{ROOT}/follow-ups")
    assert page.context["total"] == 1 and page.context["prospects"][0]["id"] == active
    held = admin.get(f"{ROOT}/follow-ups?filter=held&page=2")
    assert held.context["total"] == 55 and len(held.context["prospects"]) == 5
    assert admin.get(f"{ROOT}/follow-ups?filter=held&q=Held%2001").context["total"] == 1
    assert len(home_context._ctx_open_tasks(DAY)) == 2
    assert home_context._ctx_headline_counts(DAY)["action_items"] == 2
    assert admin.get("/admin/tasks").context["open_count"] == 2


@pytest.mark.parametrize("screening", [True, False])
def test_auth_origin_escaping_and_both_ui_modes(admin, monkeypatch, screening):
    monkeypatch.setattr(config, "SCREENING_ROOM", screening)
    pid = prospect("<script>fixture</script>")
    save(admin, pid, mode="stopped", reason="<img src=x onerror=fixture()>")
    page = admin.get(f"{ROOT}/{pid}/outreach")
    assert page.headers["cache-control"] == "no-store"
    assert "&lt;img" in page.text and "&lt;script&gt;fixture" in page.text
    assert "<img src=x" not in page.text
    values = payload(admin, pid, mode="active")
    assert (
        admin.post(
            f"{ROOT}/{pid}/outreach", data=values, headers={"Origin": "https://other.example"}
        ).status_code
        == 403
    )
    anonymous = TestClient(app)
    try:
        assert anonymous.get(f"{ROOT}/{pid}/outreach", follow_redirects=False).status_code == 303
        assert (
            anonymous.post(
                f"{ROOT}/{pid}/outreach", data=values, follow_redirects=False
            ).status_code
            == 303
        )
    finally:
        anonymous.close()
    assert admin.get(f"{ROOT}/999999/outreach").status_code == 404
    assert state(pid)["held"]


def test_additive_migration_preserves_existing_rows_and_enforces_constraints(tmp_path, monkeypatch):
    old = tmp_path / "before-preferences"
    old.mkdir()
    migrations = db.MIGRATIONS_DIR
    for path in migrations.glob("*.sql"):
        if path.name < "088_":
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "upgrade.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    db.migrate()
    pid = prospect()
    tid = task(pid)
    before = dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,)))
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    db.migrate()
    db.migrate()
    assert dict(db.one("SELECT * FROM tasks WHERE id=?", (tid,))) == before
    assert not db.all_("SELECT * FROM prospect_outreach_preferences")
    for mode, date in (
        ("snoozed", None),
        ("snoozed", "2030-02-30"),
        ("snoozed", "garbage"),
        ("stopped", DAY),
    ):
        with pytest.raises(sqlite3.IntegrityError), db.tx() as con:
            con.execute(
                "INSERT INTO prospect_outreach_preferences (prospect_id,mode,snoozed_until,reason,version,last_command_digest) VALUES (?,?,?,'Fixture',1,'fixture')",
                (pid, mode, date),
            )
    with db.tx() as con:
        con.execute(
            "INSERT INTO prospect_outreach_preferences (prospect_id,mode,reason,version,last_command_digest) VALUES (?,'stopped','Fixture',1,'fixture')",
            (pid,),
        )
    with pytest.raises(sqlite3.IntegrityError), db.tx() as con:
        con.execute("DELETE FROM prospects WHERE id=?", (pid,))
    with closing(db.connect()) as con:
        assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_linked_pauses_use_latest_date_and_a_stop_wins(admin, monkeypatch):
    first, second = prospect("First"), prospect("Second")
    link = handoff(admin, first)
    handoff(admin, second, str(link["client_id"]))
    save(admin, first, mode="snoozed", snoozed_until=DAY)
    save(admin, second, mode="snoozed", snoozed_until="2030-04-17")
    assert state(first)["snoozed_until"] == "2030-04-17"
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2030, 4, 16))
    assert preview(admin, first).status_code == 409
    save(admin, first, mode="stopped")
    monkeypatch.setattr(studio, "_today", lambda: dt.date(2030, 4, 18))
    assert "stopped" in admin.get(f"{ROOT}/{second}/outreach").context["outreach"]["label"]
    save(admin, first, mode="active")
    assert preview(admin, second).status_code == 200


def test_holds_exclude_upcoming_and_unplanned_and_stale_a_linked_draft(admin):
    first, second = prospect("First"), prospect("Second")
    link = handoff(admin, first)
    handoff(admin, second, str(link["client_id"]))
    prepared = preview(admin, second)
    saved = admin.post(f"{ROOT}/{second}/drafts", data=message(prepared), follow_redirects=False)
    editor = admin.get(saved.headers["location"])
    task(second, "2030-04-17")
    # The task itself is now part of the context; prepare a fresh editor.
    prepared = preview(admin, second)
    saved = admin.post(f"{ROOT}/{second}/drafts", data=message(prepared), follow_redirects=False)
    editor = admin.get(saved.headers["location"])
    save(admin, first, mode="stopped")
    assert admin.post(saved.headers["location"] + "/copy", data=message(editor)).status_code == 409
    assert admin.get(f"{ROOT}/follow-ups?filter=upcoming").context["total"] == 0
    unplanned = prospect("Unplanned")
    save(admin, unplanned, mode="stopped")
    assert admin.get(f"{ROOT}/follow-ups?filter=unplanned").context["total"] == 0
    assert admin.get(f"{ROOT}/follow-ups?filter=held").context["total"] == 3


def test_controls_and_read_projections_have_no_workflow_or_provider_effects(admin):
    pid = prospect()
    link = handoff(admin, pid)
    task(pid)
    tables = (
        "prospects",
        "clients",
        "projects",
        "tasks",
        "prospect_handoffs",
        "prospect_contact_events",
        "messages",
        "jobs",
        "emails_log",
        "proposals",
        "contracts",
        "invoices",
        "payments",
        "bookings",
    )

    def snapshot():
        return {
            t: [dict(r) for r in db.all_(f"SELECT * FROM {db.ident(t, tables)}")] for t in tables
        }

    before = snapshot()
    save(admin, pid, mode="stopped")
    for path in (
        f"{ROOT}/{pid}",
        f"{ROOT}/{pid}/outreach",
        f"{ROOT}/{pid}/drafts",
        f"{ROOT}/follow-ups?filter=held",
        "/admin/home",
        "/admin/tasks",
        "/admin/studio",
        f"/admin/studio/clients/{link['client_id']}",
        f"/admin/studio/projects/{link['project_id']}",
    ):
        assert admin.get(path).status_code == 200
    assert snapshot() == before
    assert len(audit_rows()) == 1


def test_database_rejects_invalid_channel_version_and_reason(admin):
    pid = prospect()
    for fields, args in (
        ("email_blocked", (2,)),
        ("version", (0,)),
        ("reason", ("",)),
        ("preferred_channel,email_blocked", ("email", 1)),
    ):
        columns = {"email_blocked", "version", "reason", "preferred_channel"}
        names = [db.ident(name, columns) for name in fields.split(",")]
        values = dict(
            prospect_id=pid,
            mode="active",
            version=1,
            reason="Fixture",
            last_command_digest="fixture",
        )
        values.update(zip(names, args, strict=True))
        allowed = columns | {"prospect_id", "mode", "last_command_digest"}
        with pytest.raises(sqlite3.IntegrityError), db.tx() as con:
            con.execute(
                "INSERT INTO prospect_outreach_preferences ("
                + ",".join(db.ident(k, allowed) for k in values)
                + ") VALUES ("
                + ",".join("?" for _ in values)
                + ")",
                tuple(values.values()),
            )
