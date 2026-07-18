import datetime as dt
from contextlib import contextmanager

import pytest

from app import db, gcal, scheduling
from app.gcal import FreeBusyQuery

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
    booking_window_days: int = 365,
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
            booking_window_days,
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


def _confirmed_booking(et, token: str, start: str) -> int:
    start_utc = scheduling._parse_utc(start)
    end_utc = start_utc + dt.timedelta(minutes=et["duration_min"])
    return db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, start_utc, end_utc)
           VALUES (?,?,?,?,?,?)""",
        (
            token,
            et["id"],
            "Existing client",
            "existing@example.com",
            start,
            scheduling._fmt_utc(end_utc),
        ),
    )


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
        lambda start, end: (
            calls.append((start, end)) or FreeBusyQuery(intervals=busy, unavailable=False)
        ),
    )
    with _event(slug="unit-google-busy", day=day) as et:
        slots = scheduling.slots_for_day(et, day)

    assert [slot["utc"] for slot in slots] == [
        "2026-02-02 14:00:00",
        "2026-02-02 17:00:00",
    ]
    assert len(calls) == 1


@pytest.mark.integration
def test_slots_fail_open_when_freebusy_errors(monkeypatch):
    day = dt.date(2026, 2, 3)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=None, unavailable=False),
    )
    with _event(slug="unit-gcal-open", day=day) as et:
        slots = scheduling.slots_for_day(et, day)
    assert [slot["utc"] for slot in slots] == [
        "2026-02-03 14:00:00",
        "2026-02-03 15:00:00",
        "2026-02-03 16:00:00",
        "2026-02-03 17:00:00",
    ]


@pytest.mark.integration
def test_slots_fail_closed_when_freebusy_unavailable(monkeypatch):
    day = dt.date(2026, 2, 4)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=None, unavailable=True),
    )
    with _event(slug="unit-gcal-strict", day=day) as et:
        assert scheduling.slots_for_day(et, day) == []
        assert scheduling.days_with_slots(et, day, 1) == set()


@pytest.mark.integration
def test_book_rejects_google_busy_slot(monkeypatch):
    day = dt.date(2026, 2, 5)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    busy = [
        (dt.datetime(2026, 2, 5, 14, 0, tzinfo=_UTC), dt.datetime(2026, 2, 5, 15, 0, tzinfo=_UTC))
    ]
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=busy, unavailable=False),
    )
    with _event(slug="unit-book-busy", day=day) as et:
        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(et, "2026-02-05 14:00:00", "Ada", "ada@example.com", "", "", "UTC")
        bid, _token = scheduling.book(
            et, "2026-02-05 15:00:00", "Ada", "ada@example.com", "", "", "UTC"
        )
        assert bid


@pytest.mark.integration
def test_book_raises_when_calendar_unavailable(monkeypatch):
    day = dt.date(2026, 2, 6)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=None, unavailable=True),
    )
    with _event(slug="unit-book-unavail", day=day) as et:
        with pytest.raises(scheduling.CalendarUnavailable):
            scheduling.book(et, "2026-02-06 14:00:00", "Ada", "ada@example.com", "", "", "UTC")


@pytest.mark.integration
def test_reschedule_availability_excludes_only_original_booking(monkeypatch):
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-original", day=day, max_per_day=1) as et:
        old_id = _confirmed_booking(et, "RescheduleOriginal", "2026-02-02 14:00:00")

        slots = scheduling.slots_for_day(et, day, exclude_id=old_id)
        assert [slot["utc"] for slot in slots] == [
            "2026-02-02 15:00:00",
            "2026-02-02 16:00:00",
            "2026-02-02 17:00:00",
        ]
        assert scheduling.days_with_slots(et, day, 1, exclude_id=old_id) == {day.isoformat()}

        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(
                et,
                "2026-02-02 14:00:00",
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=old_id,
            )

        new_id, _token = scheduling.book(
            et,
            "2026-02-02 15:00:00",
            "Ada",
            "ada@example.com",
            "",
            "",
            "UTC",
            exclude_id=old_id,
        )
        replacement = db.one("SELECT reschedule_of FROM bookings WHERE id=?", (new_id,))
        assert replacement["reschedule_of"] == old_id


@pytest.mark.integration
@pytest.mark.parametrize(
    ("slug", "day", "ref", "candidate", "event_options", "busy"),
    [
        (
            "reschedule-off-grid",
            dt.date(2026, 2, 2),
            dt.datetime(2026, 1, 1, tzinfo=_UTC),
            "2026-02-02 14:30:00",
            {},
            [],
        ),
        (
            "reschedule-notice",
            dt.date(2026, 2, 2),
            dt.datetime(2026, 2, 2, 13, 0, tzinfo=_UTC),
            "2026-02-02 14:00:00",
            {"min_notice_hours": 2},
            [],
        ),
        (
            "reschedule-horizon",
            dt.date(2026, 2, 9),
            dt.datetime(2026, 2, 1, 13, 0, tzinfo=_UTC),
            "2026-02-09 14:00:00",
            {"booking_window_days": 3},
            [],
        ),
        (
            "reschedule-google-busy",
            dt.date(2026, 2, 2),
            dt.datetime(2026, 1, 1, tzinfo=_UTC),
            "2026-02-02 14:00:00",
            {},
            [
                (
                    dt.datetime(2026, 2, 2, 14, 0, tzinfo=_UTC),
                    dt.datetime(2026, 2, 2, 15, 0, tzinfo=_UTC),
                )
            ],
        ),
    ],
)
def test_reschedule_rejects_starts_outside_canonical_policy(
    monkeypatch, slug, day, ref, candidate, event_options, busy
):
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=busy, unavailable=False),
    )

    with _event(slug=slug, day=day, **event_options) as et:
        old_id = _confirmed_booking(et, f"{slug}-original", f"{day.isoformat()} 17:00:00")

        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(
                et,
                candidate,
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=old_id,
            )


@pytest.mark.integration
def test_reschedule_keeps_target_day_cap_and_other_bookings_authoritative(monkeypatch):
    original_day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-cap", day=original_day, max_per_day=1) as et:
        old_id = _confirmed_booking(et, "RescheduleCapOriginal", "2026-02-02 14:00:00")
        _confirmed_booking(et, "RescheduleCapOther", "2026-02-09 14:00:00")

        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(
                et,
                "2026-02-09 15:00:00",
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=old_id,
            )


@pytest.mark.integration
def test_reschedule_exclusion_preserves_cross_event_and_buffer_conflicts(monkeypatch):
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-overlap", day=day, buffer_before_min=30) as et:
        old_id = _confirmed_booking(et, "RescheduleOverlapOriginal", "2026-02-02 17:00:00")
        with _event(slug="reschedule-conflict-other", day=day) as other:
            other_id = _confirmed_booking(other, "RescheduleOverlapOther", "2026-02-02 14:00:00")

            with pytest.raises(scheduling.SlotTaken):
                scheduling.book(
                    et,
                    "2026-02-02 14:00:00",
                    "Ada",
                    "ada@example.com",
                    "",
                    "",
                    "UTC",
                    exclude_id=other_id,
                )

            for candidate in ("2026-02-02 14:00:00", "2026-02-02 15:00:00"):
                with pytest.raises(scheduling.SlotTaken):
                    scheduling.book(
                        et,
                        candidate,
                        "Ada",
                        "ada@example.com",
                        "",
                        "",
                        "UTC",
                        exclude_id=old_id,
                    )


@pytest.mark.integration
def test_reschedule_rejects_blocked_date_override(monkeypatch):
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-blocked-date", day=day) as et:
        old_id = _confirmed_booking(et, "RescheduleBlockedOriginal", "2026-02-02 17:00:00")
        db.run(
            """INSERT INTO date_overrides (event_type_id, day, available)
               VALUES (?,?,0)""",
            (et["id"], day.isoformat()),
        )

        with pytest.raises(scheduling.SlotTaken):
            scheduling.book(
                et,
                "2026-02-02 14:00:00",
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=old_id,
            )


@pytest.mark.unit
def test_free_busy_strict_marks_unavailable(monkeypatch):
    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(gcal, "is_connected", lambda: True)
    monkeypatch.setattr(gcal.config, "GCAL_AVAILABILITY_STRICT", True)
    monkeypatch.setattr(
        gcal,
        "_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    start = dt.datetime(2026, 2, 1, tzinfo=_UTC)
    fb = gcal.free_busy(start, start + dt.timedelta(days=1))
    assert fb.unavailable is True
    assert fb.intervals is None


@pytest.mark.unit
def test_free_busy_fail_open_by_default(monkeypatch):
    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(gcal, "is_connected", lambda: True)
    monkeypatch.setattr(gcal.config, "GCAL_AVAILABILITY_STRICT", False)
    monkeypatch.setattr(
        gcal,
        "_api",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    start = dt.datetime(2026, 2, 1, tzinfo=_UTC)
    fb = gcal.free_busy(start, start + dt.timedelta(days=1))
    assert fb.unavailable is False
    assert fb.intervals is None
