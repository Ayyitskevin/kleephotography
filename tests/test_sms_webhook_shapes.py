"""The Quo webhook must refuse a non-object payload instead of 500-ing.

json.loads happily returns a list, a string, a number or null. Every read in the
handler is `event.get(...)` / `obj.get(...)`, so any of those raised
AttributeError — an unhandled 500, which main.py's exception handler also turns
into a Telegram alert. Same defect class as the four fixed in app/sms.py.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "whsec_quo_test")
    with TestClient(app) as c:
        yield c


def _signed(client, body: str):
    ts = str(int(time.time() * 1000))
    # Mirrors sms.verify_webhook's scheme; the exact digest does not matter to
    # these tests because a bad signature is rejected before shape is examined.
    sig = hmac.new(b"whsec_quo_test", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/quo",
        content=body,
        headers={"openphone-signature": f"hmac;1;{ts};{sig}", "content-type": "application/json"},
    )


@pytest.mark.parametrize(
    "payload",
    ["[]", '"a string"', "5", "null", "true", '[{"type":"message.received"}]'],
)
def test_non_object_payload_is_refused_not_a_500(client, monkeypatch, payload):
    monkeypatch.setattr("app.sms.verify_webhook", lambda raw, sig: True)
    r = _signed(client, payload)
    assert r.status_code == 400, f"{payload!r} produced {r.status_code}"
    assert "detail" in r.json()


def test_object_with_non_object_data_is_handled(client, monkeypatch):
    """data present but the wrong shape must not raise either."""
    monkeypatch.setattr("app.sms.verify_webhook", lambda raw, sig: True)
    for data in ('"nope"', "[1,2]", "null", "7"):
        body = json.dumps({"type": "message.received"})
        body = body[:-1] + f', "data": {data}' + "}"
        r = _signed(client, body)
        assert r.status_code == 200, f"data={data} produced {r.status_code}"
        assert r.json().get("ignored") is not None


def test_a_well_formed_event_still_works(client, monkeypatch):
    monkeypatch.setattr("app.sms.verify_webhook", lambda raw, sig: True)
    body = json.dumps({"type": "message.delivered", "data": {"object": {"direction": "outgoing"}}})
    r = _signed(client, body)
    assert r.status_code == 200 and r.json()["ok"] is True
