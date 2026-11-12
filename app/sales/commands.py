"""Sales writes with explicit transaction ownership; no HTTP routes or providers.

``change_followup`` owns its standalone transaction. ``apply_task_change`` joins
its caller's existing transaction, keeping contact/handoff writes and audit atomic.
Callers still own authentication, signed input verification and presentation.
"""

from .. import audit, db
from . import rules


def require_prospect(con, prospect_id):
    if not con.execute("SELECT 1 FROM prospects WHERE id=?", (prospect_id,)).fetchone():
        raise rules.ActivityError("Prospect not found.", 404)


def apply_task_change(
    con, prospect_id, task_id, version, action, note, due_date="", *, allow_retry=True
):
    """Apply validated values on the caller's open ``db.tx()`` connection.

    Never begin or commit here. The standalone task command allows an exact retry;
    a new contact or handoff must pass ``allow_retry=False`` so it cannot reuse an
    earlier transition. Their own operation receipts handle full-command retries.
    """
    task = con.execute(
        "SELECT * FROM tasks WHERE id=? AND prospect_id=?", (task_id, prospect_id)
    ).fetchone()
    if task is None:
        raise rules.ActivityError("This follow-up does not belong to this prospect.", 404)
    digest = rules.command_digest(
        {
            "prospect_id": prospect_id,
            "task_id": task_id,
            "version": version,
            "action": action,
            "note": note,
            "due_date": due_date,
        }
    )
    if allow_retry and task["version"] == version + 1 and task["last_command_digest"] == digest:
        return  # Exact retry of the most recent successful command; no audit/write.
    if task["version"] != version or task["done"] or task["canceled_at"]:
        raise rules.ActivityError(
            "This action changed since the form opened. Your entries are preserved; review the current action before trying again.",
            409,
        )
    if action == "complete":
        con.execute(
            "UPDATE tasks SET done=1, done_at=datetime('now'), completion_note=?, version=version+1, last_command_digest=? WHERE id=?",
            (note, digest, task_id),
        )
    elif action == "cancel":
        con.execute(
            "UPDATE tasks SET canceled_at=datetime('now'), cancel_reason=?, version=version+1, last_command_digest=? WHERE id=?",
            (note, digest, task_id),
        )
    else:
        con.execute(
            "UPDATE tasks SET due_date=?, version=version+1, last_command_digest=? WHERE id=?",
            (due_date, digest, task_id),
        )
    audit.log(
        con,
        "prospect_task",
        task_id,
        action,
        diff={
            "prospect_id": prospect_id,
            "version": [version, version + 1],
            "note": note,
            "due_date": [task["due_date"], due_date]
            if action == "reschedule"
            else task["due_date"],
        },
    )


def change_followup(prospect_id, task_id, *, action, version, note, due_date=""):
    """Validate and commit one explicit task change with its audit record.

    This accepts form-shaped values without depending on a request. Error handling
    and the fixed local return destination remain in the HTTP adapter.
    """
    parsed_version, clean_note, clean_date = rules.task_change_values(
        action, version, note, due_date.strip()
    )
    with db.tx() as con:
        require_prospect(con, prospect_id)
        apply_task_change(con, prospect_id, task_id, parsed_version, action, clean_note, clean_date)
