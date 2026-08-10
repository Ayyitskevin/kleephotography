"""mailer.send() itself — the MIME body every other test patches away.

Every suite in the repo monkeypatches ``mailer.send``, so the message that
actually reaches Gmail had zero executions: a typo in the ICS ``method``
parameter (what makes Gmail/Apple Mail draw the Accept/Decline buttons on a
booking invite, and what makes a CANCEL remove the held slot) would ship green.
These tests patch ``smtplib.SMTP_SSL`` — no socket is ever opened — and assert
on the ``EmailMessage`` the module constructs and hands to ``send_message``.
"""

import smtplib

import pytest

from app import config, mailer

pytestmark = pytest.mark.unit

ICS_BODY = "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n"


class _FakeSMTP:
    """Stand-in for the SMTP_SSL context manager: records, never connects."""

    def __init__(self, recorder):
        self._rec = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self._rec["login"] = (user, password)

    def send_message(self, msg):
        self._rec["messages"].append(msg)


@pytest.fixture
def smtp(monkeypatch):
    """Patched SMTP_SSL + credentials. Returns the recorder dict."""
    monkeypatch.setattr(config, "GMAIL_USER", "studio@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-password")
    rec = {"init": None, "login": None, "messages": []}

    def factory(*args, **kwargs):
        rec["init"] = (args, kwargs)
        return _FakeSMTP(rec)

    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    return rec


def _only(rec):
    assert len(rec["messages"]) == 1
    return rec["messages"][0]


def test_send_opens_gmail_tls_and_authenticates(smtp):
    mailer.send("client@example.com", "Subject", "Body")
    assert smtp["init"] == (("smtp.gmail.com", 465), {"timeout": 20})
    assert smtp["login"] == ("studio@example.com", "app-password")


def test_send_addresses_the_recipient_and_stamps_the_studio_from(smtp):
    mailer.send("client@example.com", "Your gallery is ready", "Body")
    msg = _only(smtp)
    assert msg["To"] == "client@example.com"
    assert msg["From"] == f"{config.SITE_NAME} <studio@example.com>"
    assert msg["Subject"] == "Your gallery is ready"
    # send_message() derives the envelope recipients from To.
    assert [a.addr_spec for a in msg["To"].addresses] == ["client@example.com"]


def test_send_sets_reply_to_only_when_given(smtp):
    mailer.send("client@example.com", "S", "B", reply_to="kevin@example.com")
    assert _only(smtp)["Reply-To"] == "kevin@example.com"

    smtp["messages"].clear()
    mailer.send("client@example.com", "S", "B")
    assert _only(smtp)["Reply-To"] is None


def test_send_without_ics_is_a_single_plain_text_part(smtp):
    mailer.send("client@example.com", "S", "Line one\nLine two")
    msg = _only(smtp)
    assert msg.is_multipart() is False
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content() == "Line one\nLine two\n"


def test_send_round_trips_non_ascii_subject_and_body(smtp):
    """Serialization must survive the accents and em dashes real copy carries."""
    import email

    subject = "Réservation confirmée — 14 juin"
    body = "Bonjour Élodie,\n\nÀ bientôt — Kevin\n"
    mailer.send("client@example.com", subject, body)

    raw = _only(smtp).as_bytes()
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed["Subject"] == subject
    assert parsed.get_content() == body


def test_send_attaches_ics_with_the_method_param_that_gates_accept_decline(smtp):
    mailer.send(
        "client@example.com",
        "Shoot confirmed",
        "Details below.",
        ics={"filename": "mise-booking.ics", "content": ICS_BODY, "method": "REQUEST"},
    )
    msg = _only(smtp)
    assert msg.is_multipart() is True

    text, cal = msg.get_payload()
    assert text.get_content_type() == "text/plain"
    assert text.get_content() == "Details below.\n"

    assert cal.get_content_type() == "text/calendar"
    assert cal.get_param("method") == "REQUEST"
    assert cal.get_param("charset") == "UTF-8"
    assert cal.get_filename() == "mise-booking.ics"
    assert cal.get_payload(decode=True).decode() == ICS_BODY

    # The wire form is what Gmail/Apple Mail parse — assert the param survives it.
    assert b'method="REQUEST"' in msg.as_bytes()


def test_send_carries_a_cancel_method_through_unchanged(smtp):
    """A CANCEL invite is how a cancelled booking leaves the client's calendar."""
    cancel = ICS_BODY.replace("REQUEST", "CANCEL")
    mailer.send(
        "client@example.com",
        "Shoot cancelled",
        "Sorry about that.",
        ics={"filename": "mise-booking.ics", "content": cancel, "method": "CANCEL"},
    )
    cal = _only(smtp).get_payload()[1]
    assert cal.get_param("method") == "CANCEL"
    assert cal.get_payload(decode=True).decode() == cancel


def test_send_defaults_the_ics_method_to_request(smtp):
    mailer.send(
        "client@example.com",
        "Shoot confirmed",
        "Details below.",
        ics={"filename": "mise-booking.ics", "content": ICS_BODY},
    )
    assert _only(smtp).get_payload()[1].get_param("method") == "REQUEST"


def test_send_keeps_reply_to_alongside_an_invite(smtp):
    mailer.send(
        "client@example.com",
        "Shoot confirmed",
        "Details below.",
        reply_to="kevin@example.com",
        ics={"filename": "mise-booking.ics", "content": ICS_BODY, "method": "REQUEST"},
    )
    msg = _only(smtp)
    assert msg["Reply-To"] == "kevin@example.com"
    assert msg.get_payload()[1].get_param("method") == "REQUEST"


def test_configured_tracks_both_credentials(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_USER", "studio@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")
    assert mailer.configured() is False
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-password")
    assert mailer.configured() is True
