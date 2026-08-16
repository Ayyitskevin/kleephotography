"""Weekly owner digest.

What must hold: the digest is owed from Monday DIGEST_HOUR local onward and a
host that slept through Monday catches up later that week; it goes out exactly
once per covered week (digest_log stamp, written only after a successful send,
so a mail failure retries); and its numbers are computed on the covered week's
business-timezone boundaries. Time is frozen into 2031 so the queried windows
contain only rows this file creates — the test DB is shared and everything
else writes 2026/2027 dates."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app import announcements, config, db, digest, mailer

pytestmark = pytest.mark.integration

_TZ = ZoneInfo(config.TIMEZONE)
_WEEK = dt.date(2031, 6, 2)  # covered week: Mon Jun 2 – Sun Jun 8, 2031
_MONDAY = dt.date(2031, 6, 9)  # the Monday the digest for _WEEK goes out


def _frozen(monkeypatch, local: dt.datetime):
    instant = local.replace(tzinfo=_TZ).astimezone(dt.UTC)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(digest.dt, "datetime", Frozen)


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM digest_log WHERE week_start LIKE '2031-%'")
    db.run("DELETE FROM payments WHERE created_at LIKE '2031-%'")
    db.run("DELETE FROM booking_payments WHERE created_at LIKE '2031-%'")
    db.run("DELETE FROM invoices WHERE slug LIKE 'digest-%'")
    db.run("DELETE FROM projects WHERE title LIKE 'digest-%'")
    db.run("DELETE FROM bookings WHERE token LIKE 'digest-%'")
    db.run("DELETE FROM event_types WHERE slug LIKE 'digest-%'")
    db.run("DELETE FROM inquiries WHERE email LIKE 'digest-%'")
    db.run("DELETE FROM galleries WHERE slug LIKE 'digest-%'")
    db.run("DELETE FROM clients WHERE name LIKE 'digest-%'")


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: sent.append((a, kw)))
    return sent


def _mine(sent):
    return [a for a, _ in sent if a[1].startswith("Mise weekly")]


def test_not_due_monday_before_hour(monkeypatch, outbox):
    _frozen(monkeypatch, dt.datetime(2031, 6, 9, config.DIGEST_HOUR - 1, 30))
    digest.sweep()
    assert _mine(outbox) == []
    assert not db.one("SELECT 1 AS x FROM digest_log WHERE week_start='2031-06-02'")


def test_kill_switch(monkeypatch, outbox):
    _frozen(monkeypatch, dt.datetime(2031, 6, 9, 12, 0))
    monkeypatch.setattr(config, "WEEKLY_DIGEST", False)
    digest.sweep()
    assert _mine(outbox) == []


def test_sends_once_and_stamps(monkeypatch, outbox):
    _frozen(monkeypatch, dt.datetime(2031, 6, 9, config.DIGEST_HOUR, 30))
    digest.sweep()
    digest.sweep()
    mine = _mine(outbox)
    assert len(mine) == 1
    assert mine[0][0] == config.GMAIL_USER
    assert "Jun 2 – Jun 8" in mine[0][1]
    row = db.one("SELECT audience_count FROM digest_log WHERE week_start='2031-06-02'")
    assert row and row["audience_count"] == len(announcements.audience())


def test_catches_up_midweek(monkeypatch, outbox):
    _frozen(monkeypatch, dt.datetime(2031, 6, 11, 15, 0))  # Wednesday
    digest.sweep()
    assert len(_mine(outbox)) == 1


def test_mail_failure_leaves_unstamped_then_retries(monkeypatch, outbox):
    _frozen(monkeypatch, dt.datetime(2031, 6, 9, 12, 0))

    def boom(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(mailer, "send", boom)
    digest.sweep()
    assert not db.one("SELECT 1 AS x FROM digest_log WHERE week_start='2031-06-02'")
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: outbox.append((a, kw)))
    digest.sweep()
    assert len(_mine(outbox)) == 1
    assert db.one("SELECT 1 AS x FROM digest_log WHERE week_start='2031-06-02'")


def _in_week(days: int, hours: int = 12) -> str:
    """A UTC timestamp safely inside the covered week (local day + hour)."""
    local = dt.datetime.combine(_WEEK + dt.timedelta(days=days), dt.time(hours), tzinfo=_TZ)
    return local.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def test_body_scopes_to_covered_week():
    cid = db.run("INSERT INTO clients (name) VALUES ('digest-Cli')")
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?, 'digest-Proj')", (cid,))
    iid = db.run(
        "INSERT INTO invoices (project_id, slug, title) VALUES (?, 'digest-inv', 'digest-Inv')",
        (pid,),
    )
    db.run(
        """INSERT INTO payments (invoice_id, amount_cents, kind, created_at)
           VALUES (?, 10000, 'full', ?)""",
        (iid, _in_week(1)),
    )
    eid = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES ('digest-call','Digest Call',30)"
    )
    bid = db.run(
        """INSERT INTO bookings (token, event_type_id, name, email, start_utc, end_utc,
                                 status, created_at)
           VALUES ('digest-b1', ?, 'digest-Mel', 'digest-mel@example.com',
                   '2031-06-20 15:00:00', '2031-06-20 15:30:00', 'confirmed', ?)""",
        (eid, _in_week(2)),
    )
    db.run(
        "INSERT INTO booking_payments (booking_id, amount_cents, created_at) VALUES (?, 5000, ?)",
        (bid, _in_week(2)),
    )
    # Outside the covered week — must not count.
    db.run(
        """INSERT INTO payments (invoice_id, amount_cents, kind, created_at)
           VALUES (?, 99900, 'full', '2031-06-12 12:00:00')""",
        (iid,),
    )
    db.run(
        """INSERT INTO inquiries (name, email, message, referral_source, created_at)
           VALUES ('digest-A', 'digest-a@example.com', 'hi', 'Google', ?)""",
        (_in_week(3),),
    )
    db.run(
        """INSERT INTO inquiries (name, email, message, created_at)
           VALUES ('digest-B', 'digest-b@example.com', 'hi', ?)""",
        (_in_week(4),),
    )
    db.run(
        """INSERT INTO galleries (slug, title, pin, published, review_requested_at)
           VALUES ('digest-g1', 'digest-G', '1234', 1, ?)""",
        (_in_week(5),),
    )
    # Confirmed session inside the FOLLOWING week — the "This week" list.
    db.run(
        """INSERT INTO bookings (token, event_type_id, name, email, start_utc, end_utc, status)
           VALUES ('digest-b2', ?, 'digest-Ahead', 'digest-ahead@example.com',
                   ?, ?, 'confirmed')""",
        (eid, _in_week(9, 14), _in_week(9, 15)),
    )
    subject, body, _ = digest.build(_WEEK)
    assert subject == "Mise weekly — Jun 2 – Jun 8"
    assert "Payments received: $150.00" in body
    assert "invoices $100.00 · reservations $50.00" in body
    assert "$999.00" not in body
    assert "Bookings made: 1" in body and "Digest Call: 1" in body
    assert "New inquiries: 2" in body
    assert "Google 1" in body and "unattributed 1" in body
    assert "Review asks sent: 1" in body
    assert "This week: 1 session" in body and "digest-Ahead" in body


def test_audience_growth_line():
    baseline = len(announcements.audience()) - 3
    db.run(
        "INSERT INTO digest_log (week_start, audience_count) VALUES ('2031-05-26', ?)",
        (baseline,),
    )
    _, body, _ = digest.build(_WEEK)
    assert "(+3 since last digest)" in body
