"""Direct sales-command contracts against disposable SQLite, without HTTP."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import audit, config, db
from app.sales import commands, rules

pytestmark = pytest.mark.integration
DAY = "2037-04-15"
NEXT_DAY = "2037-04-20"


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sales-command.db")
    db.migrate()
    pid = db.run(
        "INSERT INTO prospects (business, source, business_key, contact_key) "
        "VALUES ('Synthetic command bistro', 'Synthetic fixture', 'command bistro', '')"
    )
    task_id = db.run(
        "INSERT INTO tasks (title,due_date,prospect_id) VALUES ('Review photography need',?,?)",
        (DAY, pid),
    )
    return pid, task_id


def state(work):
    pid, task_id = work
    return (
        dict(db.one("SELECT * FROM tasks WHERE id=?", (task_id,))),
        [
            dict(row)
            for row in db.all_(
                "SELECT * FROM audit_log WHERE entity_type='prospect_task' AND entity_id=? "
                "ORDER BY id",
                (task_id,),
            )
        ],
        dict(db.one("SELECT * FROM prospects WHERE id=?", (pid,))),
    )


def change(work, **values):
    return commands.change_followup(
        *work,
        **({"action": "complete", "version": "1", "note": "Reviewed the next step"} | values),
    )


@pytest.mark.parametrize("action", ["complete", "cancel", "reschedule"])
def test_command_commits_task_and_audit_once_without_inventing_contact(work, action):
    before = state(work)
    values = {
        "action": action,
        "note": "  Explicit operator decision  ",
        "due_date": f" {NEXT_DAY} ",
    }
    assert change(work, **values) is None
    task, audits, prospect = state(work)
    assert task["version"] == 2
    assert task["done"] == (action == "complete")
    assert bool(task["canceled_at"]) == (action == "cancel")
    assert task["due_date"] == (NEXT_DAY if action == "reschedule" else DAY)
    if action != "reschedule":
        assert (
            task["completion_note" if action == "complete" else "cancel_reason"]
            == "Explicit operator decision"
        )
    assert prospect == before[2]
    assert len(audits) == 1 and audits[0]["action"] == action
    assert json.loads(audits[0]["diff_json"]) == {
        "prospect_id": work[0],
        "version": [1, 2],
        "note": "Explicit operator decision",
        "due_date": [DAY, NEXT_DAY] if action == "reschedule" else DAY,
    }
    assert db.one("SELECT COUNT(*) n FROM prospect_contact_events")["n"] == 0
    assert change(work, **values) is None
    assert state(work) == (task, audits, prospect)


@pytest.mark.parametrize(
    "values",
    [
        {"action": "toggle"},
        {"version": "0"},
        {"note": " "},
        {"note": "x" * 1001},
        {"action": "reschedule", "due_date": "2037-02-29"},
    ],
)
def test_invalid_command_is_rejected_before_opening_a_write_transaction(work, monkeypatch, values):
    before = state(work)

    def forbidden_tx():
        raise AssertionError("Invalid input must not acquire a write transaction")

    monkeypatch.setattr(db, "tx", forbidden_tx)
    with pytest.raises(rules.ActivityError) as exc:
        change(work, **values)
    assert exc.value.status == 400
    assert state(work) == before


@pytest.mark.parametrize("target", ["missing_prospect", "missing_task", "other_prospect"])
def test_command_requires_the_current_prospect_and_its_own_task(work, target):
    before = state(work)
    pid, task_id = work
    if target == "other_prospect":
        pid = db.run(
            "INSERT INTO prospects (business,source,business_key,contact_key) "
            "VALUES ('Another prospect','Synthetic fixture','another prospect','')"
        )
    elif target == "missing_prospect":
        pid += 10000
    else:
        task_id += 10000
    with pytest.raises(rules.ActivityError) as exc:
        change((pid, task_id))
    assert exc.value.status == 404
    assert state(work) == before


def test_changed_payload_and_older_retry_cannot_overwrite_a_newer_decision(work):
    change(work, action="reschedule", due_date=NEXT_DAY)
    after = state(work)
    with pytest.raises(rules.ActivityError) as exc:
        change(work, action="reschedule", due_date="2037-05-01")
    assert exc.value.status == 409 and state(work) == after
    change(work, action="reschedule", version="2", due_date="2037-04-21")
    newer = state(work)
    with pytest.raises(rules.ActivityError) as exc:
        change(work, action="reschedule", due_date=NEXT_DAY)
    assert exc.value.status == 409 and state(work) == newer


def test_audit_failure_rolls_back_the_standalone_command(work, monkeypatch):
    before = state(work)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(audit, "log", fail_audit)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        change(work)
    assert state(work) == before


def test_composed_transition_joins_the_callers_transaction_and_rolls_back(work, monkeypatch):
    before = state(work)

    def forbidden_connection():
        raise AssertionError("A composed transition must use its caller's connection")

    with pytest.raises(RuntimeError, match="Synthetic later write failure"):
        with db.tx() as con, monkeypatch.context() as patch:
            patch.setattr(db, "connect", forbidden_connection)
            patch.setattr(db, "tx", forbidden_connection)
            commands.apply_task_change(
                con, *work, 1, "complete", "Recorded outcome", allow_retry=False
            )
            assert con.in_transaction
            assert (
                con.execute("SELECT done FROM tasks WHERE id=?", (work[1],)).fetchone()["done"] == 1
            )
            con.execute(
                "UPDATE prospects SET notes='Later work in this transaction' WHERE id=?", (work[0],)
            )
            raise RuntimeError("Synthetic later write failure")
    assert state(work) == before


def test_new_composed_operation_cannot_reuse_an_accepted_task_transition(work):
    change(work)
    after = state(work)
    with pytest.raises(rules.ActivityError) as exc:
        with db.tx() as con:
            commands.apply_task_change(
                con, *work, 1, "complete", "Reviewed the next step", allow_retry=False
            )
    assert exc.value.status == 409 and state(work) == after


@pytest.mark.parametrize("action", ["complete", "cancel", "reschedule"])
def test_existing_task_controls_remain_available_under_an_outreach_hold(work, action):
    db.run(
        "INSERT INTO prospect_outreach_preferences (prospect_id,mode,reason,version,last_command_digest) "
        "VALUES (?,'stopped','Synthetic stop',1,'synthetic-stop')",
        (work[0],),
    )
    change(work, action=action, due_date=NEXT_DAY)
    assert state(work)[0]["version"] == 2
    assert (
        db.one("SELECT mode FROM prospect_outreach_preferences WHERE prospect_id=?", (work[0],))[
            "mode"
        ]
        == "stopped"
    )
    assert db.one("SELECT COUNT(*) n FROM prospect_contact_events")["n"] == 0


@pytest.mark.parametrize("same_payload", [False, True])
def test_concurrent_commands_serialize_without_duplicate_audit_or_lost_versions(work, same_payload):
    barrier = threading.Barrier(2)

    def writer(index):
        barrier.wait(timeout=10)
        try:
            change(
                work,
                action="reschedule",
                due_date=NEXT_DAY,
                note="Same decision" if same_payload else f"Decision {index}",
            )
        except rules.ActivityError as exc:
            return exc.status
        return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(writer, (1, 2)))
    assert results == ([200, 200] if same_payload else [200, 409])
    task, audits, _ = state(work)
    assert task["version"] == 2 and task["due_date"] == NEXT_DAY
    assert len(audits) == 1
