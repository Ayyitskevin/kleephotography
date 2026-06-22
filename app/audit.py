"""Append-only audit log — the ONLY write path to the audit_log table.

Entity-agnostic. Exposes INSERT only; nothing here updates or deletes a row.
Always called with the caller's transaction connection (db.tx()) so the audited
write and its audit row commit together — or roll back together.
"""

import json


def log(con, entity_type, entity_id, action, *, diff=None, actor="admin"):
    """Append one audit row on the caller's open transaction connection.

    diff: optional JSON-serializable dict, e.g. {field: [old, new]} or a snapshot.
    """
    con.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, actor, diff_json) "
        "VALUES (?,?,?,?,?)",
        (
            entity_type,
            entity_id,
            action,
            actor,
            json.dumps(diff, default=str) if diff is not None else None,
        ),
    )
