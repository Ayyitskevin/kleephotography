"""sms.send() itself — the outbound Quo call every other test patches away.

Only ``configured()`` and ``verify_webhook`` had coverage; the body that builds
the provider request (endpoint, auth header, ``{from, to[], content}`` payload)
and maps every failure to SmsError had none. These tests patch the urllib seam,
so no request leaves the process and no text is ever sent, and assert on the
``Request`` the module constructs.
"""

import json
import urllib.error
import urllib.request

import pytest

from app import config, sms

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "QUO_API_KEY", "quo-key")
    monkeypatch.setattr(config, "QUO_NUMBER", "+15551230000")
    monkeypatch.setattr(config, "QUO_API_BASE", "https://api.example.test/v1")
    monkeypatch.setattr(config, "QUO_TIMEOUT", 7)


@pytest.fixture
def calls(monkeypatch):
    """Capture every urlopen the module attempts. Empty == no network attempt."""
    seen = []

    def fake(req, timeout=None):
        seen.append((req, timeout))
        return _FakeResponse(json.dumps({"data": {"id": "MSG-1"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _fail_with(monkeypatch, exc):
    def fake(req, timeout=None):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_send_refuses_before_touching_the_network_when_unconfigured(monkeypatch, calls):
    monkeypatch.setattr(config, "QUO_API_KEY", "")
    monkeypatch.setattr(config, "QUO_NUMBER", "")
    with pytest.raises(sms.SmsError, match="not configured"):
        sms.send("+15559990000", "hello")
    assert calls == []


@pytest.mark.parametrize(
    ("to", "body", "message"),
    [
        ("", "hello", "no recipient"),
        ("   ", "hello", "no recipient"),
        ("+15559990000", "", "body is empty"),
        ("+15559990000", "   ", "body is empty"),
    ],
)
def test_send_rejects_empty_recipient_or_body_without_calling_quo(armed, calls, to, body, message):
    with pytest.raises(sms.SmsError, match=message):
        sms.send(to, body)
    assert calls == []


def test_send_posts_the_quo_payload_and_returns_the_provider_id(armed, calls):
    msg_id = sms.send("  +15559990000  ", "  Shoot confirmed for Tuesday.  ")

    assert msg_id == "MSG-1"
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.full_url == "https://api.example.test/v1/messages"
    assert req.get_method() == "POST"
    assert timeout == 7
    # urllib title-cases header keys as they are added.
    assert req.headers["Authorization"] == "quo-key"
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data) == {
        "from": "+15551230000",
        "to": ["+15559990000"],
        "content": "Shoot confirmed for Tuesday.",
    }


def test_send_accepts_a_flat_id_payload(armed, monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(b'{"id": "MSG-FLAT"}'),
    )
    assert sms.send("+15559990000", "hi") == "MSG-FLAT"


@pytest.mark.parametrize("payload", [b"{}", b'{"data": {}}', b'{"data": null}', b'{"id": null}'])
def test_send_treats_a_missing_provider_id_as_non_fatal(armed, monkeypatch, payload):
    """The text went out; the messages row simply carries no provider id."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(payload))
    assert sms.send("+15559990000", "hi") == ""


def test_send_leaks_attributeerror_when_quo_answers_with_a_json_array(armed, monkeypatch):
    """Pins CURRENT behaviour, not intended behaviour.

    The module contract is "raises SmsError on every failure path", and the only
    caller (admin/inbox.py reply) catches SmsError alone — so a valid-JSON but
    non-object body escapes as a 500 instead of the 502 "SMS send failed" the
    operator is meant to see. Nothing is written either way. Change this test
    together with the fix.
    """
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"[]"))
    with pytest.raises(AttributeError):
        sms.send("+15559990000", "hi")


def test_send_maps_an_http_error_to_sms_error_without_leaking_the_body(armed, monkeypatch):
    _fail_with(
        monkeypatch,
        urllib.error.HTTPError(
            "https://api.example.test/v1/messages", 402, "Payment Required", {}, None
        ),
    )
    with pytest.raises(sms.SmsError) as exc:
        sms.send("+15559990000", "hi")
    assert str(exc.value) == "Quo returned HTTP 402"


def test_send_maps_an_unreachable_provider_to_sms_error(armed, monkeypatch):
    _fail_with(monkeypatch, urllib.error.URLError("name resolution failed"))
    with pytest.raises(sms.SmsError, match="Quo unreachable: name resolution failed"):
        sms.send("+15559990000", "hi")


def test_send_maps_a_timeout_to_sms_error(armed, monkeypatch):
    _fail_with(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(sms.SmsError, match="Quo unreachable"):
        sms.send("+15559990000", "hi")


def test_send_maps_an_unreadable_response_to_sms_error(armed, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"<html>502</html>")
    )
    with pytest.raises(sms.SmsError, match="unreadable response"):
        sms.send("+15559990000", "hi")
