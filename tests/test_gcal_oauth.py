"""Google Calendar OAuth — token exchange, refresh, skew, disconnect, callback.

`_token_call`, `exchange_code`, `_access_token` and `disconnect` had no tests at
all, nor did `/admin/scheduling/google/callback`. A refresh bug here is silent:
free/busy fails open, event sync swallows its exception, and calendar sync is
simply dead in production with nothing on the page to say so.

The HTTP seam (urllib) is patched throughout — no request leaves the process.
Every test leaves `google_oauth` empty so the rest of the suite still sees an
unconnected calendar.
"""

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app import config, db, gcal
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admin_client():
    with TestClient(app) as client:
        response = client.post(
            "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303
        yield client


@pytest.fixture(autouse=True)
def _clean_oauth_state():
    """The whole session shares one DB — never leave a connected calendar behind."""
    db.run("DELETE FROM google_oauth")
    yield
    db.run("DELETE FROM google_oauth")
    db.run("DELETE FROM audit_log WHERE entity_type='google_calendar'")


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "https://example.test/cb")


class _FakeResponse:
    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _token_seam(monkeypatch, payload: dict):
    """Patch urllib so the token endpoint answers `payload`; return the call log."""
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def _fail_seam(monkeypatch, exc):
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def _stamp(offset_secs: int) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=offset_secs)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _connect_row(expiry_offset_secs: int, access_token: str = "cached-access"):
    db.run(
        """INSERT INTO google_oauth (id, refresh_token, access_token, access_expiry, scope)
           VALUES (1,?,?,?,?)""",
        ("refresh-abc", access_token, _stamp(expiry_offset_secs), gcal.SCOPES),
    )


# ── auth_url / _token_call ───────────────────────────────────────────────────


def test_auth_url_requests_offline_consent_for_a_refresh_token(creds):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(gcal.auth_url("state-xyz")).query)
    assert gcal.auth_url("state-xyz").startswith(gcal.AUTH_URL + "?")
    assert q["client_id"] == ["client-id.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["https://example.test/cb"]
    assert q["state"] == ["state-xyz"]
    # Without both of these Google withholds the refresh token on re-consent.
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["scope"] == [gcal.SCOPES]


def test_token_call_posts_form_encoded_payload_to_googles_token_endpoint(monkeypatch):
    calls = _token_seam(monkeypatch, {"access_token": "at"})
    assert gcal._token_call({"grant_type": "refresh_token", "refresh_token": "r"}) == {
        "access_token": "at"
    }
    req, timeout = calls[0]
    assert req.full_url == gcal.TOKEN_URL
    assert req.get_method() == "POST"
    assert timeout == 15
    assert req.headers["Content-type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(req.data.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["r"],
    }


def test_token_call_maps_an_http_error_to_gcal_error_without_echoing_the_body(monkeypatch):
    _fail_seam(
        monkeypatch,
        urllib.error.HTTPError(gcal.TOKEN_URL, 400, "Bad Request", {}, None),
    )
    with pytest.raises(gcal.GcalError) as exc:
        gcal._token_call({"grant_type": "refresh_token", "refresh_token": "secret-token"})
    assert str(exc.value) == "token endpoint 400"
    assert "secret-token" not in str(exc.value)


def test_token_call_maps_an_unreachable_endpoint_to_gcal_error(monkeypatch):
    _fail_seam(monkeypatch, urllib.error.URLError("no route to host"))
    with pytest.raises(gcal.GcalError, match="token endpoint unreachable"):
        gcal._token_call({"grant_type": "refresh_token"})


# ── exchange_code ────────────────────────────────────────────────────────────


def test_exchange_code_trades_the_code_and_persists_the_refresh_token(creds, monkeypatch):
    calls = _token_seam(
        monkeypatch,
        {
            "refresh_token": "refresh-1",
            "access_token": "access-1",
            "expires_in": 3599,
            "scope": gcal.SCOPES,
        },
    )
    gcal.exchange_code("auth-code-1")

    assert urllib.parse.parse_qs(calls[0][0].data.decode()) == {
        "code": ["auth-code-1"],
        "client_id": ["client-id.apps.googleusercontent.com"],
        "client_secret": ["client-secret"],
        "redirect_uri": ["https://example.test/cb"],
        "grant_type": ["authorization_code"],
    }

    row = db.one("SELECT * FROM google_oauth WHERE id=1")
    assert row["refresh_token"] == "refresh-1"
    assert row["access_token"] == "access-1"
    assert row["scope"] == gcal.SCOPES
    assert row["connected_at"]
    assert _stamp(3500) < row["access_expiry"] <= _stamp(3600)
    assert gcal.is_connected() is True
    assert gcal.status()["connected"] is True


def test_exchange_code_upserts_over_an_existing_connection(creds, monkeypatch):
    _connect_row(600)
    _token_seam(
        monkeypatch,
        {"refresh_token": "refresh-2", "access_token": "access-2", "expires_in": 3600},
    )
    gcal.exchange_code("auth-code-2")

    rows = db.all_("SELECT * FROM google_oauth")
    assert len(rows) == 1
    assert rows[0]["refresh_token"] == "refresh-2"
    assert rows[0]["access_token"] == "access-2"


def test_exchange_code_without_a_refresh_token_raises_and_writes_nothing(creds, monkeypatch):
    _token_seam(monkeypatch, {"access_token": "access-only", "expires_in": 3600})
    with pytest.raises(gcal.GcalError, match="no refresh_token returned"):
        gcal.exchange_code("auth-code-3")
    assert db.all_("SELECT * FROM google_oauth") == []
    assert gcal.is_connected() is False


# ── _access_token: cache, skew, refresh, failure ─────────────────────────────


def test_access_token_raises_when_not_connected(monkeypatch):
    calls = _token_seam(monkeypatch, {"access_token": "never"})
    with pytest.raises(gcal.GcalError, match="not connected"):
        gcal._access_token()
    assert calls == []


def test_access_token_reuses_a_live_cached_token(creds, monkeypatch):
    _connect_row(600)
    calls = _token_seam(monkeypatch, {"access_token": "fresh"})
    assert gcal._access_token() == "cached-access"
    assert calls == []


def test_access_token_refreshes_inside_the_sixty_second_expiry_skew(creds, monkeypatch):
    """A token valid for another 30s is treated as spent — an in-flight API call
    must not be handed a token that dies mid-request."""
    _connect_row(30)
    calls = _token_seam(monkeypatch, {"access_token": "refreshed", "expires_in": 3600})

    assert gcal._access_token() == "refreshed"
    assert urllib.parse.parse_qs(calls[0][0].data.decode()) == {
        "client_id": ["client-id.apps.googleusercontent.com"],
        "client_secret": ["client-secret"],
        "refresh_token": ["refresh-abc"],
        "grant_type": ["refresh_token"],
    }

    row = db.one("SELECT * FROM google_oauth WHERE id=1")
    assert row["access_token"] == "refreshed"
    assert row["refresh_token"] == "refresh-abc"  # the long-lived grant is untouched
    assert _stamp(3500) < row["access_expiry"] <= _stamp(3600)


def test_access_token_refreshes_an_already_expired_token(creds, monkeypatch):
    _connect_row(-120)
    _token_seam(monkeypatch, {"access_token": "refreshed-2", "expires_in": 3600})
    assert gcal._access_token() == "refreshed-2"
    assert db.one("SELECT access_token FROM google_oauth WHERE id=1")["access_token"] == (
        "refreshed-2"
    )


def test_access_token_refreshes_when_no_token_was_ever_cached(creds, monkeypatch):
    db.run(
        "INSERT INTO google_oauth (id, refresh_token, access_token, access_expiry) VALUES (1,?,?,?)",
        ("refresh-abc", "", None),
    )
    _token_seam(monkeypatch, {"access_token": "first", "expires_in": 3600})
    assert gcal._access_token() == "first"


def test_access_token_raises_and_keeps_the_old_row_when_refresh_returns_nothing(creds, monkeypatch):
    _connect_row(-10, access_token="stale-access")
    _token_seam(monkeypatch, {"error": "invalid_grant"})
    with pytest.raises(gcal.GcalError, match="refresh produced no access token"):
        gcal._access_token()
    row = db.one("SELECT * FROM google_oauth WHERE id=1")
    assert row["access_token"] == "stale-access"
    assert row["refresh_token"] == "refresh-abc"


def test_access_token_propagates_a_token_endpoint_failure(creds, monkeypatch):
    _connect_row(-10)
    _fail_seam(monkeypatch, urllib.error.HTTPError(gcal.TOKEN_URL, 401, "Unauthorized", {}, None))
    with pytest.raises(gcal.GcalError, match="token endpoint 401"):
        gcal._access_token()


# ── disconnect ───────────────────────────────────────────────────────────────


def test_disconnect_drops_the_stored_grant(creds):
    _connect_row(600)
    assert gcal.is_connected() is True
    gcal.disconnect()
    assert db.all_("SELECT * FROM google_oauth") == []
    assert gcal.is_connected() is False
    assert gcal.status()["connected"] is False


def test_disconnect_is_idempotent():
    gcal.disconnect()
    gcal.disconnect()
    assert db.all_("SELECT * FROM google_oauth") == []


# ── /admin/scheduling/google/callback ────────────────────────────────────────


def _start_consent(admin_client) -> str:
    """Run the connect leg and return the state Google would echo back."""
    resp = admin_client.get("/admin/scheduling/google/connect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(gcal.AUTH_URL)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.headers["location"]).query)["state"][0]
    assert admin_client.cookies.get("g_oauth_state") == state
    return state


def test_callback_completes_the_connection_and_audits_it(admin_client, creds, monkeypatch):
    state = _start_consent(admin_client)
    calls = _token_seam(
        monkeypatch,
        {"refresh_token": "refresh-cb", "access_token": "access-cb", "expires_in": 3600},
    )

    resp = admin_client.get(
        f"/admin/scheduling/google/callback?code=code-cb&state={state}", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/scheduling"
    assert urllib.parse.parse_qs(calls[0][0].data.decode())["code"] == ["code-cb"]

    assert db.one("SELECT refresh_token FROM google_oauth WHERE id=1")["refresh_token"] == (
        "refresh-cb"
    )
    audit = db.one(
        """SELECT entity_id, action FROM audit_log
           WHERE entity_type='google_calendar' ORDER BY id DESC LIMIT 1"""
    )
    assert (audit["entity_id"], audit["action"]) == (1, "connect")
    assert admin_client.cookies.get("g_oauth_state") is None


def test_callback_rejects_a_mismatched_state_without_exchanging(admin_client, creds, monkeypatch):
    _start_consent(admin_client)
    calls = _token_seam(monkeypatch, {"refresh_token": "should-not-be-stored"})

    resp = admin_client.get(
        "/admin/scheduling/google/callback?code=code-cb&state=forged", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/scheduling?gerr=state"
    assert calls == []
    assert db.all_("SELECT * FROM google_oauth") == []


def test_callback_reports_a_denied_consent(admin_client, creds, monkeypatch):
    _start_consent(admin_client)
    calls = _token_seam(monkeypatch, {"refresh_token": "should-not-be-stored"})

    resp = admin_client.get(
        "/admin/scheduling/google/callback?error=access_denied", follow_redirects=False
    )
    assert resp.headers["location"] == "/admin/scheduling?gerr=denied"
    assert calls == []
    assert db.all_("SELECT * FROM google_oauth") == []


def test_callback_reports_a_missing_code(admin_client, creds):
    state = _start_consent(admin_client)
    resp = admin_client.get(
        f"/admin/scheduling/google/callback?state={state}", follow_redirects=False
    )
    assert resp.headers["location"] == "/admin/scheduling?gerr=exchange"
    assert db.all_("SELECT * FROM google_oauth") == []


def test_callback_reports_a_failed_exchange(admin_client, creds, monkeypatch):
    state = _start_consent(admin_client)
    _fail_seam(monkeypatch, urllib.error.HTTPError(gcal.TOKEN_URL, 400, "Bad Request", {}, None))

    resp = admin_client.get(
        f"/admin/scheduling/google/callback?code=stale&state={state}", follow_redirects=False
    )
    assert resp.headers["location"] == "/admin/scheduling?gerr=exchange"
    assert db.all_("SELECT * FROM google_oauth") == []
    assert db.one("SELECT id FROM audit_log WHERE entity_type='google_calendar'") is None


def test_disconnect_route_clears_the_grant_and_audits_it(admin_client, creds):
    _connect_row(600)
    resp = admin_client.post("/admin/scheduling/google/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/scheduling"
    assert db.all_("SELECT * FROM google_oauth") == []
    audit = db.one(
        """SELECT entity_id, action FROM audit_log
           WHERE entity_type='google_calendar' ORDER BY id DESC LIMIT 1"""
    )
    assert (audit["entity_id"], audit["action"]) == (1, "disconnect")


def test_connect_route_refuses_before_the_client_creds_are_provisioned(admin_client, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "")
    resp = admin_client.get("/admin/scheduling/google/connect", follow_redirects=False)
    assert resp.status_code == 400
