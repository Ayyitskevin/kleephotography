import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import db, scheduling
from app.admin import scheduling as admin_scheduling
from app.main import app


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


def _event(slug: str) -> int:
    return db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        (slug, slug, 30),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("00:00", 0),
        ("9:05", 545),
        ("23:59", 1439),
        ("24:00", 1440),
    ],
)
def test_hhmm_parser_preserves_valid_boundaries(value, expected):
    assert admin_scheduling._hhmm_to_min(value) == expected


@pytest.mark.integration
def test_global_override_write_replaces_same_day_and_preserves_event_scope(admin_client):
    day = dt.date(2035, 7, 19)
    generic_id = _event("override-global-owner")
    specific_id = _event("override-specific-owner")
    specific_override_id = None
    try:
        # Legacy versions stored accepted compact dates verbatim and could also
        # accumulate canonical duplicates. One write must collapse both forms.
        db.run(
            """INSERT INTO date_overrides
               (event_type_id, day, available, start_min, end_min)
               VALUES (NULL,?,1,480,510)""",
            ("20350719",),
        )
        db.run(
            "INSERT INTO date_overrides (event_type_id, day, available) VALUES (NULL,?,0)",
            (day.isoformat(),),
        )
        specific_override_id = db.run(
            """INSERT INTO date_overrides
               (event_type_id, day, available, start_min, end_min)
               VALUES (?,?,?,?,?)""",
            (specific_id, day.isoformat(), 1, 720, 780),
        )

        # Python accepts the compact basic-ISO spelling. The admin route must
        # canonicalize it so the scheduler's YYYY-MM-DD lookup can find it.
        response = admin_client.post(
            "/admin/scheduling/override",
            data={"day": "20350719", "mode": "block"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = admin_client.post(
            "/admin/scheduling/override",
            data={
                "day": day.isoformat(),
                "mode": "hours",
                "start": "09:00",
                "end": "10:00",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        global_rows = db.all_(
            """SELECT id, day, available, start_min, end_min FROM date_overrides
               WHERE event_type_id IS NULL AND day IN (?,?) ORDER BY id""",
            (day.isoformat(), "20350719"),
        )
        assert len(global_rows) == 1
        global_row = global_rows[0]
        assert tuple(global_row)[1:] == (day.isoformat(), 1, 540, 600)
        assert db.one("SELECT id FROM date_overrides WHERE id=?", (specific_override_id,))

        audit_row = db.one(
            """SELECT entity_id, action, diff_json FROM audit_log
               WHERE entity_type='date_override' ORDER BY id DESC LIMIT 1"""
        )
        assert (audit_row["entity_id"], audit_row["action"]) == (global_row["id"], "set")
        assert json.loads(audit_row["diff_json"]) == {
            "previous": [
                {
                    "day": day.isoformat(),
                    "available": 0,
                    "start_min": None,
                    "end_min": None,
                }
            ],
            "new": {
                "day": day.isoformat(),
                "available": 1,
                "start_min": 540,
                "end_min": 600,
            },
        }

        con = db.connect()
        try:
            generic = db.one("SELECT * FROM event_types WHERE id=?", (generic_id,))
            specific = db.one("SELECT * FROM event_types WHERE id=?", (specific_id,))
            assert scheduling._windows_for_day(con, generic, day) == [(540, 600)]
            assert scheduling._windows_for_day(con, specific, day) == [(720, 780)]
        finally:
            con.close()
    finally:
        db.run("DELETE FROM date_overrides WHERE day=?", (day.isoformat(),))
        db.run("DELETE FROM date_overrides WHERE day=?", ("20350719",))
        db.run("DELETE FROM event_types WHERE id IN (?,?)", (generic_id, specific_id))


@pytest.mark.integration
def test_global_override_rejects_unknown_mode_and_invalid_clock_without_writes(admin_client):
    day = "2035-07-20"
    db.run("DELETE FROM date_overrides WHERE day=?", (day,))
    sentinel_id = db.run(
        """INSERT INTO date_overrides (event_type_id, day, available, start_min, end_min)
           VALUES (NULL,?,1,480,540)""",
        (day,),
    )
    before_audits = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='date_override'")[
        "n"
    ]
    try:
        bad_submissions = (
            {"day": day, "mode": "surprise"},
            {"day": day, "mode": "hours", "start": "09:60", "end": "11:00"},
            {"day": day, "mode": "hours", "start": "24:01", "end": "24:00"},
            {"day": day, "mode": "hours", "start": "09:5", "end": "11:00"},
            {"day": day, "mode": "hours", "start": "01:-1", "end": "11:00"},
        )
        for data in bad_submissions:
            response = admin_client.post(
                "/admin/scheduling/override", data=data, follow_redirects=False
            )
            assert response.status_code == 400
            row = db.one(
                """SELECT id, available, start_min, end_min FROM date_overrides
                   WHERE event_type_id IS NULL AND day=?""",
                (day,),
            )
            assert tuple(row) == (sentinel_id, 1, 480, 540)

        after_audits = db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='date_override'"
        )["n"]
        assert after_audits == before_audits
    finally:
        db.run("DELETE FROM date_overrides WHERE day=?", (day,))


@pytest.mark.integration
def test_global_override_write_rolls_back_when_audit_fails(admin_client, monkeypatch):
    day = "2035-07-22"
    old_id = db.run(
        "INSERT INTO date_overrides (event_type_id, day, available) VALUES (NULL,?,0)",
        (day,),
    )
    before_audits = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='date_override'")[
        "n"
    ]

    def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(admin_scheduling.audit, "log", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            admin_client.post(
                "/admin/scheduling/override",
                data={"day": day, "mode": "hours", "start": "09:00", "end": "10:00"},
                follow_redirects=False,
            )

        row = db.one(
            """SELECT id, available, start_min, end_min FROM date_overrides
               WHERE event_type_id IS NULL AND day=?""",
            (day,),
        )
        assert tuple(row) == (old_id, 0, None, None)
        after_audits = db.one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='date_override'"
        )["n"]
        assert after_audits == before_audits
    finally:
        db.run("DELETE FROM date_overrides WHERE day=?", (day,))


@pytest.mark.integration
def test_console_lists_and_deletes_global_overrides(admin_client):
    hours_day, block_day = "2035-08-04", "2035-08-05"
    try:
        response = admin_client.post(
            "/admin/scheduling/override",
            data={"day": hours_day, "mode": "hours", "start": "10:00", "end": "12:00"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = admin_client.post(
            "/admin/scheduling/override",
            data={"day": block_day, "mode": "block"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        page = admin_client.get("/admin/scheduling")
        assert page.status_code == 200
        assert "Aug 4, 2035" in page.text
        assert "Custom hours" in page.text
        assert "Aug 5, 2035" in page.text
        assert "Blocked" in page.text

        row = db.one(
            "SELECT id FROM date_overrides WHERE event_type_id IS NULL AND day=?",
            (hours_day,),
        )
        response = admin_client.post(
            f"/admin/scheduling/override/{row['id']}/delete", follow_redirects=False
        )
        assert response.status_code == 303

        page = admin_client.get("/admin/scheduling")
        assert "Aug 4, 2035" not in page.text
        assert "Aug 5, 2035" in page.text
    finally:
        db.run("DELETE FROM date_overrides WHERE day IN (?,?)", (hours_day, block_day))


@pytest.mark.integration
def test_historical_same_scope_override_uses_latest_row():
    day = dt.date(2035, 7, 21)
    event_id = _event("override-latest-wins")
    try:
        db.run(
            "INSERT INTO date_overrides (event_type_id, day, available) VALUES (NULL,?,0)",
            (day.isoformat(),),
        )
        db.run(
            """INSERT INTO date_overrides
               (event_type_id, day, available, start_min, end_min)
               VALUES (NULL,?,1,600,660)""",
            (day.isoformat(),),
        )
        event = db.one("SELECT * FROM event_types WHERE id=?", (event_id,))
        con = db.connect()
        try:
            assert scheduling._windows_for_day(con, event, day) == [(600, 660)]
        finally:
            con.close()
    finally:
        db.run("DELETE FROM date_overrides WHERE day=?", (day.isoformat(),))
        db.run("DELETE FROM event_types WHERE id=?", (event_id,))
