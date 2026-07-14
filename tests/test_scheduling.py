import datetime as dt
from contextlib import contextmanager

import pytest

from app import db, scheduling

_UTC = dt.UTC


@contextmanager
def _event(
    *,
    slug: str,
    day: dt.date,
    start_min: int = 540,
    end_min: int = 780,
    duration_min: int = 60,
    slot_step_min: int = 60,
    min_notice_hours: int = 0,
    buffer_before_min: int = 0,
    buffer_after_min: int = 0,
    max_per_day: int = 0,
):
    eid = db.run(
        """INSERT INTO event_types
           (slug, name, duration_min, slot_step_min, min_notice_hours,
            buffer_before_min, buffer_after_min, max_per_day, booking_window_days)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            slug,
            slug,
            duration_min,
            slot_step_min,
            min_notice_hours,
            buffer_before_min,
            buffer_after_min,
            max_per_day,
            365,
        ),
    )
    db.run(
        """INSERT INTO availability_rules
           (event_type_id, weekday, start_min, end_min) VALUES (?,?,?,?)""",
        (eid, day.weekday(), start_min, end_min),
    )
    try:
        yield scheduling.event_by_slug(slug)
    finally:
        db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM availability_rules WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def _slot_strings(et, day: dt.date, ref: dt.datetime, busy=None) -> list[str]:
    con = db.connect()
    try:
        return [scheduling._fmt_utc(s) for s in scheduling._slots_utc(con, et, day, ref, busy)]
    finally:
        con.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (dt.date(2026, 3, 1), "2026-03-01 14:00:00"),
        (dt.date(2026, 3, 8), "2026-03-08 13:00:00"),
        (dt.date(2026, 11, 1), "2026-11-01 14:00:00"),
    ],
)
def test_slots_follow_new_york_dst_offsets(day, expected):
    with _event(slug=f"unit-dst-{day}", day=day, start_min=540, end_min=600) as et:
        slots = _slot_strings(et, day, dt.datetime(2026, 1, 1, tzinfo=_UTC))

    assert slots == [expected]


@pytest.mark.integration
def test_slots_enforce_minimum_notice():
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 2, 2, 13, 0, tzinfo=_UTC)  # 8:00 AM EST
    with _event(
        slug="unit-min-notice",
        day=day,
        start_min=540,
        end_min=720,
        min_notice_hours=2,
    ) as et:
        slots = _slot_strings(et, day, ref)

    assert slots == ["2026-02-02 15:00:00", "2026-02-02 16:00:00"]


@pytest.mark.integration
def test_slots_apply_buffers_on_both_bookings():
    day = dt.date(2026, 2, 2)
    with _event(
        slug="unit-buffers",
        day=day,
        buffer_before_min=30,
        buffer_after_min=30,
    ) as et:
        db.run(
            """INSERT INTO bookings
               (token, event_type_id, name, email, start_utc, end_utc)
               VALUES (?,?,?,?,?,?)""",
            (
                "UnitBufferBooking",
                et["id"],
                "Buffer",
                "buffer@example.com",
                "2026-02-02 15:00:00",
                "2026-02-02 16:00:00",
            ),
        )
        slots = _slot_strings(et, day, dt.datetime(2026, 1, 1, tzinfo=_UTC))

    assert slots == ["2026-02-02 17:00:00"]


@pytest.mark.integration
def test_slots_stop_when_max_per_day_is_reached():
    day = dt.date(2026, 2, 2)
    with _event(slug="unit-day-cap", day=day, max_per_day=1) as et:
        db.run(
            """INSERT INTO bookings
               (token, event_type_id, name, email, start_utc, end_utc)
               VALUES (?,?,?,?,?,?)""",
            (
                "UnitCappedBooking",
                et["id"],
                "Cap",
                "cap@example.com",
                "2026-02-02 14:00:00",
                "2026-02-02 15:00:00",
            ),
        )
        slots = _slot_strings(et, day, dt.datetime(2026, 1, 1, tzinfo=_UTC))

    assert slots == []


@pytest.mark.integration
def test_slots_hide_mocked_google_busy_intervals(monkeypatch):
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    busy = [
        (dt.datetime(2026, 2, 2, 15, 30, tzinfo=_UTC), dt.datetime(2026, 2, 2, 16, 30, tzinfo=_UTC))
    ]
    calls = []

    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: calls.append((start, end)) or busy,
    )
    with _event(slug="unit-google-busy", day=day) as et:
        slots = scheduling.slots_for_day(et, day)

    assert [slot["utc"] for slot in slots] == [
        "2026-02-02 14:00:00",
        "2026-02-02 17:00:00",
    ]
    assert len(calls) == 1
