"""Auto Google Meet links for virtual event types (Cal.com-parity item 2).

What must hold: only auto_meet event types request a conference (with
conferenceDataVersion=1, which Google requires to act on conferenceData); the
minted link is stamped on the booking and reaches every client surface — the
confirmation email (which forces gcal sync to run BEFORE the mailer), the
manage page, and the T-24h reminders; a gcal outage never blocks the emails
but does wake Kevin, because a video call with no join link is a no-show in
the making. Assertions are scoped to this file's own rows — shared test DB."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import alerts, booking_notify, booking_reminders, db, gcal, mailer, sms
from app.main import app

pytestmark = pytest.mark.integration

MEET = "https://meet.google.com/abc-defg-hij"


@pytest.fixture(autouse=True)
def _clean():
    yield
    db.run("DELETE FROM bookings WHERE token LIKE 'autmeet-%'")
    db.run("DELETE FROM inquiries WHERE email = 'autmeet@example.com'")
    db.run("DELETE FROM clients WHERE email = 'autmeet@example.com'")
    db.run("DELETE FROM event_types WHERE slug LIKE 'autmeet-%'")


@pytest.fixture
def google(monkeypatch):
    """Calendar connected; every events-API call captured as (method, path, body)."""
    calls = []

    def fake_api(method, path, payload=None):
        calls.append((method, path, payload))
        return {"id": "gevt-1", "hangoutLink": MEET}

    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(gcal, "is_connected", lambda: True)
    monkeypatch.setattr(gcal, "_api", fake_api)
    return calls


def _event(auto_meet=1, slug="autmeet-call"):
    return db.run(
        "INSERT INTO event_types (slug, name, duration_min, auto_meet) VALUES (?,?,?,?)",
        (slug, "Discovery Call", 30, auto_meet),
    )


def _booking(eid, token="autmeet-1", start="2027-03-01 15:00:00", meet_url=None):
    return db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, phone, start_utc, end_utc, status,
            tz, meet_url)
           VALUES (?,?,?,?,?,?,?,'confirmed',?,?)""",
        (
            token,
            eid,
            "Mel",
            "autmeet@example.com",
            "+15550002222",
            start,
            start[:11] + "15:30:00",
            "America/New_York",
            meet_url,
        ),
    )


def test_conference_requested_and_link_stamped(google):
    bid = _booking(_event())
    gcal.on_booking_confirmed(bid)
    (method, path, body) = [c for c in google if c[0] == "POST"][0]
    assert "conferenceDataVersion=1" in path
    conf = body["conferenceData"]["createRequest"]
    assert conf["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert conf["requestId"].startswith(f"mise-b{bid}-")
    row = db.one("SELECT meet_url, google_event_id FROM bookings WHERE id=?", (bid,))
    assert row["meet_url"] == MEET
    assert row["google_event_id"] == "gevt-1"


def test_regular_event_requests_no_conference(google, monkeypatch):
    # A non-Meet event whose API response carries no hangoutLink stays linkless.
    monkeypatch.setattr(
        gcal, "_api", lambda m, p, payload=None: google.append((m, p, payload)) or {"id": "gevt-2"}
    )
    bid = _booking(_event(auto_meet=0, slug="autmeet-shoot"))
    gcal.on_booking_confirmed(bid)
    (method, path, body) = [c for c in google if c[0] == "POST"][0]
    assert "conferenceDataVersion" not in path
    assert "conferenceData" not in body
    assert db.one("SELECT meet_url FROM bookings WHERE id=?", (bid,))["meet_url"] is None


def test_confirmation_email_carries_join_link(google, monkeypatch):
    """Guards the ordering: gcal sync must run before the mailer composes, or
    the link exists only after the email is gone."""
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: sent.append((a, kw)))
    bid = _booking(_event())
    booking_notify.confirm(bid)
    client_mail = [a for a, _ in sent if a[0] == "autmeet@example.com"]
    assert len(client_mail) == 1
    assert f"Join: {MEET}" in client_mail[0][2]
    kevin_mail = [a for a, kw in sent if a[0] != "autmeet@example.com"]
    assert any(f"Meet: {MEET}" in a[2] for a in kevin_mail)
    # The .ics description advertises the join link too.
    assert any(MEET in (kw.get("ics") or {}).get("content", "") for _, kw in sent)


def test_gcal_down_alerts_but_emails_go_out(monkeypatch):
    sent, alarms = [], []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(alerts, "ops_alert", lambda kind, text, **kw: alarms.append((kind, text)))
    bid = _booking(_event())  # gcal.configured() is falsy in the test env
    booking_notify.confirm(bid)
    assert any(a[0] == "autmeet@example.com" for a, _ in sent)
    assert all("Join:" not in a[2] for a, _ in sent if a[0] == "autmeet@example.com")
    assert any(kind == "booking_meet_link_missing" for kind, _ in alarms)


def test_manage_page_shows_join_link_only_while_confirmed():
    eid = _event()
    _booking(eid, token="autmeet-live", meet_url=MEET)
    with TestClient(app) as pub:
        page = pub.get("/booking/autmeet-live").text
        assert MEET in page
    db.run("UPDATE bookings SET status='cancelled' WHERE token='autmeet-live'")
    with TestClient(app) as pub:
        assert MEET not in pub.get("/booking/autmeet-live").text


def test_reminders_carry_join_link(monkeypatch):
    now = dt.datetime(2027, 3, 1, 3, 0, tzinfo=dt.UTC)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(booking_reminders.dt, "datetime", Frozen)
    emails, texts = [], []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **kw: emails.append((a, kw)))
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send", lambda to, body: texts.append((to, body)) or "id")
    _booking(_event(), token="autmeet-rem", meet_url=MEET)  # starts 12h out
    booking_reminders.sweep()
    mine_mail = [a for a, _ in emails if a[0] == "autmeet@example.com"]
    assert len(mine_mail) == 1 and f"Join: {MEET}" in mine_mail[0][2]
    mine_sms = [(to, body) for to, body in texts if "autmeet-rem" in body]
    assert len(mine_sms) == 1 and MEET in mine_sms[0][1]
