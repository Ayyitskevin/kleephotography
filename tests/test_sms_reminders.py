"""T-24h SMS booking reminders (Cal.com-parity item 1).

What must hold: the text fires once inside the 0-24h window and only for
confirmed bookings with a phone number; SMS is an independent channel (email
down does not block it, and vice versa); a dormant Quo stamps nothing so arming
it later still catches open windows; a failed send leaves the flag unset so the
next sweep retries. Recipient assertions are scoped to this file's own bookings
— the test DB is shared."""

import datetime as dt

import pytest

from app import booking_reminders, db, mailer, sms

pytestmark = pytest.mark.integration

_NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


class _FrozenDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz else _NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(booking_reminders.dt, "datetime", _FrozenDateTime)
    yield
    db.run("DELETE FROM bookings WHERE token LIKE 'smsrem-%'")
    db.run("DELETE FROM event_types WHERE slug LIKE 'smsrem-%'")


@pytest.fixture
def texts(monkeypatch):
    """Quo armed; every send captured as (to, body)."""
    sent = []
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send", lambda to, body: sent.append((to, body)) or "msg-id")
    return sent


@pytest.fixture
def emails(monkeypatch):
    """Mailer armed; every send captured."""
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: sent.append((a, kw)))
    return sent


def _booking(hours_out, phone="+15550001111", status="confirmed", token=None):
    token = token or f"smsrem-{hours_out}-{phone or 'nophone'}"
    phone = phone or ""  # bookings.phone is NOT NULL; blank means "none given"
    eid = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        (f"smsrem-{token}", "Mini Session", 60),
    )
    start = _NOW + dt.timedelta(hours=hours_out)
    return db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, phone, start_utc, end_utc, status, tz)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            token,
            eid,
            "Sam",
            "sam@example.com",
            phone,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            (start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            status,
            "America/New_York",
        ),
    )


def _flag(bid):
    return db.one("SELECT reminded_sms FROM bookings WHERE id=?", (bid,))["reminded_sms"]


def _mine(sent):
    return [(to, body) for to, body in sent if "smsrem-" in body]


def test_sms_sent_once_inside_24h_window(texts):
    bid = _booking(12)
    booking_reminders.sweep()
    booking_reminders.sweep()
    mine = _mine(texts)
    assert len(mine) == 1
    to, body = mine[0]
    assert to == "+15550001111"
    assert "Mini Session" in body and "/booking/smsrem-" in body
    assert _flag(bid) == 1


def test_no_sms_outside_window_or_off_status(texts):
    _booking(36)  # 48h-email territory, too early for the text
    _booking(-2, token="smsrem-past")  # already started
    _booking(12, status="cancelled", token="smsrem-cancelled")
    _booking(12, status="pending_payment", token="smsrem-pending")
    booking_reminders.sweep()
    assert _mine(texts) == []


def test_no_phone_no_sms(texts):
    bid = _booking(12, phone=None)
    booking_reminders.sweep()
    assert _mine(texts) == []
    assert _flag(bid) == 0  # never stamped without a send


def test_dormant_quo_stamps_nothing_then_arming_catches(monkeypatch, emails):
    bid = _booking(12)
    booking_reminders.sweep()  # email goes out, SMS dormant
    assert _flag(bid) == 0
    assert any("sam@example.com" in a[0] for a, _ in emails)
    sent = []
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send", lambda to, body: sent.append((to, body)) or "msg-id")
    booking_reminders.sweep()  # window still open — armed Quo catches it
    assert len(_mine(sent)) == 1
    assert _flag(bid) == 1
    # ...and the email side stayed one-shot despite the second sweep.
    assert len([a for a, _ in emails if "sam@example.com" in a[0]]) == 1


def test_sms_failure_leaves_flag_for_retry(monkeypatch, texts):
    bid = _booking(12)

    def boom(to, body):
        raise sms.SmsError("Quo returned HTTP 500")

    monkeypatch.setattr(sms, "send", boom)
    booking_reminders.sweep()
    assert _flag(bid) == 0
    monkeypatch.setattr(sms, "send", lambda to, body: texts.append((to, body)) or "msg-id")
    booking_reminders.sweep()
    assert len(_mine(texts)) == 1
    assert _flag(bid) == 1


def test_sms_runs_without_mailer(texts):
    """Email down must not block the text — guards the old mailer-only early
    return in sweep()."""
    bid = _booking(12)
    booking_reminders.sweep()  # mailer.configured() is falsy in the test env
    assert len(_mine(texts)) == 1
    row = db.one("SELECT reminded_24h, reminded_sms FROM bookings WHERE id=?", (bid,))
    assert row["reminded_sms"] == 1
    assert row["reminded_24h"] == 0  # email channel untouched, retries when armed
