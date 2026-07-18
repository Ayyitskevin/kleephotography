import datetime as dt
import sqlite3
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app import booking_notify, db, gcal, notion_sync, scheduling
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
        original = db.one(
            "SELECT status, cancel_reason, cancelled_at FROM bookings WHERE id=?", (old_id,)
        )
        assert original["status"] == "cancelled"
        assert original["cancel_reason"] == "Rescheduled"
        assert original["cancelled_at"] is not None
        assert (
            db.one(
                "SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=? AND status='confirmed'",
                (et["id"],),
            )["n"]
            == 1
        )


@pytest.mark.integration
def test_reschedule_atomically_preserves_owned_state_and_updates_local_dates(monkeypatch):
    old_day = dt.date(2026, 1, 26)
    day = dt.date(2026, 2, 2)
    ref = dt.datetime(2026, 1, 1, tzinfo=_UTC)
    monkeypatch.setattr(scheduling, "now_utc", lambda: ref)
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    client_id = project_id = inquiry_id = None
    try:
        with _event(
            slug="reschedule-state",
            day=day,
            start_min=1200,
            end_min=1320,
            max_per_day=1,
        ) as et:
            client_id = db.run(
                "INSERT INTO clients (name, email) VALUES (?,?)",
                ("State Client", "state@example.com"),
            )
            project_id = db.run(
                "INSERT INTO projects (client_id, title, shoot_date) VALUES (?,?,?)",
                (client_id, "State Project", old_day.isoformat()),
            )
            inquiry_id = db.run(
                """INSERT INTO inquiries (name, email, message, kind, shoot_date, service)
                   VALUES (?,?,?,?,?,?)""",
                (
                    "State Client",
                    "state@example.com",
                    "Original booking inquiry",
                    "booking",
                    old_day.isoformat(),
                    et["name"],
                ),
            )
            old_id = _confirmed_booking(et, "RescheduleStateOriginal", "2026-01-27 01:00:00")
            db.run(
                """UPDATE bookings
                      SET phone=?, notes=?, inquiry_id=?, google_event_id=?,
                          notion_page_id=?, notion_session_id=?, client_id=?,
                          project_id=?, venue_address=?, dish_count=?,
                          parking_notes=?, style_refs=?, onsite_contact=?,
                          reminded_48h=1, reminded_24h=1, armed_postshoot=1
                    WHERE id=?""",
                (
                    "555-0100",
                    "Keep the blue backdrop",
                    inquiry_id,
                    "google-original",
                    "notion-booking-original",
                    "notion-session-original",
                    client_id,
                    project_id,
                    "Studio 12",
                    "8 dishes",
                    "Loading dock",
                    "Warm editorial",
                    "Morgan",
                    old_id,
                ),
            )

            new_id, _token = scheduling.book(
                et,
                "2026-02-03 01:00:00",
                "Ignored replacement name",
                "ignored@example.com",
                "",
                "",
                "America/Los_Angeles",
                exclude_id=old_id,
            )

            old = db.one("SELECT * FROM bookings WHERE id=?", (old_id,))
            new = db.one("SELECT * FROM bookings WHERE id=?", (new_id,))
            assert old["status"] == "cancelled"
            assert old["google_event_id"] is None
            assert old["notion_page_id"] is None
            assert new["status"] == "confirmed"
            assert new["reschedule_of"] == old_id
            assert new["name"] == "Existing client"
            assert new["email"] == "existing@example.com"
            assert new["phone"] == "555-0100"
            assert new["notes"] == "Keep the blue backdrop"
            assert new["tz"] == "America/Los_Angeles"
            assert new["reminded_48h"] == 0
            assert new["reminded_24h"] == 0
            assert new["armed_postshoot"] == 0
            for field in (
                "inquiry_id",
                "notion_session_id",
                "client_id",
                "project_id",
                "venue_address",
                "dish_count",
                "parking_notes",
                "style_refs",
                "onsite_contact",
            ):
                assert new[field] == old[field]
            assert new["google_event_id"] == "google-original"
            assert new["notion_page_id"] == "notion-booking-original"
            assert (
                db.one("SELECT shoot_date FROM inquiries WHERE id=?", (inquiry_id,))["shoot_date"]
                == day.isoformat()
            )
            assert (
                db.one("SELECT shoot_date FROM projects WHERE id=?", (project_id,))["shoot_date"]
                == day.isoformat()
            )

            calls = []
            monkeypatch.setattr(gcal, "configured", lambda: True)
            monkeypatch.setattr(gcal, "is_connected", lambda: True)
            monkeypatch.setattr(gcal.config, "GOOGLE_CALENDAR_ID", "calendar")
            monkeypatch.setattr(
                gcal,
                "_api",
                lambda method, path, body=None: calls.append((method, path, body)) or {},
            )
            gcal.on_booking_confirmed(new_id)
            assert [(method, path) for method, path, _body in calls] == [
                ("PATCH", "/calendars/calendar/events/google-original")
            ]
            assert calls[0][2]["start"] == {"dateTime": "2026-02-03T01:00:00Z"}
            assert calls[0][2]["end"] == {"dateTime": "2026-02-03T02:00:00Z"}

            calls.clear()
            gcal.on_booking_confirmed(old_id)
            assert calls == []

            def stale_api(method, path, body=None):
                calls.append((method, path, body))
                if method == "PATCH":
                    raise urllib.error.HTTPError(path, 404, "gone", {}, None)
                return {"id": "google-recreated"}

            monkeypatch.setattr(gcal, "_api", stale_api)
            gcal.on_booking_confirmed(new_id)
            assert [(method, path) for method, path, _body in calls] == [
                ("PATCH", "/calendars/calendar/events/google-original"),
                ("POST", "/calendars/calendar/events"),
            ]
            assert (
                db.one("SELECT google_event_id FROM bookings WHERE id=?", (new_id,))[
                    "google_event_id"
                ]
                == "google-recreated"
            )
            calls.clear()

            def transient_api(method, path, body=None):
                calls.append((method, path, body))
                raise urllib.error.HTTPError(path, 502, "upstream unavailable", {}, None)

            monkeypatch.setattr(gcal, "_api", transient_api)
            gcal.on_booking_confirmed(new_id)
            assert [(method, path) for method, path, _body in calls] == [
                ("PATCH", "/calendars/calendar/events/google-recreated")
            ]
            assert (
                db.one("SELECT google_event_id FROM bookings WHERE id=?", (new_id,))[
                    "google_event_id"
                ]
                == "google-recreated"
            )

            patched = []
            created = []
            monkeypatch.setattr(notion_sync.config, "NOTION_TOKEN", "token")
            monkeypatch.setattr(notion_sync.config, "NOTION_BOOKINGS_DB", "bookings-db")
            monkeypatch.setattr(
                notion_sync,
                "_patch_page",
                lambda page_id, props: patched.append((page_id, props)),
            )
            monkeypatch.setattr(
                notion_sync,
                "_create_page",
                lambda *args: created.append(args) or "unexpected-page",
            )
            notion_sync.sync_booking(new_id)
            assert created == []
            assert len(patched) == 1
            page_id, properties = patched[0]
            assert page_id == "notion-booking-original"
            assert properties["Status"]["select"]["name"] == "Confirmed"
            assert properties["When"]["date"] == {
                "start": "2026-02-03T01:00:00Z",
                "end": "2026-02-03T02:00:00Z",
            }
    finally:
        if inquiry_id is not None:
            db.run("DELETE FROM inquiries WHERE id=?", (inquiry_id,))
        if project_id is not None:
            db.run("DELETE FROM projects WHERE id=?", (project_id,))
        if client_id is not None:
            db.run("DELETE FROM clients WHERE id=?", (client_id,))


@pytest.mark.integration
def test_reschedule_release_failure_rolls_back_the_replacement(monkeypatch):
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-rollback", day=day, max_per_day=1) as et:
        old_id = _confirmed_booking(et, "RescheduleRollbackOriginal", "2026-02-02 14:00:00")
        db.run(
            "UPDATE bookings SET google_event_id=?, notion_page_id=? WHERE id=?",
            ("google-rollback", "notion-rollback", old_id),
        )
        con = db.connect()
        try:
            con.executescript(
                """CREATE TRIGGER test_reschedule_release_abort
                   BEFORE UPDATE OF status ON bookings
                   WHEN OLD.token='RescheduleRollbackOriginal'
                        AND NEW.status='cancelled'
                   BEGIN
                     SELECT RAISE(ABORT, 'forced reschedule release failure');
                   END;"""
            )
            con.commit()
        finally:
            con.close()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="forced reschedule release failure"):
                scheduling.book(
                    et,
                    "2026-02-02 15:00:00",
                    "Ada",
                    "ada@example.com",
                    "",
                    "",
                    "UTC",
                    exclude_id=old_id,
                )
        finally:
            db.run("DROP TRIGGER test_reschedule_release_abort")

        rows = db.all_(
            """SELECT id, status, google_event_id, notion_page_id
                 FROM bookings WHERE event_type_id=? ORDER BY id""",
            (et["id"],),
        )
        assert [(row["id"], row["status"]) for row in rows] == [(old_id, "confirmed")]
        assert rows[0]["google_event_id"] == "google-rollback"
        assert rows[0]["notion_page_id"] == "notion-rollback"


@pytest.mark.integration
def test_reschedule_linked_date_failure_rolls_back_prior_booking_writes(monkeypatch):
    old_day = dt.date(2026, 1, 26)
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    client_id = project_id = inquiry_id = None
    try:
        with _event(slug="reschedule-linked-rollback", day=day, max_per_day=1) as et:
            client_id = db.run(
                "INSERT INTO clients (name, email) VALUES (?,?)",
                ("Rollback Client", "rollback@example.com"),
            )
            project_id = db.run(
                "INSERT INTO projects (client_id, title, shoot_date) VALUES (?,?,?)",
                (client_id, "Rollback Project", old_day.isoformat()),
            )
            inquiry_id = db.run(
                """INSERT INTO inquiries (name, email, message, kind, shoot_date, service)
                   VALUES (?,?,?,?,?,?)""",
                (
                    "Rollback Client",
                    "rollback@example.com",
                    "Original inquiry",
                    "booking",
                    old_day.isoformat(),
                    et["name"],
                ),
            )
            old_id = _confirmed_booking(
                et, "RescheduleLinkedRollbackOriginal", "2026-01-26 14:00:00"
            )
            db.run(
                "UPDATE bookings SET inquiry_id=?, project_id=? WHERE id=?",
                (inquiry_id, project_id, old_id),
            )
            con = db.connect()
            try:
                con.executescript(
                    f"""CREATE TRIGGER test_reschedule_linked_date_abort
                        BEFORE UPDATE OF shoot_date ON projects
                        WHEN OLD.id={project_id}
                        BEGIN
                          SELECT RAISE(ABORT, 'forced linked-state failure');
                        END;"""
                )
                con.commit()
            finally:
                con.close()
            try:
                with pytest.raises(sqlite3.IntegrityError, match="forced linked-state failure"):
                    scheduling.book(
                        et,
                        "2026-02-02 15:00:00",
                        "Ada",
                        "ada@example.com",
                        "",
                        "",
                        "UTC",
                        exclude_id=old_id,
                    )
            finally:
                db.run("DROP TRIGGER test_reschedule_linked_date_abort")

            rows = db.all_(
                """SELECT id, status, cancel_reason FROM bookings
                   WHERE event_type_id=? ORDER BY id""",
                (et["id"],),
            )
            assert [(row["id"], row["status"], row["cancel_reason"]) for row in rows] == [
                (old_id, "confirmed", "")
            ]
            assert (
                db.one("SELECT shoot_date FROM inquiries WHERE id=?", (inquiry_id,))["shoot_date"]
                == old_day.isoformat()
            )
            assert (
                db.one("SELECT shoot_date FROM projects WHERE id=?", (project_id,))["shoot_date"]
                == old_day.isoformat()
            )
    finally:
        if inquiry_id is not None:
            db.run("DELETE FROM inquiries WHERE id=?", (inquiry_id,))
        if project_id is not None:
            db.run("DELETE FROM projects WHERE id=?", (project_id,))
        if client_id is not None:
            db.run("DELETE FROM clients WHERE id=?", (client_id,))


@pytest.mark.integration
def test_reschedule_rejects_a_stale_original_even_when_target_is_open(monkeypatch):
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug="reschedule-stale", day=day) as et:
        old_id = _confirmed_booking(et, "RescheduleStaleOriginal", "2026-02-02 14:00:00")
        db.run("UPDATE bookings SET status='cancelled' WHERE id=?", (old_id,))

        with pytest.raises(scheduling.SlotTaken, match="original booking"):
            scheduling.book(
                et,
                "2026-02-02 15:00:00",
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=old_id,
            )
        assert (
            db.one("SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=?", (et["id"],))["n"]
            == 1
        )


@pytest.mark.integration
def test_concurrent_reschedules_leave_one_confirmed_leaf(monkeypatch):
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    barrier = threading.Barrier(2)

    def free_busy(_start, _end):
        barrier.wait(timeout=5)
        return FreeBusyQuery(intervals=[], unavailable=False)

    monkeypatch.setattr(scheduling.gcal, "free_busy", free_busy)

    with _event(slug="reschedule-concurrent", day=day) as et:
        old_id = _confirmed_booking(et, "RescheduleConcurrentOriginal", "2026-02-02 14:00:00")

        def attempt(start):
            try:
                return (
                    "ok",
                    scheduling.book(
                        et,
                        start,
                        "Ada",
                        "ada@example.com",
                        "",
                        "",
                        "UTC",
                        exclude_id=old_id,
                    )[0],
                )
            except scheduling.SlotTaken:
                return ("taken", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("2026-02-02 15:00:00", "2026-02-02 16:00:00")))

        assert sorted(status for status, _booking_id in results) == ["ok", "taken"]
        rows = db.all_(
            "SELECT id, status, reschedule_of FROM bookings WHERE event_type_id=? ORDER BY id",
            (et["id"],),
        )
        assert sum(row["status"] == "confirmed" for row in rows) == 1
        assert len(rows) == 2
        assert rows[0]["id"] == old_id and rows[0]["status"] == "cancelled"
        assert rows[1]["reschedule_of"] == old_id


@pytest.mark.integration
def test_concurrent_distinct_reschedules_claim_the_target_once(monkeypatch):
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    barrier = threading.Barrier(2)

    def free_busy(_start, _end):
        barrier.wait(timeout=5)
        return FreeBusyQuery(intervals=[], unavailable=False)

    monkeypatch.setattr(scheduling.gcal, "free_busy", free_busy)

    with _event(slug="reschedule-concurrent-target", day=day) as et:
        first_id = _confirmed_booking(et, "ConcurrentTargetFirst", "2026-02-02 14:00:00")
        second_id = _confirmed_booking(et, "ConcurrentTargetSecond", "2026-02-02 15:00:00")

        def attempt(original_id):
            try:
                new_id, _token = scheduling.book(
                    et,
                    "2026-02-02 16:00:00",
                    "Ada",
                    "ada@example.com",
                    "",
                    "",
                    "UTC",
                    exclude_id=original_id,
                )
                return ("ok", new_id)
            except scheduling.SlotTaken:
                return ("taken", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (first_id, second_id)))

        assert sorted(status for status, _booking_id in results) == ["ok", "taken"]
        rows = db.all_(
            """SELECT id, status, start_utc, reschedule_of FROM bookings
               WHERE event_type_id=? ORDER BY id""",
            (et["id"],),
        )
        assert len(rows) == 3
        assert sum(row["status"] == "confirmed" for row in rows) == 2
        target_rows = [row for row in rows if row["start_utc"] == "2026-02-02 16:00:00"]
        assert len(target_rows) == 1
        assert target_rows[0]["status"] == "confirmed"
        assert target_rows[0]["reschedule_of"] in {first_id, second_id}
        original_statuses = {
            row["id"]: row["status"] for row in rows if row["id"] in {first_id, second_id}
        }
        assert sorted(original_statuses.values()) == ["cancelled", "confirmed"]


@pytest.mark.integration
@pytest.mark.parametrize("selected", ["parent", "child"])
def test_reschedule_rejects_a_preexisting_split_lineage(monkeypatch, selected):
    day = dt.date(2026, 2, 2)
    monkeypatch.setattr(scheduling, "now_utc", lambda: dt.datetime(2026, 1, 1, tzinfo=_UTC))
    monkeypatch.setattr(
        scheduling.gcal,
        "free_busy",
        lambda start, end: FreeBusyQuery(intervals=[], unavailable=False),
    )

    with _event(slug=f"reschedule-split-{selected}", day=day) as et:
        parent_id = _confirmed_booking(et, f"SplitParent-{selected}", "2026-02-02 14:00:00")
        child_id = db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, reschedule_of)
               VALUES (?,?,?,?,?,?,?)""",
            (
                f"SplitChild-{selected}",
                et["id"],
                "Existing client",
                "existing@example.com",
                "2026-02-02 15:00:00",
                "2026-02-02 16:00:00",
                parent_id,
            ),
        )
        selected_id = parent_id if selected == "parent" else child_id

        with pytest.raises(scheduling.SlotTaken, match="lineage"):
            scheduling.book(
                et,
                "2026-02-02 16:00:00",
                "Ada",
                "ada@example.com",
                "",
                "",
                "UTC",
                exclude_id=selected_id,
            )

        rows = db.all_(
            "SELECT id, status FROM bookings WHERE event_type_id=? ORDER BY id", (et["id"],)
        )
        assert [(row["id"], row["status"]) for row in rows] == [
            (parent_id, "confirmed"),
            (child_id, "confirmed"),
        ]


@pytest.mark.integration
@pytest.mark.parametrize("selected", ["parent", "child"])
def test_cancel_rejects_a_preexisting_split_lineage(selected):
    day = dt.date(2026, 2, 2)
    with _event(slug=f"cancel-split-{selected}", day=day) as et:
        parent_token = f"CancelSplitParent-{selected}"
        child_token = f"CancelSplitChild-{selected}"
        parent_id = _confirmed_booking(et, parent_token, "2026-02-02 14:00:00")
        child_id = db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, reschedule_of)
               VALUES (?,?,?,?,?,?,?)""",
            (
                child_token,
                et["id"],
                "Existing client",
                "existing@example.com",
                "2026-02-02 15:00:00",
                "2026-02-02 16:00:00",
                parent_id,
            ),
        )
        selected_token = parent_token if selected == "parent" else child_token

        assert scheduling.cancel(selected_token, "Client request") is False
        rows = db.all_(
            "SELECT id, status FROM bookings WHERE event_type_id=? ORDER BY id", (et["id"],)
        )
        assert [(row["id"], row["status"]) for row in rows] == [
            (parent_id, "confirmed"),
            (child_id, "confirmed"),
        ]


@pytest.mark.integration
def test_calendar_identity_is_stable_across_reschedule_chain(monkeypatch):
    day = dt.date(2026, 2, 2)
    with _event(slug="reschedule-calendar-lineage", day=day) as et:
        first_id = _confirmed_booking(et, "CalendarLineage1", "2026-02-02 14:00:00")
        second_id = db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, reschedule_of)
               VALUES (?,?,?,?,?,?,?)""",
            (
                "CalendarLineage2",
                et["id"],
                "Existing client",
                "existing@example.com",
                "2026-02-02 15:00:00",
                "2026-02-02 16:00:00",
                first_id,
            ),
        )
        third_id = db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, reschedule_of)
               VALUES (?,?,?,?,?,?,?)""",
            (
                "CalendarLineage3",
                et["id"],
                "Existing client",
                "existing@example.com",
                "2026-02-02 16:00:00",
                "2026-02-02 17:00:00",
                second_id,
            ),
        )

        assert booking_notify.calendar_identity(first_id) == (
            f"mise-booking-{first_id}@kleephotography.com",
            0,
        )
        assert booking_notify.calendar_identity(second_id) == (
            f"mise-booking-{first_id}@kleephotography.com",
            1,
        )
        assert booking_notify.calendar_identity(third_id) == (
            f"mise-booking-{first_id}@kleephotography.com",
            2,
        )
        db.run(
            """UPDATE bookings SET status='cancelled', cancel_reason='Rescheduled'
               WHERE id IN (?,?)""",
            (first_id, second_id),
        )
        db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, status,
                  reschedule_of, cancel_reason)
               VALUES (?,?,?,?,?,?,'cancelled',?,'Reschedule failed — replacement rolled back')""",
            (
                "CalendarLineageCompensated",
                et["id"],
                "Existing client",
                "existing@example.com",
                "2026-02-02 17:00:00",
                "2026-02-02 18:00:00",
                first_id,
            ),
        )
        assert scheduling.has_confirmed_replacement(first_id) is True
        assert scheduling.has_confirmed_replacement(second_id) is True
        assert scheduling.has_confirmed_replacement(third_id) is False

        db.run(
            """UPDATE bookings SET status='cancelled', cancel_reason='Client request'
               WHERE id=?""",
            (third_id,),
        )
        sent = []
        monkeypatch.setattr(booking_notify.mailer, "configured", lambda: True)
        monkeypatch.setattr(
            booking_notify.mailer,
            "send",
            lambda *args, **kwargs: sent.append((args, kwargs)),
        )
        monkeypatch.setattr(booking_notify.notion_sync, "sync_booking", lambda *args: None)
        monkeypatch.setattr(booking_notify.gcal, "on_booking_cancelled", lambda *args: None)

        booking_notify.cancelled(third_id, by_admin=True)

        assert len(sent) == 1
        cancel_invite = sent[0][1]["ics"]["content"]
        assert f"UID:mise-booking-{first_id}@kleephotography.com" in cancel_invite
        assert "SEQUENCE:3" in cancel_invite
        assert "METHOD:CANCEL" in cancel_invite
        assert "STATUS:CANCELLED" in cancel_invite


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
