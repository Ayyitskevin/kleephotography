"""Anniversary nudges (revenue roadmap item 5).

What must hold: the window is bounded on both ends (no first-enable flood); a
client who already came back on their own is never nudged; the nudge goes to
Kevin's channel only; disabled alerts stamp nothing; and the cycle repeats
next year without re-pinging inside one.
"""

import time

import pytest

from app import alerts, anniversary_nudges, config, db

pytestmark = pytest.mark.integration

_STAMP = str(time.time_ns())


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM bookings WHERE name LIKE 'anniv-%'")
    db.run("DELETE FROM projects WHERE title LIKE 'anniv-%'")
    db.run("DELETE FROM clients WHERE name LIKE 'anniv-%'")


@pytest.fixture
def kevin(monkeypatch):
    """Alerts armed; every notify captured."""
    monkeypatch.setattr(alerts, "is_enabled", lambda: True)
    lines = []
    monkeypatch.setattr(alerts, "notify", lambda text: lines.append(text))
    return lines


def _client(days_since_close, name=None, title=None):
    name = name or f"anniv-{time.time_ns()}"
    db.run("INSERT INTO clients (name) VALUES (?)", (name,))
    cid = db.one("SELECT id FROM clients ORDER BY id DESC LIMIT 1")["id"]
    db.run(
        """INSERT INTO projects (client_id, title, status, created_at, stage_changed_at)
           VALUES (?,?,'project_closed', datetime('now', ?), datetime('now', ?))""",
        (
            cid,
            title or f"anniv-shoot-{name}",
            f"-{days_since_close + 30} days",
            f"-{days_since_close} days",
        ),
    )
    return cid


def _mine(lines):
    return [ln for ln in lines if "anniv-" in ln]


def test_window_is_bounded_both_ends(kevin):
    _client(config.ANNIVERSARY_NUDGE_DAYS + 10)  # inside
    _client(100, name=f"anniv-early-{_STAMP}")  # too recent
    _client(900, name=f"anniv-ancient-{_STAMP}")  # pre-history: flood guard
    anniversary_nudges.sweep()
    mine = _mine(kevin)
    assert len(mine) == 1
    assert "anniv-early" not in mine[0] and "anniv-ancient" not in mine[0]
    assert "/admin/studio/clients/" in mine[0]


def test_client_who_came_back_is_left_alone(kevin):
    cid = _client(config.ANNIVERSARY_NUDGE_DAYS + 10, name=f"anniv-back-{_STAMP}")
    # a NEWER project in any stage means they're already in the funnel
    db.run(
        """INSERT INTO projects (client_id, title, status, created_at)
           VALUES (?,?, 'inquiry_received', datetime('now', '-30 days'))""",
        (cid, f"anniv-new-{_STAMP}"),
    )
    anniversary_nudges.sweep()
    assert _mine(kevin) == []


def test_upcoming_booking_also_counts_as_back(kevin):
    cid = _client(config.ANNIVERSARY_NUDGE_DAYS + 10, name=f"anniv-booked-{_STAMP}")
    db.run("INSERT INTO event_types (slug, name) VALUES (?, 'AnnivType')", (f"anniv-{_STAMP}",))
    et = db.one("SELECT id FROM event_types WHERE slug=?", (f"anniv-{_STAMP}",))["id"]
    db.run(
        """INSERT INTO bookings (token, event_type_id, name, email, start_utc, end_utc,
                                 status, client_id)
           VALUES (?,?, 'anniv-b', 'b@x.com', datetime('now','+10 days'),
                   datetime('now','+10 days','+1 hour'), 'confirmed', ?)""",
        (f"anniv-tok-{_STAMP}", et, cid),
    )
    anniversary_nudges.sweep()
    assert _mine(kevin) == []
    db.run("DELETE FROM bookings WHERE event_type_id=?", (et,))
    db.run("DELETE FROM event_types WHERE id=?", (et,))


def test_one_shot_per_cycle_and_renudges_next_year(kevin):
    cid = _client(config.ANNIVERSARY_NUDGE_DAYS + 10)
    anniversary_nudges.sweep()
    anniversary_nudges.sweep()
    assert len(_mine(kevin)) == 1, "nudged twice in one cycle"
    # the stamp ages out -> next cycle may nudge again (the project date must
    # still be in-window for the test, so only age the stamp)
    db.run(
        "UPDATE clients SET anniversary_nudged_at=datetime('now','-400 days') WHERE id=?", (cid,)
    )
    anniversary_nudges.sweep()
    assert len(_mine(kevin)) == 2


def test_disabled_alerts_stamp_nothing(monkeypatch):
    monkeypatch.setattr(alerts, "is_enabled", lambda: False)
    cid = _client(config.ANNIVERSARY_NUDGE_DAYS + 10)
    anniversary_nudges.sweep()
    assert (
        db.one("SELECT anniversary_nudged_at FROM clients WHERE id=?", (cid,))[
            "anniversary_nudged_at"
        ]
        is None
    ), "stamped while disabled — enabling Telegram later would silently skip them"


def test_notify_failure_leaves_stamp_unset(kevin, monkeypatch):
    cid = _client(config.ANNIVERSARY_NUDGE_DAYS + 10)

    def boom(text):
        raise OSError("telegram down")

    monkeypatch.setattr(alerts, "notify", boom)
    anniversary_nudges.sweep()  # must not raise
    assert (
        db.one("SELECT anniversary_nudged_at FROM clients WHERE id=?", (cid,))[
            "anniversary_nudged_at"
        ]
        is None
    )
