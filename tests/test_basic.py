"""Basic unit tests extracted from smoke for fast feedback (-m unit).

These are simple, no heavy side effects beyond the test client.
"""

import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.unit
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.unit
def test_security_headers(client):
    # clickjacking + MIME-sniffing protection on every response (R18)
    r = client.get("/healthz")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    # marketing home stays indexable but keeps the hardening headers
    home = client.get("/")
    assert "x-robots-tag" not in home.headers
    assert home.headers["x-frame-options"] == "DENY"


@pytest.mark.unit
def test_csp_header(client):
    # Content-Security-Policy ships on every response as XSS/clickjacking
    # defense-in-depth (R18). script-src keeps 'unsafe-inline' for now because the
    # templates use inline handlers; the locked-down directives are the win here.
    csp = client.get("/healthz").headers["content-security-policy"]
    for needed in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert needed in csp, needed
    # analytics is the only off-origin asset, allowed for script + connect
    assert "https://plausible.io" in csp
    # indexable marketing pages carry the policy too
    assert "content-security-policy" in client.get("/").headers


@pytest.mark.unit
def test_csrf_same_origin_enforced(client):
    from app import config, security

    # a cross-origin state-changing POST is rejected by the guard, before auth
    # even runs — the browser stamps Origin on a malicious cross-site form submit
    r = client.post(
        "/admin/login",
        data={"password": "x"},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # Referer is the fallback signal when Origin is absent
    r = client.post(
        "/admin/login",
        data={"password": "x"},
        headers={"referer": "https://evil.example/page"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # a same-origin POST passes the guard (wrong pw -> 401, decidedly NOT 403)
    r = client.post(
        "/admin/login",
        data={"password": "nope"},
        headers={"origin": config.BASE_URL},
        follow_redirects=False,
    )
    assert r.status_code == 401
    # server-to-server webhooks send no Origin/Referer and stay unaffected
    # (503 = not configured in tests; the point is it is NOT a 403)
    r = client.post("/webhooks/stripe", content=b"{}")
    assert r.status_code == 503
    security.pin_clear("testclient", 0)  # drop the failed-login bookkeeping


@pytest.mark.unit
def test_branded_error_pages(client):
    # clients clicking bad links in a browser get a branded page, not raw JSON
    r = client.get("/g/nope12345678", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "double-check it" in r.text and "text/html" in r.headers["content-type"]
    # programmatic callers (HTMX, zip status polls) keep plain JSON errors
    r = client.get("/g/nope12345678", headers={"accept": "application/json"})
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"


@pytest.mark.unit
def test_admin_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.unit
def test_error_alert_throttle(monkeypatch):
    # error_alert collapses a crash storm to one alert per signature per window,
    # counting the rest — so one bug can't flood Telegram (R14: observable, not noisy).
    from app import alerts

    sent: list[str] = []

    class _InlineThread:  # run the send synchronously so the test can assert on it
        def __init__(self, target, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(alerts, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(alerts.threading, "Thread", _InlineThread)
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "t")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "c")
    alerts._error_last.clear()
    alerts._error_suppressed.clear()

    sig = "GET /boom|KeyError"
    alerts.error_alert(sig, "first")
    alerts.error_alert(sig, "second")  # within window -> suppressed
    alerts.error_alert(sig, "third")  # within window -> suppressed
    assert len(sent) == 1 and "first" in sent[0]
    assert alerts._error_suppressed[sig] == 2

    # once the window passes, the next alert sends AND reports the swallowed count
    alerts._error_last[sig] = 0.0
    alerts.error_alert(sig, "later")
    assert len(sent) == 2 and "+2 more" in sent[1]


@pytest.mark.unit
def test_unhandled_exception_alerts_and_500(monkeypatch):
    # an uncaught exception returns a clean 500 (branded HTML / plain JSON, no leak)
    # AND fires a crash alert — the whole point of in-app monitoring.
    from app import alerts

    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "error_alert", lambda sig, text: fired.append((sig, text)))

    async def _boom(request):
        raise RuntimeError("kaboom-secret-detail")

    app.add_route("/__test_boom", _boom)
    # no `with` (no lifespan): the raising route touches neither db nor the job
    # pool, and entering lifespan here would stop the module-shared pool for later
    # tests. raise_server_exceptions=False so the handler's 500 is returned, not re-raised.
    c = TestClient(app, raise_server_exceptions=False)
    try:
        r = c.get("/__test_boom", headers={"accept": "text/html"})
        assert r.status_code == 500
        assert "Something went wrong" in r.text
        assert "kaboom-secret-detail" not in r.text  # detail never leaks to client
        r = c.get("/__test_boom", headers={"accept": "application/json"})
        assert r.status_code == 500 and r.json()["detail"] == "internal server error"
    finally:
        app.router.routes = [
            rt for rt in app.router.routes if getattr(rt, "path", None) != "/__test_boom"
        ]
    assert fired and "RuntimeError" in fired[0][0]
