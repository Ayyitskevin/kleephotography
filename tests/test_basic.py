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
def test_hsts_tracks_cookie_secure(monkeypatch):
    from app import config
    from app.main import app

    # HSTS ships only when the site knows it's on TLS (same signal as Secure
    # cookies) — never for a plain-http dev origin, which would pin localhost.
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    with TestClient(app) as c:
        h = c.get("/healthz").headers["strict-transport-security"]
        assert "max-age=63072000" in h and "includeSubDomains" in h
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    with TestClient(app) as c:
        assert "strict-transport-security" not in c.get("/healthz").headers


@pytest.mark.unit
def test_cookie_secure_default_fails_safe_for_https():
    from app import config

    # an https base auto-enables Secure cookies; plain-http (dev) stays off —
    # so production can't silently ship insecure cookies by forgetting a flag
    assert config._cookie_secure_default("https://kleephotography.com") == "true"
    assert config._cookie_secure_default("http://localhost:8400") == "false"


@pytest.mark.unit
def test_portal_pin_bucket_avoids_throttle_sentinels():
    from app import security
    from app.public.portal import _pin_bucket

    # Portal lockout rows must never land on the inquiry-throttle sentinels
    # (-2/-3/-4) — a bare -portal_id collided portals 2/3/4 with them, so a
    # mistyped portal PIN could 429 that IP's contact form.
    sentinels = {
        security.INQUIRY_BUCKET_CONTACT,
        security.INQUIRY_BUCKET_BOOK,
        security.INQUIRY_BUCKET_FORM,
    }
    buckets = {_pin_bucket(i) for i in range(1, 1000)}
    assert not (buckets & sentinels)  # disjoint from the throttle sentinels
    assert all(b < -1_000_000 for b in buckets)  # distinct large-negative band
    # and disjoint from gallery ids (positive) + admin login (0) + workspace (+2M)
    assert all(b < 0 for b in buckets)


@pytest.mark.unit
def test_check_admin_password_handles_non_ascii(monkeypatch):
    from app import config, security

    # a non-ASCII password attempt must return False, not raise TypeError (which
    # would 500, fire an alert, and skip the login lockout counter)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "correct-horse")
    assert security.check_admin_password("pässwörd") is False
    assert security.check_admin_password("correct-horse") is True
    assert security.check_admin_password("naïve🔑") is False


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
def test_session_cookie_helper_sets_shared_policy(monkeypatch):
    from http.cookies import SimpleCookie

    from fastapi import Response

    from app import security

    monkeypatch.setattr(security.config, "COOKIE_SECURE", True)
    resp = Response()
    security.set_signed_session_cookie(resp, security.ADMIN_COOKIE, "admin", max_age=123)

    cookie = SimpleCookie()
    cookie.load(resp.headers["set-cookie"])
    morsel = cookie[security.ADMIN_COOKIE]

    assert security.unsign(morsel.value) == "admin"
    assert morsel["max-age"] == "123"
    assert morsel["path"] == "/"
    assert morsel["httponly"]
    assert morsel["secure"]
    assert morsel["samesite"].lower() == "lax"


@pytest.mark.unit
def test_public_showcase_bootstrap_relabels_demo_gallery(client):
    from app import bootstrap, db

    gid = db.run(
        """INSERT INTO galleries
           (slug, title, client_name, pin, published, cs_published, cs_tagline,
            cs_brief, cs_credits, cs_location)
           VALUES (?,?,?,?,1,1,?,?,?,?)""",
        (
            "showcase-demo",
            "Sample Tasting Menu",
            "Mise Demo",
            "1234",
            "Old tagline",
            "",
            "Client: Mise Demo",
            "",
        ),
    )

    assert bootstrap.ensure_public_showcase() is True
    gallery = db.one(
        "SELECT title, client_name, cs_published, cs_tagline, cs_brief, "
        "cs_credits, cs_location FROM galleries WHERE id=?",
        (gid,),
    )

    assert gallery["title"] == "Seasonal Tasting Menu"
    assert gallery["client_name"] == "Independent Restaurant"
    assert gallery["cs_published"] == 1
    assert gallery["cs_tagline"] == "Old tagline"
    assert "same-week gallery" in gallery["cs_brief"]
    assert "Client: Independent restaurant" in gallery["cs_credits"]
    assert gallery["cs_location"] == "Western North Carolina"


@pytest.mark.unit
def test_access_routes_use_shared_session_cookie_policy(client, monkeypatch):
    from http.cookies import SimpleCookie

    from app import config, db, gcal, security

    def morsel(response, name: str):
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        return cookie[name]

    def assert_session_cookie(response, name: str, max_age: int = config.SESSION_MAX_AGE):
        m = morsel(response, name)
        assert m["max-age"] == str(max_age)
        assert m["path"] == "/"
        assert m["httponly"]
        assert m["samesite"].lower() == "lax"
        return m

    admin = client.post(
        "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
    )
    assert admin.status_code == 303
    assert security.unsign(assert_session_cookie(admin, security.ADMIN_COOKIE).value) == "admin"

    db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        ("cookie-gallery", "Cookie Gallery", "1234"),
    )
    gallery = db.one("SELECT * FROM galleries WHERE slug='cookie-gallery'")
    gallery_pin = client.post("/g/cookie-gallery/pin", data={"pin": "1234"}, follow_redirects=False)
    assert gallery_pin.status_code == 303
    assert security.unsign(assert_session_cookie(gallery_pin, f"mise_g{gallery['id']}").value)

    client_id = db.run(
        "INSERT INTO clients (name, company, email) VALUES (?,?,?)",
        ("Cookie Client", "Cookie Co", "client@example.test"),
    )
    portal_id = db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (client_id, "cookie-portal", "2468"),
    )
    portal_pin = client.post(
        "/portal/cookie-portal/pin", data={"pin": "2468"}, follow_redirects=False
    )
    assert portal_pin.status_code == 303
    assert security.unsign(assert_session_cookie(portal_pin, f"mise_p{portal_id}").value) == (
        f"portal:{portal_id}"
    )

    project_id = db.run(
        """INSERT INTO projects
           (client_id, title, workspace_slug, workspace_pin, workspace_published)
           VALUES (?,?,?,?,1)""",
        (client_id, "Cookie Project", "cookie-workspace", "1357"),
    )
    workspace_pin = client.post(
        "/w/cookie-workspace/pin", data={"pin": "1357"}, follow_redirects=False
    )
    assert workspace_pin.status_code == 303
    assert security.unsign(assert_session_cookie(workspace_pin, f"mise_w{project_id}").value) == (
        f"workspace:{project_id}"
    )

    monkeypatch.setattr(gcal, "configured", lambda: True)
    monkeypatch.setattr(
        gcal, "auth_url", lambda state: f"https://accounts.example/auth?state={state}"
    )
    oauth = client.get("/admin/scheduling/google/connect", follow_redirects=False)
    assert oauth.status_code == 303
    assert assert_session_cookie(oauth, "g_oauth_state", max_age=600).value


@pytest.mark.unit
def test_custom_forms_are_public_rate_limited():
    from app import ratelimit

    assert ratelimit._bucket_for("/forms/wedding-lead") == "public"
    assert ratelimit._bucket_for("/static/mise.css") is None
    # contracts, workspace, and testimonials are now metered like every sibling
    # public route (they were unmetered)
    assert ratelimit._bucket_for("/c/abc123def456") == "public"
    assert ratelimit._bucket_for("/w/abc123def456") == "public"
    assert ratelimit._bucket_for("/t/abc123def456") == "public"
    # /work/ marketing pages stay exempt — "/w/" must not swallow them
    assert ratelimit._bucket_for("/work/spring-menu") is None


@pytest.mark.unit
def test_static_assets_cached_immutable(client):
    # /static URLs are content-hash-busted (?v=), so they're safe to cache
    # forever; the middleware must stamp the long-lived immutable header.
    r = client.get("/static/mise.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    # non-static responses must NOT get the immutable header
    assert "immutable" not in client.get("/healthz").headers.get("cache-control", "")


@pytest.mark.unit
def test_favicon(client):
    # legacy crawlers and share scrapers request /favicon.ico directly,
    # ignoring the <link rel=icon> tags — it must not 404
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    home = client.get("/").text
    assert 'rel="apple-touch-icon"' in home and "/static/favicon.svg" in home


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
