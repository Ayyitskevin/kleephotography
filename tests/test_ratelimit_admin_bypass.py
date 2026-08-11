"""Rate limiter: when the admin bypass is evaluated, and why it stays at the top.

`security.is_admin` costs a database read for any request carrying an admin
cookie (it returns False without touching the DB when there is no cookie, so
public traffic is already free). Moving that check down to the limit boundary
looks like an easy saving. It is not — see the regression guard below.
"""

import pytest
from fastapi import Request

from app import config, ratelimit, security

pytestmark = pytest.mark.unit


def _req():
    return Request({"type": "http", "headers": [], "client": ("203.0.113.7", 1234)})


@pytest.fixture(autouse=True)
def _clean():
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


def test_anonymous_client_is_metered(monkeypatch):
    monkeypatch.setattr(security, "is_admin", lambda r: False)
    limit, _ = config.RATE_LIMITS["public"]
    for i in range(limit):
        assert ratelimit.check(_req(), "/contact") is None, f"blocked early at {i}"
    blocked = ratelimit.check(_req(), "/contact")
    assert blocked is not None and blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_admin_is_never_blocked(monkeypatch):
    monkeypatch.setattr(security, "is_admin", lambda r: True)
    limit, _ = config.RATE_LIMITS["public"]
    for _ in range(limit * 2 + 5):
        assert ratelimit.check(_req(), "/contact") is None


def test_exempt_paths_never_consult_is_admin(monkeypatch):
    """Static and media short-circuit before any bypass logic runs."""
    calls = {"n": 0}
    monkeypatch.setattr(security, "is_admin", lambda r: calls.__setitem__("n", calls["n"] + 1))
    for path in ("/healthz", "/static/app.css", "/media/1/thumb.jpg", "/work/x"):
        assert ratelimit.check(_req(), path) is None
    assert calls["n"] == 0


def test_admin_traffic_does_not_meter_a_later_anonymous_login(monkeypatch):
    """Regression guard for a tempting optimisation that is actually unsafe.

    Deferring the is_admin check to the limit boundary would save a database
    read on every admin request. It also means admin requests land in the
    sliding window while under the limit, where today they are never recorded.

    Kevin's own browsing then fills the (ip, "admin") bucket — the admin routes
    are HTMX-partial heavy, so 120 requests in 60s is an ordinary minute — and
    the moment his session ends, /admin/login answers 429. He is locked out of
    his own login for up to a window, exactly when a deploy or a session expiry
    has sent him there. The bypass exists so that "deploys and post-deploy
    testing never trip it" (see ratelimit's module docstring); recording admin
    hits defeats the reason it exists.

    Verified by trying it: this and six other tests failed with 429 on
    /admin/login. The saving was one indexed SELECT on Kevin's own requests.
    """
    limit, _ = config.RATE_LIMITS["admin"]
    monkeypatch.setattr(security, "is_admin", lambda r: True)
    for _ in range(limit * 2):
        assert ratelimit.check(_req(), "/admin/home") is None

    # Session now gone — logged out, or expired on the shorter admin clock.
    monkeypatch.setattr(security, "is_admin", lambda r: False)
    assert ratelimit.check(_req(), "/admin/login") is None, (
        "a logged-in admin's own traffic metered the anonymous login that follows it"
    )
