"""Application integration tests extracted from the full smoke suite.

These exercise TestClient, application lifecycle, and the test database.
"""

import os
import tempfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.integration
def test_healthz(client):
    r = client.get("/healthz")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["db_connected"] is True
    assert isinstance(body["jobs_pending"], int)
    assert isinstance(body["jobs_failed"], int)
    assert isinstance(body["disk_free_gb"], float)
    assert isinstance(body["backup_present"], bool)
    assert "backup_age_hours" in body


@pytest.mark.integration
def test_healthz_returns_503_only_for_database_failure(client, monkeypatch):
    from app import db

    real_one = db.one

    def fail_probe(sql, params=()):
        if sql == "SELECT 1 AS ok":
            raise OSError("database unavailable")
        return real_one(sql, params)

    monkeypatch.setattr(db, "one", fail_probe)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert r.json()["db_connected"] is False


@pytest.mark.integration
def test_healthz_reports_storage_warning_without_failing(client, monkeypatch):
    from app import ops_monitor

    monkeypatch.setattr(
        ops_monitor,
        "storage_status",
        lambda: {
            "disk_free_gb": 0.25,
            "disk_low": True,
            "backup_present": False,
            "backup_age_hours": None,
            "backup_stale": True,
        },
    )
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["disk_low"] is True
    assert r.json()["backup_stale"] is True


@pytest.mark.integration
def test_static_revision_cache_refreshes_within_ttl(tmp_path, monkeypatch):
    import os

    from app import render

    static = tmp_path / "static"
    static.mkdir()
    asset = static / "site.js"
    asset.write_text("one")
    os.utime(asset, (100, 100))
    clock = [10.0]
    monkeypatch.setattr(render, "ROOT", tmp_path)
    monkeypatch.setattr(render.time, "monotonic", lambda: clock[0])
    render._static_rev_cache.update(value=0, expires=0.0)

    assert render._static_rev() == 100
    os.utime(asset, (200, 200))
    assert render._static_rev() == 100
    clock[0] += render._STATIC_REV_TTL_SECONDS
    assert render._static_rev() == 200
    render._static_rev_cache["expires"] = 0.0


@pytest.mark.integration
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


@pytest.mark.integration
def test_hsts_tracks_cookie_secure(monkeypatch):
    from app import config
    from app.main import app

    # HSTS ships only when the site knows it's on TLS (same signal as Secure
    # cookies) — never for a plain-http dev origin, which would pin localhost.
    # Keep the first production rollout short and root-only so it is reversible.
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    with TestClient(app) as c:
        h = c.get("/healthz").headers["strict-transport-security"]
        assert h == "max-age=300"
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    with TestClient(app) as c:
        assert "strict-transport-security" not in c.get("/healthz").headers


@pytest.mark.integration
def test_cookie_secure_default_fails_safe_for_https():
    from app import config

    # an https base auto-enables Secure cookies; plain-http (dev) stays off —
    # so production can't silently ship insecure cookies by forgetting a flag
    assert config._cookie_secure_default("https://kleephotography.com") == "true"
    assert config._cookie_secure_default("http://localhost:8400") == "false"


@pytest.mark.integration
def test_admin_logout_revokes_session(client):
    from app import config, db, security

    client.cookies.clear()
    r = client.post(
        "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303
    raw = r.cookies.get(security.ADMIN_COOKIE)
    token = security.unsign(raw)
    assert token and db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (token,))
    # authenticated request passes the require_admin gate
    assert client.get("/admin/home", follow_redirects=False).status_code == 200
    # logout deletes the server-side row → real revocation
    assert client.post("/admin/logout", follow_redirects=False).status_code == 303
    assert db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (token,)) is None
    # replaying the OLD signed cookie is now dead — bounced to login, not admitted
    client.cookies.clear()
    client.cookies.set(security.ADMIN_COOKIE, raw)
    r = client.get("/admin/home", follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers.get("location", "")
    client.cookies.clear()


@pytest.mark.integration
def test_admin_sign_out_everywhere_requires_auth(client):
    from app import db, security

    other = security.create_admin_session()
    client.cookies.clear()
    try:
        before = db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"]
        r = client.post("/admin/logout", data={"everywhere": "1"}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"
        assert db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"] == before
        assert db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (other,))
    finally:
        db.run("DELETE FROM admin_sessions WHERE token=?", (other,))


@pytest.mark.integration
def test_admin_sign_out_everywhere_kills_all_sessions(client):
    from app import config, db, security

    # a second, independent session (another device) plus this browser's login
    other = security.create_admin_session()
    client.cookies.clear()
    client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"] >= 2
    # "sign out everywhere" revokes ALL sessions, including the other device's
    r = client.post("/admin/logout", data={"everywhere": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM admin_sessions")["n"] == 0
    assert db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (other,)) is None
    client.cookies.clear()


@pytest.mark.integration
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


@pytest.mark.integration
def test_pin_target_circuit_breaker(client, monkeypatch):
    from app import config, db, security

    monkeypatch.setattr(config, "PIN_MAX_FAILS", 5)
    monkeypatch.setattr(config, "PIN_TARGET_MAX_FAILS", 10)
    monkeypatch.setattr(config, "PIN_TARGET_WINDOW_MIN", 60)

    gid = 987654
    db.run("DELETE FROM pin_attempts WHERE gallery_id IN (?, 0)", (gid,))
    try:
        # 10 failures spread across 10 distinct IPs — no single IP hits the
        # per-IP cap of 5, so the old per-IP check alone would never lock this.
        for i in range(10):
            security.pin_fail(f"10.0.0.{i}", gid)
        assert security.pin_locked("10.0.0.3", gid) is True  # a participating IP
        # a brand-new IP that never guessed is ALSO locked — the distributed
        # defense is the whole point
        assert security.pin_locked("203.0.113.99", gid) is True
        # below the target threshold, a fresh IP is NOT locked
        db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (gid,))
        for i in range(9):
            security.pin_fail(f"10.0.2.{i}", gid)
        assert security.pin_locked("203.0.113.99", gid) is False

        # admin login (bucket 0) is EXEMPT from the global cap — a strong password
        # isn't PIN-brute-forceable and a global lock there would just DoS Kevin
        for i in range(20):
            security.pin_fail(f"10.0.3.{i}", 0)
        assert security.pin_locked("203.0.113.99", 0) is False
    finally:
        db.run("DELETE FROM pin_attempts WHERE gallery_id IN (?, 0)", (gid,))


@pytest.mark.integration
def test_check_admin_password_handles_non_ascii(monkeypatch):
    from app import config, security

    # a non-ASCII password attempt must return False, not raise TypeError (which
    # would 500, fire an alert, and skip the login lockout counter)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "correct-horse")
    assert security.check_admin_password("pässwörd") is False
    assert security.check_admin_password("correct-horse") is True
    assert security.check_admin_password("naïve🔑") is False


@pytest.mark.integration
def test_csp_header(client):
    # Content-Security-Policy ships on every response as XSS defense-in-depth
    # (R18). script-src has NO 'unsafe-inline' — inline handlers moved to
    # /static/behaviors.js and the remaining inline blocks carry a nonce.
    csp = client.get("/healthz").headers["content-security-policy"]
    for needed in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert needed in csp, needed
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src "))
    assert "'unsafe-inline'" not in script_src
    assert "'nonce-" in script_src
    # style-src deliberately keeps 'unsafe-inline' (inline style= attributes are
    # pervasive and style injection is a far weaker vector than script)
    style_src = next(d for d in csp.split("; ") if d.startswith("style-src "))
    assert "'unsafe-inline'" in style_src
    # analytics is the only off-origin asset, allowed for script + connect
    assert "https://plausible.io" in csp
    # indexable marketing pages carry the policy too
    assert "content-security-policy" in client.get("/").headers


@pytest.mark.integration
def test_csp_nonce_fresh_and_matches_markup(client):
    import re

    # The header nonce must be echoed on the page's inline <script> blocks —
    # a mismatch means every inline script dies silently in the browser.
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{nonce}">' in r.text
    # and no inline block may ship without its nonce
    assert "<script>" not in r.text
    # nonces are per-request secrets, never reused
    r2 = client.get("/")
    nonce2 = re.search(r"'nonce-([^']+)'", r2.headers["content-security-policy"]).group(1)
    assert nonce2 != nonce
    # the pre-auth admin login renders nonce-clean too
    assert "<script>" not in client.get("/admin/login").text


@pytest.mark.integration
def test_permissions_policy_header(client):
    # unused browser features are switched off on every response; fullscreen and
    # picture-in-picture are deliberately NOT restricted (native <video> controls
    # in galleries/reels depend on them)
    pp = client.get("/healthz").headers["permissions-policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert feature in pp, feature
    for kept in ("fullscreen", "picture-in-picture", "autoplay"):
        assert kept not in pp, kept
    assert "permissions-policy" in client.get("/").headers


@pytest.mark.integration
def test_security_txt(client):
    # RFC 9116: Contact + a not-yet-expired Expires, served as text/plain
    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert body.startswith("Contact: ")
    expires = next(ln for ln in body.splitlines() if ln.startswith("Expires: "))
    from datetime import datetime

    exp = datetime.strptime(expires.removeprefix("Expires: "), "%Y-%m-%dT%H:%M:%SZ")
    assert exp.replace(tzinfo=UTC) > datetime.now(UTC)


@pytest.mark.integration
def test_invoice_stripe_return_banner(client, monkeypatch):
    from app import config, db

    # Back from Stripe Checkout with ?thanks=1 but the webhook still in flight,
    # the invoice must reassure and HIDE the Pay button (a live button moments
    # after paying invites a double charge) — while never touching payment state.
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)", ("Return UX", "r@ux.test"))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Shoot"))
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status)
           VALUES (?,?,?,?,?,?)""",
        (pid, "thanks-ux", "Shoot", "[]", 100000, "sent"),
    )
    try:
        # a plain visit shows the live Pay button and no banner
        page = client.get("/i/thanks-ux").text
        assert "client-pay-btn" in page and "client-doc-thanks" not in page
        # returning with ?thanks=1 pre-webhook: banner + auto-refresh, no Pay
        # button — and the invoice is NOT marked paid by the query param
        page = client.get("/i/thanks-ux?thanks=1").text
        assert "client-doc-thanks" in page and "client-pay-btn" not in page
        assert 'http-equiv="refresh"' in page
        assert db.one("SELECT status FROM invoices WHERE id=?", (iid,))["status"] == "viewed"
        # once the webhook records the payment (deposit here), the banner yields
        # to the real state — deposit confirmed, balance button live again
        db.run(
            """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                  amount_cents, kind) VALUES (?,?,?,?,?)""",
            (iid, "evt_ux_dep", "cs_ux_dep", 40000, "deposit"),
        )
        db.run("UPDATE invoices SET status='deposit_paid', deposit_cents=40000 WHERE id=?", (iid,))
        page = client.get("/i/thanks-ux?thanks=1").text
        assert "client-doc-thanks" not in page and "Deposit received" in page
        assert "client-pay-btn" in page  # balance is genuinely still due
        # fully paid: normal paid state even with a stale ?thanks=1 bookmark
        db.run("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?", (iid,))
        page = client.get("/i/thanks-ux?thanks=1").text
        assert "client-doc-thanks" not in page and "Paid in full" in page
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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
    # the admin cookie now carries a per-login server-side session token (not a
    # signed constant); it must resolve to a live row in admin_sessions
    admin_token = security.unsign(assert_session_cookie(admin, security.ADMIN_COOKIE).value)
    assert admin_token and db.one("SELECT 1 AS x FROM admin_sessions WHERE token=?", (admin_token,))

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


@pytest.mark.integration
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


@pytest.mark.integration
def test_static_asset_cache_and_font_preload_identity(client):
    # Versioned top-level assets retain the long-lived immutable policy.
    r = client.get("/static/mise.css?v=test")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    fonts_css_response = client.get("/static/fonts.css?v=test")
    assert fonts_css_response.status_code == 200
    assert fonts_css_response.headers["cache-control"] == r.headers["cache-control"]
    fonts_css = fonts_css_response.text

    # Font filenames are stable and unversioned in fonts.css. They must revalidate
    # on a short cadence, and preload URLs must be byte-for-byte identical so the
    # browser can reuse each preload for the later @font-face request.
    font_urls = (
        "/static/fonts/newsreader-latin.woff2",
        "/static/fonts/archivo-latin.woff2",
    )
    for url in font_urls:
        font = client.get(url)
        assert font.status_code == 200
        assert font.headers["cache-control"] == "public, max-age=86400"
        assert "immutable" not in font.headers["cache-control"]
        assert f"url({url})" in fonts_css

    def assert_font_preloads(page):
        assert page.count('rel="preload" href="/static/fonts/') == len(font_urls)
        for url in font_urls:
            tag = f'<link rel="preload" href="{url}" as="font" type="font/woff2" crossorigin>'
            assert page.count(tag) == 1

    home = client.get("/").text
    assert_font_preloads(home)

    from app import config

    login = client.post(
        "/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False
    )
    assert login.status_code == 303
    admin = client.get("/admin/home")
    assert admin.status_code == 200
    assert_font_preloads(admin.text)

    # non-static responses must NOT get the immutable header
    assert "immutable" not in client.get("/healthz").headers.get("cache-control", "")


@pytest.mark.integration
def test_favicon(client):
    # legacy crawlers and share scrapers request /favicon.ico directly,
    # ignoring the <link rel=icon> tags — it must not 404
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    home = client.get("/").text
    assert 'rel="apple-touch-icon"' in home and "/static/favicon.svg" in home


@pytest.mark.integration
def test_branded_error_pages(client):
    # clients clicking bad links in a browser get a branded page, not raw JSON
    r = client.get("/g/nope12345678", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "double-check it" in r.text and "text/html" in r.headers["content-type"]
    # programmatic callers (HTMX, zip status polls) keep plain JSON errors
    r = client.get("/g/nope12345678", headers={"accept": "application/json"})
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"


@pytest.mark.integration
def test_admin_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_financials_csv_include_paid_checkbox(client):
    """Unchecking Paid in the export panel must omit paid rows — the old default
    of inc_paid='on' made an absent checkbox still export payments."""
    from app import config, db

    client.post("/admin/login", data={"password": config.ADMIN_PASSWORD}, follow_redirects=False)
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("CSV Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Paid Job"))
    paid_iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status,
           created_at) VALUES (?,?,?,?,?,?,datetime('now'))""",
        (pid, "csv-paid", "Paid Job", "[]", 50000, "paid"),
    )
    out_iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, line_items, total_cents, status,
           created_at) VALUES (?,?,?,?,?,?,datetime('now'))""",
        (pid, "csv-out", "Open Job", "[]", 75000, "sent"),
    )
    db.run(
        """INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
           amount_cents, kind, created_at) VALUES (?,?,?,?,?,datetime('now'))""",
        (paid_iid, "evt_csv", "cs_csv", 50000, "full"),
    )
    try:
        both = client.get("/admin/financials/income.csv?range=ytd&inc_paid=on&inc_out=on").text
        assert "Paid" in both and "Outstanding" in both
        out_only = client.get("/admin/financials/income.csv?range=ytd&inc_out=on").text
        assert "Outstanding" in out_only and "Paid" not in out_only
        paid_only = client.get("/admin/financials/income.csv?range=ytd&inc_paid=on").text
        assert "Paid" in paid_only and "Outstanding" not in paid_only
    finally:
        db.run("DELETE FROM payments WHERE invoice_id=?", (paid_iid,))
        db.run("DELETE FROM invoices WHERE id IN (?,?)", (paid_iid, out_iid))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


@pytest.mark.integration
def test_portal_crop_links_show_preparing_while_jobs_run(client):
    """Social-crop ratio links must not 404 while encode jobs are still running."""
    from app import db

    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Crop UX Co",))
    pid = db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (cid, "crop-ux", "2468"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, client_id) VALUES (?,?,?,1,?)",
        ("crop-ux-g", "Crop UX", "1234", cid),
    )
    aid = db.run(
        """INSERT INTO assets (gallery_id, filename, stored, kind, status)
           VALUES (?,?,?,?,?)""",
        (gid, "hero.jpg", "hero.jpg", "photo", "ready"),
    )
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, "crop-ux-vis"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aid))
    try:
        client.post("/portal/crop-ux/pin", data={"pin": "2468"}, follow_redirects=False)
        page = client.get("/portal/crop-ux").text
        assert "preparing" in page
        assert f"/portal/crop-ux/crop/{aid}/" not in page
    finally:
        db.run("DELETE FROM favorites WHERE visitor_id=?", (vid,))
        db.run("DELETE FROM visitors WHERE id=?", (vid,))
        db.run("DELETE FROM assets WHERE id=?", (aid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
        db.run("DELETE FROM portals WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))
        client.cookies.clear()
