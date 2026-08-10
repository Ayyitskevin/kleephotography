"""Quo (OpenPhone) SMS adapter — outbound send and inbound webhook verification.

No network: `urllib.request.urlopen` is monkeypatched at the module seam, so the
real request construction and response parsing run. The contract pinned here is
the one `app/admin/inbox.py` depends on — its send-reply route catches
`sms.SmsError` and nothing else, so anything else escaping `send()` is a 500 plus
a crash alert instead of the 502 the operator is meant to see. `verify_webhook`
must likewise stay total: it returns a bool and fails closed, never raises.
"""

import base64
import hashlib
import hmac
import json
import urllib.error

import pytest

from app import config, sms

pytestmark = pytest.mark.unit

SIGNING_SECRET = base64.b64encode(b"quo-signing-secret").decode()


@pytest.fixture()
def armed(monkeypatch):
    """Fake Quo credentials so configured() is true and send() proceeds."""
    monkeypatch.setattr(config, "QUO_API_KEY", "quo-test-key")
    monkeypatch.setattr(config, "QUO_NUMBER", "+15550000000")
    monkeypatch.setattr(config, "QUO_API_BASE", "https://quo.test/v1")
    monkeypatch.setattr(config, "QUO_TIMEOUT", 5)


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _answers(monkeypatch, body, captured=None):
    """Point the urllib seam at a canned response.

    `body` is bytes/str returned as the response payload, or an Exception the
    call raises. Pass `captured` to record what send() actually put on the wire.
    """

    def fake_urlopen(req, timeout):
        if captured is not None:
            captured["url"] = req.full_url
            captured["method"] = req.method
            captured["timeout"] = timeout
            captured["json"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
        if isinstance(body, Exception):
            raise body
        return _Resp(body if isinstance(body, bytes) else body.encode())

    monkeypatch.setattr(sms.urllib.request, "urlopen", fake_urlopen)


def _never_called(monkeypatch):
    """Fail loudly if send() reaches the network at all."""

    def fake_urlopen(req, timeout):
        raise AssertionError("send() should not have called Quo")

    monkeypatch.setattr(sms.urllib.request, "urlopen", fake_urlopen)


def _sign(raw: bytes, timestamp: str = "1700000000", secret: str = SIGNING_SECRET) -> str:
    digest = hmac.new(base64.b64decode(secret), timestamp.encode() + b"." + raw, hashlib.sha256)
    return f"hmac;1;{timestamp};{base64.b64encode(digest.digest()).decode()}"


# --- send(): the wire format and the success envelope ------------------------


def test_send_posts_the_quo_envelope_and_returns_the_nested_id(armed, monkeypatch):
    captured = {}
    _answers(monkeypatch, json.dumps({"data": {"id": "AC-123"}}), captured)

    assert sms.send(" +15551234567 ", "  hello there  ") == "AC-123"
    assert captured["url"] == "https://quo.test/v1/messages"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5
    assert captured["json"] == {
        "from": "+15550000000",
        "to": ["+15551234567"],
        "content": "hello there",
    }
    assert captured["auth"] == "quo-test-key"


def test_send_accepts_a_flat_id_envelope(armed, monkeypatch):
    _answers(monkeypatch, json.dumps({"id": "flat-9"}))
    assert sms.send("+15551234567", "hi") == "flat-9"


def test_send_reports_success_with_no_id_when_the_envelope_omits_one(armed, monkeypatch):
    # Documented: the text went out, the messages row simply carries no provider id.
    _answers(monkeypatch, json.dumps({"data": {}}))
    assert sms.send("+15551234567", "hi") == ""


@pytest.mark.parametrize(
    "raw_id, expected",
    [
        (12345, "12345"),  # a numeric id is still an id — keep it
        (["AC-1"], ""),  # everything below is not an id: same path as an absent one
        ({"id": "AC-1"}, ""),
        (True, ""),
        (1.5, ""),
        (0, ""),
        (None, ""),
    ],
)
def test_send_survives_a_provider_id_that_is_not_a_string(armed, monkeypatch, raw_id, expected):
    """The envelope is well-formed and the vendor answered 2xx, so the text went
    out — an id of an unexpected type must not become SmsError, and must not
    escape as AttributeError from .strip() either."""
    _answers(monkeypatch, json.dumps({"data": {"id": raw_id}}))
    assert sms.send("+15551234567", "hi") == expected


# --- send(): refusals that never touch the network ---------------------------


def test_send_refuses_when_sms_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "QUO_API_KEY", "")
    monkeypatch.setattr(config, "QUO_NUMBER", "")
    _never_called(monkeypatch)

    with pytest.raises(sms.SmsError, match="not configured"):
        sms.send("+15551234567", "hi")


def test_send_refuses_a_blank_recipient(armed, monkeypatch):
    _never_called(monkeypatch)
    with pytest.raises(sms.SmsError, match="no recipient"):
        sms.send("   ", "hi")


def test_send_refuses_a_blank_body(armed, monkeypatch):
    _never_called(monkeypatch)
    with pytest.raises(sms.SmsError, match="body is empty"):
        sms.send("+15551234567", "   ")


# --- send(): transport failures all surface as SmsError ----------------------


def test_send_maps_an_http_error_to_smserror(armed, monkeypatch):
    _answers(
        monkeypatch,
        urllib.error.HTTPError("https://quo.test/v1/messages", 429, "Too Many", {}, None),
    )
    with pytest.raises(sms.SmsError, match="Quo returned HTTP 429"):
        sms.send("+15551234567", "hi")


def test_send_maps_an_unreachable_host_to_smserror(armed, monkeypatch):
    _answers(monkeypatch, urllib.error.URLError("name resolution failed"))
    with pytest.raises(sms.SmsError, match="Quo unreachable: name resolution failed"):
        sms.send("+15551234567", "hi")


def test_send_maps_a_timeout_to_smserror(armed, monkeypatch):
    _answers(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(sms.SmsError, match="Quo unreachable: timed out"):
        sms.send("+15551234567", "hi")


def test_send_maps_a_non_json_body_to_smserror(armed, monkeypatch):
    _answers(monkeypatch, b"<html>502 Bad Gateway</html>")
    with pytest.raises(sms.SmsError, match="unreadable response"):
        sms.send("+15551234567", "hi")


@pytest.mark.parametrize(
    "body, shape",
    [("[]", "list"), ("null", "NoneType"), ('"queued"', "str"), ("42", "int")],
)
def test_send_rejects_a_json_body_that_is_not_an_object(armed, monkeypatch, body, shape):
    """Valid JSON that is not an object is not a Quo envelope — a proxy error page,
    a wrong API base. Nothing in it confirms the send, so it must surface as
    SmsError (the only exception the Inbox reply route catches) and name the shape
    the vendor actually returned, not report success and not escape as AttributeError."""
    _answers(monkeypatch, body)
    with pytest.raises(sms.SmsError, match=f"unexpected response shape \\({shape}\\)"):
        sms.send("+15551234567", "hi")


# --- verify_webhook(): fails closed on everything it cannot verify -----------


def test_verify_webhook_accepts_a_correctly_signed_body(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", SIGNING_SECRET)
    raw = b'{"type":"message.received"}'
    assert sms.verify_webhook(raw, _sign(raw)) is True


def test_verify_webhook_rejects_a_body_that_changed_after_signing(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", SIGNING_SECRET)
    header = _sign(b'{"type":"message.received"}')
    assert sms.verify_webhook(b'{"type":"message.tampered"}', header) is False


def test_verify_webhook_rejects_a_replayed_timestamp_swap(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", SIGNING_SECRET)
    raw = b'{"type":"message.received"}'
    header = _sign(raw, timestamp="1700000000")
    assert sms.verify_webhook(raw, header.replace("1700000000", "1700009999")) is False


def test_verify_webhook_rejects_when_no_secret_is_provisioned(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "")
    raw = b'{"type":"message.received"}'
    assert sms.verify_webhook(raw, _sign(raw)) is False


@pytest.mark.parametrize(
    "header",
    [
        "",
        "hmac;1;1700000000",
        "hmac;1;1700000000;sig;extra",
        "sha256;1;1700000000;c2ln",
        "hmac;1;1700000000;not-the-signature",
    ],
)
def test_verify_webhook_rejects_malformed_or_wrong_headers(monkeypatch, header):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", SIGNING_SECRET)
    assert sms.verify_webhook(b'{"type":"message.received"}', header) is False


def test_verify_webhook_rejects_an_unusable_signing_secret(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "not!valid!base64!")
    raw = b'{"type":"message.received"}'
    assert sms.verify_webhook(raw, _sign(raw)) is False
