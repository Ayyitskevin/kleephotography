"""End-to-end smoke: gallery lifecycle through the real app against a temp data dir.

Run:  MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
      python -m pytest tests/ -x -q
"""

import io
import os
import tempfile
import zipfile

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/admin/login", data={"password": os.environ["MISE_ADMIN_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # The middleware limiter (app.ratelimit._hits) is a module-global keyed by
    # ip="testclient" for every TestClient, so request counts accrete across the
    # whole file and later tests trip 429 on their first call. Clear it per test so
    # each exercises the limiter from a clean window, exactly as in isolation. The
    # inquiry/PIN throttles live in the DB (pin_attempts) and are reset by the tests
    # that use them, so this only touches the in-process buckets.
    from app import ratelimit
    ratelimit._hits.clear()
    yield


def _jpeg_bytes(w=800, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (180, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


def _logo_png(w=300, h=150, color=(0, 200, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _close(a, b, tol=12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _mp4_bytes(seconds=2, w=128, h=96) -> bytes:
    """A real tiny mp4 via ffmpeg so the transcode pipeline runs for real (no mocks).
    2s long so the poster grab at -ss 1 has a frame to land on."""
    import subprocess
    from pathlib import Path
    path = tempfile.mktemp(suffix=".mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"testsrc=duration={seconds}:size={w}x{h}:rate=10",
             "-pix_fmt", "yuv420p", path],
            check=True, capture_output=True)
        return Path(path).read_bytes()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


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


def test_csp_header(client):
    # Content-Security-Policy ships on every response as XSS/clickjacking
    # defense-in-depth (R18). script-src keeps 'unsafe-inline' for now because the
    # templates use inline handlers; the locked-down directives are the win here.
    csp = client.get("/healthz").headers["content-security-policy"]
    for needed in ("default-src 'self'", "frame-ancestors 'none'",
                   "object-src 'none'", "base-uri 'self'", "form-action 'self'"):
        assert needed in csp, needed
    # analytics is the only off-origin asset, allowed for script + connect
    assert "https://plausible.io" in csp
    # indexable marketing pages carry the policy too
    assert "content-security-policy" in client.get("/").headers


def test_csrf_same_origin_enforced(client):
    from app import config, security
    # a cross-origin state-changing POST is rejected by the guard, before auth
    # even runs — the browser stamps Origin on a malicious cross-site form submit
    r = client.post("/admin/login", data={"password": "x"},
                    headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    # Referer is the fallback signal when Origin is absent
    r = client.post("/admin/login", data={"password": "x"},
                    headers={"referer": "https://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    # a same-origin POST passes the guard (wrong pw -> 401, decidedly NOT 403)
    r = client.post("/admin/login", data={"password": "nope"},
                    headers={"origin": config.BASE_URL}, follow_redirects=False)
    assert r.status_code == 401
    # server-to-server webhooks send no Origin/Referer and stay unaffected
    # (503 = not configured in tests; the point is it is NOT a 403)
    r = client.post("/webhooks/stripe", content=b"{}")
    assert r.status_code == 503
    security.pin_clear("testclient", 0)  # drop the failed-login bookkeeping


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
    alerts.error_alert(sig, "third")   # within window -> suppressed
    assert len(sent) == 1 and "first" in sent[0]
    assert alerts._error_suppressed[sig] == 2

    # once the window passes, the next alert sends AND reports the swallowed count
    alerts._error_last[sig] = 0.0
    alerts.error_alert(sig, "later")
    assert len(sent) == 2 and "+2 more" in sent[1]


def test_unhandled_exception_alerts_and_500(monkeypatch):
    # an uncaught exception returns a clean 500 (branded HTML / plain JSON, no leak)
    # AND fires a crash alert — the whole point of in-app monitoring.
    from app import alerts

    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "error_alert",
                        lambda sig, text: fired.append((sig, text)))

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
        app.router.routes = [rt for rt in app.router.routes
                             if getattr(rt, "path", None) != "/__test_boom"]
    assert fired and "RuntimeError" in fired[0][0]


def test_branded_error_pages(client):
    # clients clicking bad links in a browser get a branded page, not raw JSON
    r = client.get("/g/nope12345678", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "double-check it" in r.text and "text/html" in r.headers["content-type"]
    # programmatic callers (HTMX, zip status polls) keep plain JSON errors
    r = client.get("/g/nope12345678", headers={"accept": "application/json"})
    assert r.status_code == 404 and r.json()["detail"] == "Not Found"


def test_admin_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303


def test_full_gallery_flow(admin):
    # create
    r = admin.post("/admin/galleries", data={"title": "Test Bistro", "client_name": "Chef"},
                   follow_redirects=False)
    assert r.status_code == 303
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    assert g["title"] == "Test Bistro" and len(g["slug"]) >= 12

    # upload a photo
    r = admin.post(f"/admin/galleries/{g['id']}/upload",
                   files=[("files", ("dish.jpg", _jpeg_bytes(), "image/jpeg"))])
    assert r.status_code == 200 and r.json()["accepted"] == 1

    # wait for derivative job
    import time
    for _ in range(50):
        a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
        if a["status"] == "ready":
            break
        time.sleep(0.2)
    assert a["status"] == "ready" and a["width"] == 800

    # publish with PIN
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": "Test Bistro", "pin": "1234", "published": "true"})

    # set cover from the asset grid; nonexistent asset 404s
    assert admin.post(f"/admin/galleries/{g['id']}/assets/999999/cover").status_code == 404
    r = admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/cover",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT cover_asset_id FROM galleries WHERE id=?",
                  (g["id"],))["cover_asset_id"] == a["id"]
    # saving settings must NOT wipe the cover (field no longer in the form)
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": "Test Bistro", "pin": "1234", "published": "true"})
    assert db.one("SELECT cover_asset_id FROM galleries WHERE id=?",
                  (g["id"],))["cover_asset_id"] == a["id"]

    # public flow in a fresh client (no admin cookie)
    with TestClient(app) as pub:
        # unpublished slug 404s — wrong slug
        assert pub.get("/g/nope12345678").status_code == 404
        # PIN page
        r = pub.get(f"/g/{g['slug']}")
        assert r.status_code == 200 and "PIN" in r.text
        # wrong PIN
        r = pub.post(f"/g/{g['slug']}/pin", data={"pin": "0000"})
        assert r.status_code == 401
        # right PIN
        r = pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        assert r.status_code == 303
        # gallery renders with tile
        r = pub.get(f"/g/{g['slug']}")
        assert r.status_code == 200 and f"/media/{g['slug']}/thumb/{a['id']}" in r.text
        # cover renders as the hero background
        assert f"background-image:url('/media/{g['slug']}/web/{a['id']}')" in r.text
        # tiles carry the endpoints the lightbox action bar drives via fetch()
        assert f'data-fav="/g/{g["slug"]}/fav/{a["id"]}"' in r.text
        assert f'data-dl="/g/{g["slug"]}/download?asset_id={a["id"]}"' in r.text
        assert 'class="lb-fav"' in r.text and 'class="lb-dl"' in r.text
        assert 'class="lb-play"' in r.text
        # lb-proof slot ships hidden (lightbox shows it when in a proofing section)
        assert 'class="lb-proof" hidden' in r.text
        # the tile carries its section id so the lightbox can read the live
        # progress label out of the section header
        if a["section_id"]:
            assert f'data-section="{a["section_id"]}"' in r.text
        # static URLs are cache-busted — CF edge caches /static/ for hours
        assert "/static/lightbox.js?v=" in r.text and "/static/mise.css?v=" in r.text
        # media serves
        assert pub.get(f"/media/{g['slug']}/thumb/{a['id']}").status_code == 200
        assert pub.get(f"/media/{g['slug']}/web/{a['id']}").status_code == 200
        # Range request honored
        r = pub.get(f"/media/{g['slug']}/original/{a['id']}",
                    headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        # favorite toggle
        r = pub.post(f"/g/{g['slug']}/fav/{a['id']}")
        assert r.status_code == 200 and "faved" in r.text
        # download requires email
        r = pub.get(f"/g/{g['slug']}/download/asset/{a['id']}", follow_redirects=False)
        assert r.status_code == 303
        # email-gate page carries a "Have a question?" deep link to /contact
        # with the gallery name prefilled (ship #44)
        gate = pub.get(f"/g/{g['slug']}/download", follow_redirects=True)
        assert "Have a question" in gate.text
        assert "prefill=gallery_question" in gate.text
        # email gate
        r = pub.post(f"/g/{g['slug']}/email",
                     data={"email": "chef@bistro.com", "asset_id": str(a["id"])},
                     follow_redirects=False)
        assert r.status_code == 303
        # single download works now
        r = pub.get(f"/g/{g['slug']}/download/asset/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
        # ZIP: first request enqueues, then becomes ready
        r = pub.get(f"/g/{g['slug']}/download/zip")
        assert r.status_code == 200
        for _ in range(50):
            if pub.get(f"/g/{g['slug']}/download/zip/status").json()["ready"]:
                break
            time.sleep(0.2)
        r = pub.get(f"/g/{g['slug']}/download/zip")
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.namelist() == ["dish.jpg"]
        # favorites ZIP: header button shows, bundle holds exactly the faved original
        page = pub.get(f"/g/{g['slug']}").text
        assert f"/g/{g['slug']}/download/favorites" in page and "Favorites (1)" in page
        r = pub.get(f"/g/{g['slug']}/download/favorites")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == ["dish.jpg"]
        assert "favorites.zip" in r.headers["content-disposition"]
        # unfavoriting empties the bundle → 404, button gone
        pub.post(f"/g/{g['slug']}/fav/{a['id']}")
        assert pub.get(f"/g/{g['slug']}/download/favorites").status_code == 404
        assert "Favorites (" not in pub.get(f"/g/{g['slug']}").text
        # re-fav so downstream assertions (admin ♥ badge, crops) still hold
        pub.post(f"/g/{g['slug']}/fav/{a['id']}")

    # the client favorite surfaces on the admin grid (♥ tile badge + proofing count)
    page = admin.get(f"/admin/galleries/{g['id']}").text
    assert "&#9829;" in page and "1 favorited" in page

    # second click toggles the cover off
    admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/cover")
    assert db.one("SELECT cover_asset_id FROM galleries WHERE id=?",
                  (g["id"],))["cover_asset_id"] is None

    # activity recorded
    rows = db.all_("SELECT * FROM downloads WHERE gallery_id=?", (g["id"],))
    assert len(rows) == 3  # single + zip + favorites zip
    v = db.one("SELECT * FROM visitors WHERE gallery_id=?", (g["id"],))
    assert v["email"] == "chef@bistro.com"


def test_video_pipeline_full_flow(admin):
    """Locks the previously-untested video path end-to-end: a .mp4 upload routes to
    kind='video' + a transcode job; ffmpeg produces the web mp4 + poster + thumb and
    the asset goes ready with probed dims/duration; the gallery renders a video tile
    and media serves the mp4 with HTTP Range (iOS scrubbing) plus the poster frame."""
    import time
    from pathlib import Path

    from app import config

    r = admin.post("/admin/galleries",
                   data={"title": "Test Kitchen Reel", "client_name": "Chef"},
                   follow_redirects=False)
    assert r.status_code == 303
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")

    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        # upload routes a .mp4 to kind='video' (image extensions would be kind='photo')
        r = admin.post(f"/admin/galleries/{g['id']}/upload",
                       files=[("files", ("reel.mp4", _mp4_bytes(), "video/mp4"))])
        assert r.status_code == 200 and r.json()["accepted"] == 1
        a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
        assert a["kind"] == "video"

        # the transcode job runs for real: ffmpeg web mp4 + poster, then ready with
        # the metadata ffprobe read back off the transcoded file
        for _ in range(100):
            a = db.one("SELECT * FROM assets WHERE id=?", (a["id"],))
            if a["status"] == "ready":
                break
            time.sleep(0.2)
        assert a["status"] == "ready"
    assert a["width"] == 128 and a["height"] == 96
    assert a["duration"] and a["duration"] > 0

    # derivatives landed on disk: web mp4, poster jpg, and the grid thumb
    base = config.MEDIA_DIR / str(g["id"])
    stem = Path(a["stored"]).stem
    assert (base / "web" / f"{stem}.mp4").is_file()
    assert (base / "web" / f"{stem}_poster.jpg").is_file()
    assert (base / "thumb" / f"{stem}.jpg").is_file()

    # publish + PIN
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": "Test Kitchen Reel", "pin": "1234", "published": "true"})

    with TestClient(app) as pub:
        r = pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        assert r.status_code == 303
        page = pub.get(f"/g/{g['slug']}").text
        # the video tile carries kind, the poster + web endpoints, and the play badge
        assert 'data-kind="video"' in page
        assert f'data-poster="/media/{g["slug"]}/poster/{a["id"]}"' in page
        assert f'data-web="/media/{g["slug"]}/web/{a["id"]}"' in page
        assert 'class="play-badge"' in page

        # web variant serves the transcoded mp4 (not a jpg) with the video mime type
        r = pub.get(f"/media/{g['slug']}/web/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        # Range honored — iOS scrubbing depends on 206 partial content
        r = pub.get(f"/media/{g['slug']}/web/{a['id']}",
                    headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        # grid thumb serves as a jpeg
        r = pub.get(f"/media/{g['slug']}/thumb/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        # poster frame serves as a jpeg; the poster route is video-only
        r = pub.get(f"/media/{g['slug']}/poster/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert pub.get(f"/media/{g['slug']}/poster/999999").status_code == 404


def _ready_video(admin, title="Reel Review", pin="1234"):
    """Create a published gallery with one ready video + one photo; return
    (gallery_row, video_asset_row, photo_asset_row). Shared setup for the
    comment tests — uses the real ffmpeg transcode path like the pipeline test."""
    import time

    admin.post("/admin/galleries", data={"title": title}, follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        admin.post(f"/admin/galleries/{g['id']}/upload",
                   files=[("files", ("reel.mp4", _mp4_bytes(), "video/mp4"))])
        admin.post(f"/admin/galleries/{g['id']}/upload",
                   files=[("files", ("dish.jpg", _jpeg_bytes(), "image/jpeg"))])
        for _ in range(100):
            assets = db.all_("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
            if assets and all(a["status"] == "ready" for a in assets):
                break
            time.sleep(0.2)
    vid = db.one("SELECT * FROM assets WHERE gallery_id=? AND kind='video'", (g["id"],))
    photo = db.one("SELECT * FROM assets WHERE gallery_id=? AND kind='photo'", (g["id"],))
    assert vid and vid["status"] == "ready" and photo
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": title, "pin": pin, "published": "true"})
    return g, vid, photo


def test_video_comments_flow(admin):
    """C3: timecoded review comments on a video deliverable. A PIN-gated client
    leaves a note anchored to a playhead second; replies thread under it and
    inherit the parent's timecode; the admin authors + threads too; admin hide
    soft-deletes the comment AND its replies and writes one audit row. The gate
    rejects non-visitors and non-video assets."""
    g, vid, photo = _ready_video(admin, title="Reel Review A")

    # the client gallery page ships the lightbox comment wiring
    with TestClient(app) as pub:
        assert pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"},
                        follow_redirects=False).status_code == 303
        page = pub.get(f"/g/{g['slug']}").text
        assert f'data-slug="{g["slug"]}"' in page
        assert 'class="lb-comments"' in page

        # client posts a top-level note anchored at 12.5s → persists with that timecode
        r = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                     data={"body": "Tighten this cut", "timecode": 12.5})
        assert r.status_code == 200
        thread = r.json()
        assert len(thread) == 1
        top = thread[0]
        assert top["timecode"] == 12.5 and top["author_role"] == "client"
        assert top["body"] == "Tighten this cut" and top["parent_id"] is None

        # a reply inherits the parent's timecode (ignores any posted timecode)
        r = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                     data={"body": "agreed", "parent_id": top["id"], "timecode": 99})
        assert r.status_code == 200
        reply = next(c for c in r.json() if c["parent_id"] == top["id"])
        assert reply["timecode"] == 12.5  # inherited, not 99

        # GET returns the visible thread (both rows), ordered for display
        got = pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()
        assert {c["id"] for c in got} == {top["id"], reply["id"]}

        # gate: empty body 400, bogus reply target 400, photo/asset are not video → 404
        assert pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                        data={"body": "   "}).status_code == 400
        assert pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                        data={"body": "x", "parent_id": 999999}).status_code == 400
        assert pub.get(f"/g/{g['slug']}/comments/{photo['id']}").status_code == 404
        assert pub.post(f"/g/{g['slug']}/comments/{photo['id']}",
                        data={"body": "x"}).status_code == 404

    # gate: a visitor cookie is required (no PIN → 403 on read and write)
    with TestClient(app) as anon:
        assert anon.get(f"/g/{g['slug']}/comments/{vid['id']}").status_code == 403
        assert anon.post(f"/g/{g['slug']}/comments/{vid['id']}",
                         data={"body": "x"}).status_code == 403

    # admin authors a studio note + a reply; both visible in the shared thread
    assert admin.post(f"/admin/galleries/{g['id']}/comments/{vid['id']}",
                      data={"body": "Color is warm here", "timecode": 5},
                      follow_redirects=False).status_code == 303
    studio = db.one("SELECT * FROM video_comments WHERE asset_id=? AND author_role='admin'",
                    (vid["id"],))
    assert studio and studio["timecode"] == 5.0
    assert admin.post(f"/admin/galleries/{g['id']}/comments/{vid['id']}",
                      data={"body": "reply under client note", "parent_id": top["id"]},
                      follow_redirects=False).status_code == 303

    # admin hide cascades to descendants + writes exactly one audit row
    n_before = db.one("SELECT COUNT(*) AS n FROM video_comments "
                      "WHERE asset_id=? AND deleted_at IS NULL", (vid["id"],))["n"]
    audit_before = db.one("SELECT COUNT(*) AS n FROM audit_log "
                          "WHERE entity_type='video_comment'")["n"]
    assert admin.post(f"/admin/comments/{top['id']}/hide",
                      follow_redirects=False).status_code == 303
    # top + its client reply + the admin reply under it all hidden together
    visible = db.all_("SELECT id FROM video_comments WHERE asset_id=? AND deleted_at IS NULL",
                      (vid["id"],))
    vis_ids = {r["id"] for r in visible}
    assert top["id"] not in vis_ids and reply["id"] not in vis_ids
    assert studio["id"] in vis_ids  # a sibling thread is untouched
    assert len(vis_ids) == n_before - 3
    audit_after = db.one("SELECT COUNT(*) AS n FROM audit_log "
                         "WHERE entity_type='video_comment' AND action='hide'")["n"]
    assert audit_after == audit_before + 1
    # hidden comments drop from the client-facing thread too
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        got = pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()
        assert top["id"] not in {c["id"] for c in got}

    # bogus comment id → 404
    assert admin.post("/admin/comments/999999/hide",
                      follow_redirects=False).status_code == 404


def test_video_comment_resolve_flow(admin):
    """C4: admin-only open⇄resolved state machine on the comment wall. New comments
    are born 'open'; resolving a thread root cascades 'resolved' to its replies (a
    sibling thread untouched); reopen flips back. Illegal transitions (resolve an
    already-resolved root, reopen an already-open), a non-root target, a hidden
    comment, and bogus ids are all rejected server-side. Each admin transition
    writes exactly one audit row."""
    g, vid, photo = _ready_video(admin, title="Reel Review B")

    # client builds a thread (root + reply) and a separate sibling thread
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        # the wall ships the resolve filter/count UI
        assert 'class="vc-filter"' in pub.get(f"/g/{g['slug']}").text
        root = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                        data={"body": "Re-grade the intro", "timecode": 3}).json()[0]
        # a new comment is born 'open' and the status field surfaces client-side
        assert root["status"] == "open"
        pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                 data={"body": "esp. the first shot", "parent_id": root["id"]})
        sib = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                       data={"body": "Audio dips at the end", "timecode": 20}).json()
        sibling = next(c for c in sib if c["parent_id"] is None and c["id"] != root["id"])

    reply = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))
    # DB default really is 'open'
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "open"

    # resolve the root → cascades to the reply; sibling thread untouched; +1 audit
    a0 = db.one("SELECT COUNT(*) AS n FROM audit_log "
                "WHERE entity_type='video_comment' AND action='resolved'")["n"]
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 303
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "resolved"
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (reply["id"],))["status"] == "resolved"
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (sibling["id"],))["status"] == "open"
    a1 = db.one("SELECT COUNT(*) AS n FROM audit_log "
                "WHERE entity_type='video_comment' AND action='resolved'")["n"]
    assert a1 == a0 + 1

    # client thread reflects resolved status (render extends; server-proven)
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        got = {c["id"]: c["status"]
               for c in pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()}
        assert got[root["id"]] == "resolved" and got[reply["id"]] == "resolved"
        assert got[sibling["id"]] == "open"

    # illegal transitions rejected: re-resolve → 409, reopen an open one → 409
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 409
    assert admin.post(f"/admin/comments/{sibling['id']}/reopen",
                      follow_redirects=False).status_code == 409
    # a reply (non-root) is not independently transitionable → 400
    assert admin.post(f"/admin/comments/{reply['id']}/resolve",
                      follow_redirects=False).status_code == 400

    # reopen the root → cascades back to open; +1 audit (action='open')
    o0 = db.one("SELECT COUNT(*) AS n FROM audit_log "
                "WHERE entity_type='video_comment' AND action='open'")["n"]
    assert admin.post(f"/admin/comments/{root['id']}/reopen",
                      follow_redirects=False).status_code == 303
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "open"
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (reply["id"],))["status"] == "open"
    o1 = db.one("SELECT COUNT(*) AS n FROM audit_log "
                "WHERE entity_type='video_comment' AND action='open'")["n"]
    assert o1 == o0 + 1

    # hide × status orthogonal: a hidden comment is out of the workflow → 404
    assert admin.post(f"/admin/comments/{sibling['id']}/hide",
                      follow_redirects=False).status_code == 303
    assert admin.post(f"/admin/comments/{sibling['id']}/resolve",
                      follow_redirects=False).status_code == 404

    # bogus comment id → 404 on both transitions
    assert admin.post("/admin/comments/999999/resolve",
                      follow_redirects=False).status_code == 404
    assert admin.post("/admin/comments/999999/reopen",
                      follow_redirects=False).status_code == 404


def test_video_comment_reply_reopen_flow(admin):
    """Seam closer: a client reply auto-reopens a thread the studio resolved
    (transitions are admin-only, so a reply is the client's only pushback). The
    whole thread cascades back to open and a system-attributed audit row records
    the triggering reply. A reply on an already-open thread is a status no-op, and
    the admin transition guards survive the shared-cascade extraction."""
    import json

    g, vid, photo = _ready_video(admin, title="Reel Review C")

    # client builds a thread (root + reply) and a separate sibling thread
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        root = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                        data={"body": "Re-grade the intro", "timecode": 3}).json()[0]
        pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                 data={"body": "first shot esp.", "parent_id": root["id"]})
        sib = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                       data={"body": "Audio dips at the end", "timecode": 20}).json()
        sibling = next(c for c in sib if c["parent_id"] is None and c["id"] != root["id"])
    child = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))

    # admin resolves the root thread (cascades 'resolved' to root + child)
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 303
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "resolved"

    # ── a client reply on the RESOLVED thread reopens the whole thread ───────
    sys0 = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
                  "AND action='open' AND actor='system'")["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        # reply to the CHILD (depth 2) — the upward walk must still reach the root
        thread = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                          data={"body": "still too warm", "parent_id": child["id"],
                                "timecode": 99}).json()
    new_reply = db.one("SELECT * FROM video_comments WHERE body='still too warm'")
    # C3 not regressed: a reply still inherits its parent's timecode (child @3, not 99)
    assert new_reply["timecode"] == child["timecode"]
    # whole thread back to open — root, the original child, and the new reply
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "open"
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (child["id"],))["status"] == "open"
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (new_reply["id"],))["status"] == "open"
    # the sibling thread never changed
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (sibling["id"],))["status"] == "open"
    # the open-count signal: the client thread shows the root open again
    assert {c["id"]: c["status"] for c in thread}[root["id"]] == "open"
    # exactly one system-attributed audit row, naming the triggering reply
    sys_rows = db.all_("SELECT * FROM audit_log WHERE entity_type='video_comment' "
                       "AND action='open' AND actor='system' ORDER BY id")
    assert len(sys_rows) == sys0 + 1
    last = sys_rows[-1]
    assert last["entity_id"] == root["id"]
    diff = json.loads(last["diff_json"])
    assert diff["from"] == "resolved" and diff["to"] == "open"
    assert diff["cause_reply_id"] == new_reply["id"]

    # ── a reply on an already-OPEN thread is a status no-op (no row written) ──
    sys_a = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
                   "AND action='open' AND actor='system'")["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                 data={"body": "one more on audio", "parent_id": sibling["id"]})
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (sibling["id"],))["status"] == "open"
    sys_b = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
                   "AND action='open' AND actor='system'")["n"]
    assert sys_b == sys_a  # no spurious reopen audit row

    # ── admin transition guards still intact after the helper extraction ─────
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 303   # open → resolved
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 409   # already resolved
    assert admin.post(f"/admin/comments/{child['id']}/reopen",
                      follow_redirects=False).status_code == 400   # non-root target


def test_video_comment_reopen_notify_flow(admin, monkeypatch):
    """studio-notify-on-reopen: when a client reply auto-reopens a resolved thread,
    the studio gets a best-effort push carrying the gallery + thread coordinates.
    It must fire exactly once and only on a real reopen — never on a reply to an
    already-open thread. And it is strictly best-effort: a push that blows up must
    not roll back or break the client's comment or the committed reopen (the audit
    row is the durable record; the notification rides on top)."""
    from app import reopen_notify

    g, vid, photo = _ready_video(admin, title="Reel Review Notify")

    # capture every payload mise tries to push (mise does NOT call Telegram itself)
    pushed = []
    monkeypatch.setattr(reopen_notify, "notify_reopen",
                        lambda payload: pushed.append(payload) or True)

    # client builds a thread (root + reply), then admin resolves it
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        root = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                        data={"body": "Re-grade the intro", "timecode": 3}).json()[0]
        pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                 data={"body": "first shot esp.", "parent_id": root["id"]})
    child = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 303
    assert not pushed  # building + resolving never pushes — only a client reopen does

    # ── a client reply on the RESOLVED thread pushes exactly one notify ──────
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        r = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                     data={"body": "notify-reopen warm", "parent_id": child["id"]})
        assert r.status_code == 200
    new_reply = db.one("SELECT * FROM video_comments WHERE body='notify-reopen warm'")
    assert len(pushed) == 1
    p = pushed[0]
    assert p["gallery_slug"] == g["slug"] and p["gallery_title"] == g["title"]
    assert p["asset_id"] == vid["id"] and p["root_id"] == root["id"]
    assert p["cause_reply_id"] == new_reply["id"]

    # ── a reply on an already-OPEN thread pushes nothing ─────────────────────
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                 data={"body": "one more", "parent_id": new_reply["id"]})
    assert len(pushed) == 1  # the thread was already open — no reopen, no push

    # ── a push that BLOWS UP must not break the comment or the reopen ────────
    assert admin.post(f"/admin/comments/{root['id']}/resolve",
                      follow_redirects=False).status_code == 303  # resolve again
    def boom(payload): raise RuntimeError("odysseus exploded")
    monkeypatch.setattr(reopen_notify, "notify_reopen", boom)
    sys0 = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
                  "AND action='open' AND actor='system'")["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        r = pub.post(f"/g/{g['slug']}/comments/{vid['id']}",
                     data={"body": "still warm again", "parent_id": child["id"]})
        assert r.status_code == 200  # client comment unaffected by the push failure
    assert db.one("SELECT status FROM video_comments WHERE id=?",
                  (root["id"],))["status"] == "open"  # reopen still committed
    sys1 = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
                  "AND action='open' AND actor='system'")["n"]
    assert sys1 == sys0 + 1  # the durable audit row was still written


def test_captured_emails(admin):
    # chef@bistro.com was captured by the download gate in the full flow above
    r = admin.get("/admin/emails")
    assert r.status_code == 200 and "chef@bistro.com" in r.text
    assert "Test Bistro" in r.text and "<b>1</b> unique" in r.text
    assert admin.get("/admin/emails.txt").text == "chef@bistro.com\n"
    # admin-gated like the rest of /admin
    with TestClient(app) as anon:
        assert anon.get("/admin/emails", follow_redirects=False).status_code == 303


def test_dashboard_storage(admin):
    # The disk/backup heartbeat was stripped from the strict-1:1 galleries grid
    # (prototype card has no size cell) and re-homed to Settings → "System health",
    # alongside the other operational settings. It's an operational safety signal
    # and must keep rendering loudly.
    page = admin.get("/admin/settings").text
    # free-space line always renders; the test box is nowhere near the watermark
    assert "GB free" in page
    assert "uploads refused" not in page
    # no snapshots in the fresh test data dir → loud "none found" (silence ≠ evidence)
    assert "none found" in page
    # a fresh snapshot flips the line to a quiet age
    from app import config
    bdir = config.DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    snap = bdir / "mise-2026-06-12-0230.db.gz"
    snap.write_bytes(b"x")
    fresh = admin.get("/admin/settings").text
    assert "under an hour ago" in fresh and "none found" not in fresh
    snap.unlink()


def test_pin_lockout(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app) as pub:
        for _ in range(5):
            pub.post(f"/g/{g['slug']}/pin", data={"pin": "9999"})
        r = pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"})
        assert r.status_code == 429
    db.run("DELETE FROM pin_attempts", ())


def test_expired_gallery(admin):
    import datetime as dt
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    db.run("UPDATE galleries SET expires_at='2000-01-01', published=1 WHERE id=?",
           (g["id"],))
    with TestClient(app) as pub:
        assert pub.get(f"/g/{g['slug']}").status_code == 410

    # the grid card derives an Expiring status from a real expiry so Kevin sees
    # lockouts before clients do. Anchor on the grid card (last href occurrence —
    # the orphan strip may mention it earlier) and read to the card's </a>.
    def card_of(gid):
        page = admin.get("/admin/galleries").text
        start = page.rindex(f"/admin/galleries/{gid}")
        return page[start:page.index("</a>", start)]

    card = card_of(g["id"])
    assert ">Expiring<" in card and "expired" in card  # past-due → dated "expired"

    near = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    db.run("UPDATE galleries SET expires_at=? WHERE id=?", (near, g["id"]))
    card = card_of(g["id"])
    assert ">Expiring<" in card and "3 days" in card  # within the 7-day window

    far = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    db.run("UPDATE galleries SET expires_at=? WHERE id=?", (far, g["id"]))
    # far-future expiry isn't flagged Expiring on the card grid — only soon/expired
    # are; the full date lives on the gallery detail page (checked below)
    assert ">Expiring<" not in card_of(g["id"])

    # delivery email prefill carries the expiry note (form renders when published)
    assert f"Available until {far}" in admin.get(f"/admin/galleries/{g['id']}").text
    db.run("UPDATE galleries SET expires_at=NULL, published=? WHERE id=?",
           (g["published"], g["id"]))


def test_studio_clients_projects(admin):
    # client
    r = admin.post("/admin/studio/clients",
                   data={"name": "Dana Chef", "company": "Test Bistro",
                         "email": "dana@bistro.com", "phone": ""},
                   follow_redirects=False)
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    assert c["name"] == "Dana Chef" and c["company"] == "Test Bistro"

    # project
    r = admin.post(f"/admin/studio/clients/{c['id']}/projects",
                   data={"title": "Spring menu shoot"}, follow_redirects=False)
    assert r.status_code == 303
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    assert p["client_id"] == c["id"] and p["status"] == "inquiry_received"

    # status advances and pages render
    r = admin.post(f"/admin/studio/projects/{p['id']}",
                   data={"title": p["title"], "status": "proposal_sent", "notes": "",
                         "gallery_id": "", "notion_page_id": ""},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"] == "proposal_sent"
    for url in ("/admin/studio", f"/admin/studio/clients/{c['id']}",
                f"/admin/studio/projects/{p['id']}"):
        assert admin.get(url).status_code == 200

    # bad status rejected
    r = admin.post(f"/admin/studio/projects/{p['id']}",
                   data={"title": p["title"], "status": "bogus"},
                   follow_redirects=False)
    assert r.status_code == 400


def test_client_activity_timeline(admin):
    # The client page must narrate document history across ALL of a client's
    # sessions in one reverse-chron feed — the gap the per-project timeline left
    # open (you had to open each session to see what happened). If a sent
    # proposal's event never reaches the client page, this view is broken.
    r = admin.post("/admin/studio/clients",
                   data={"name": "Marco Feed", "company": "Trattoria",
                         "email": "marco@trattoria.com", "phone": ""},
                   follow_redirects=False)
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")

    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Autumn menu shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # empty state before any document activity
    page = admin.get(f"/admin/studio/clients/{c['id']}").text
    assert "Recent activity" in page
    assert "No document activity yet" in page

    # a sent proposal produces drafted + sent events that must surface here,
    # not only on the project page
    admin.post(f"/admin/studio/projects/{p['id']}/proposals",
               data={"preset": "photo_starter"}, follow_redirects=False)
    d = db.one("SELECT * FROM proposals ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/proposals/{d['id']}/send", follow_redirects=False)

    page = admin.get(f"/admin/studio/clients/{c['id']}").text
    assert "No document activity yet" not in page
    assert 'class="timeline"' in page
    assert f"Proposal “{d['title']}” sent" in page

    # Clean up: force-delete this client so the "latest client/project" rows the
    # downstream studio lifecycle tests depend on revert to their fixtures.
    r = admin.post(f"/admin/studio/clients/{c['id']}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_lifecycle(admin):
    from app import config
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create from preset
    r = admin.post(f"/admin/studio/projects/{p['id']}/proposals",
                   data={"preset": "photo_starter"}, follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and d["total_cents"] == 90000
    page = admin.get(f"/admin/studio/proposals/{d['id']}")
    assert page.status_code == 200
    # copy-link macro emits a "Copy link" button (no PIN) carrying the public URL
    assert "Copy link</button>" in page.text  # no "+ PIN" because pin=None
    assert f'data-copy="{config.BASE_URL}/p/{d["slug"]}"' in page.text

    # draft is hidden from the public link
    with TestClient(app) as pub:
        assert pub.get(f"/p/{d['slug']}").status_code == 404

    # edit draft items (recalculates total)
    r = admin.post(f"/admin/studio/proposals/{d['id']}",
                   data={"title": d["title"], "intro": "Hi Dana",
                         "item_label_0": "Half-day session", "item_qty_0": "1",
                         "item_price_0": "1000",
                         "item_label_1": "Extra dishes", "item_qty_1": "2",
                         "item_price_1": "75.50"},
                   follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
    assert d["total_cents"] == 100000 + 15100

    # mark sent — locks editing, advances project
    db.run("UPDATE projects SET status='inquiry_received' WHERE id=?", (p["id"],))
    r = admin.post(f"/admin/studio/proposals/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
    assert d["status"] == "sent" and d["sent_at"]
    assert db.one("SELECT status FROM projects WHERE id=?", (p["id"],))["status"] == "proposal_sent"
    r = admin.post(f"/admin/studio/proposals/{d['id']}",
                   data={"title": "nope"}, follow_redirects=False)
    assert r.status_code == 400

    # public view flips sent → viewed; accept records acceptance but does NOT
    # advance the project (the pipeline advances on contract SIGN, not proposal
    # accept — there is no proposal_accepted stage in the 8-stage funnel)
    with TestClient(app) as pub:
        assert pub.get(f"/p/{d['slug']}").status_code == 200
        d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
        assert d["status"] == "viewed" and d["viewed_at"]
        r = pub.post(f"/p/{d['slug']}/accept", follow_redirects=False)
        assert r.status_code == 303
        d = db.one("SELECT * FROM proposals WHERE id=?", (d["id"],))
        assert d["status"] == "accepted" and d["accepted_at"]
        assert db.one("SELECT status FROM projects WHERE id=?",
                      (p["id"],))["status"] == "proposal_sent"
        # accepted proposals can't be re-actioned
        assert pub.post(f"/p/{d['slug']}/decline",
                        follow_redirects=False).status_code == 400


def test_contract_lifecycle(admin):
    from app import config
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — merge fields pull from project + accepted proposal total
    r = admin.post(f"/admin/studio/projects/{p['id']}/contracts",
                   follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM contracts ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and "Dana Chef" in d["body"]
    assert "$1151.00" in d["body"]  # accepted proposal total merged in
    page = admin.get(f"/admin/studio/contracts/{d['id']}")
    assert page.status_code == 200
    assert "Copy link</button>" in page.text  # macro w/ pin=None
    assert f'data-copy="{config.BASE_URL}/c/{d["slug"]}"' in page.text

    # draft hidden from public; editable
    with TestClient(app) as pub:
        assert pub.get(f"/c/{d['slug']}").status_code == 404
    r = admin.post(f"/admin/studio/contracts/{d['id']}",
                   data={"title": d["title"], "body": d["body"] + "\n8. EXTRA — Test clause."},
                   follow_redirects=False)
    assert r.status_code == 303

    # send locks body and records hash
    r = admin.post(f"/admin/studio/contracts/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM contracts WHERE id=?", (d["id"],))
    assert d["status"] == "sent" and len(d["body_sha256"]) == 64
    r = admin.post(f"/admin/studio/contracts/{d['id']}",
                   data={"title": "x", "body": "tampered"}, follow_redirects=False)
    assert r.status_code == 400

    with TestClient(app) as pub:
        # view flips sent → viewed
        assert pub.get(f"/c/{d['slug']}").status_code == 200
        assert db.one("SELECT status FROM contracts WHERE id=?",
                      (d["id"],))["status"] == "viewed"

        # tampered body refuses signature (integrity check)
        db.run("UPDATE contracts SET body=body||' ' WHERE id=?", (d["id"],))
        r = pub.post(f"/c/{d['slug']}/sign",
                     data={"signer_name": "Dana Chef", "agree": "yes"},
                     follow_redirects=False)
        assert r.status_code == 409
        db.run("UPDATE contracts SET body=rtrim(body,' ') WHERE id=?", (d["id"],))

        # sign records name/ip/timestamp, advances project to contract_signed
        r = pub.post(f"/c/{d['slug']}/sign",
                     data={"signer_name": "Dana Chef", "agree": "yes"},
                     follow_redirects=False)
        assert r.status_code == 303
        d = db.one("SELECT * FROM contracts WHERE id=?", (d["id"],))
        assert (d["status"] == "signed" and d["signer_name"] == "Dana Chef"
                and d["signed_at"] and d["signer_ip"])
        assert db.one("SELECT status FROM projects WHERE id=?",
                      (p["id"],))["status"] == "contract_signed"
        # signed contract renders the signature record, can't be re-signed
        assert "Signed by Dana Chef" in pub.get(f"/c/{d['slug']}").text
        assert pub.post(f"/c/{d['slug']}/sign",
                        data={"signer_name": "X", "agree": "yes"},
                        follow_redirects=False).status_code == 400


def _stripe_sig(payload: bytes, secret: str) -> str:
    import hmac as _hmac, hashlib as _hl, time as _t
    t = int(_t.time())
    mac = _hmac.new(secret.encode(), f"{t}.".encode() + payload, _hl.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _checkout_event(event_id, invoice_id, kind, amount):
    import json as _json
    return _json.dumps({
        "id": event_id, "object": "event", "api_version": "2024-06-20",
        "type": "checkout.session.completed",
        "data": {"object": {"id": f"cs_{event_id}", "object": "checkout.session",
                            "payment_status": "paid", "amount_total": amount,
                            "metadata": {"invoice_id": str(invoice_id),
                                         "kind": kind}}},
    }).encode()


def test_invoice_lifecycle(admin, monkeypatch):
    from app import config
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — seeds items/total from the accepted proposal
    r = admin.post(f"/admin/studio/projects/{p['id']}/invoices", follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    assert d["status"] == "draft" and d["total_cents"] == 115100
    page = admin.get(f"/admin/studio/invoices/{d['id']}")
    assert page.status_code == 200
    assert "Copy link</button>" in page.text  # macro w/ pin=None
    assert f'data-copy="{config.BASE_URL}/i/{d["slug"]}"' in page.text
    with TestClient(app) as pub:
        assert pub.get(f"/i/{d['slug']}").status_code == 404

    # deposit above total rejected; valid deposit + due date saved
    base = {"title": d["title"], "item_label_0": "Shoot package",
            "item_qty_0": "1", "item_price_0": "1151"}
    r = admin.post(f"/admin/studio/invoices/{d['id']}",
                   data=base | {"deposit": "2000"}, follow_redirects=False)
    assert r.status_code == 400
    r = admin.post(f"/admin/studio/invoices/{d['id']}",
                   data=base | {"deposit": "500", "due_date": "2026-07-01"},
                   follow_redirects=False)
    assert r.status_code == 303
    d = db.one("SELECT * FROM invoices WHERE id=?", (d["id"],))
    assert d["deposit_cents"] == 50000 and d["due_date"] == "2026-07-01"

    # send locks it; public view flips sent → viewed
    r = admin.post(f"/admin/studio/invoices/{d['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    with TestClient(app) as pub:
        page = pub.get(f"/i/{d['slug']}")
        assert page.status_code == 200 and "$500.00" in page.text
        assert db.one("SELECT status FROM invoices WHERE id=?",
                      (d["id"],))["status"] == "viewed"
        # payments not configured → pay degrades, webhook refuses
        assert pub.post(f"/i/{d['slug']}/pay",
                        follow_redirects=False).status_code == 503
        assert pub.post("/webhooks/stripe", content=b"{}").status_code == 503

        # webhook with signature verification
        monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        body = _checkout_event("evt_dep_1", d["id"], "deposit", 50000)
        assert pub.post("/webhooks/stripe", content=body,
                        headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
        r = pub.post("/webhooks/stripe", content=body,
                     headers={"stripe-signature": _stripe_sig(body, "whsec_test")})
        assert r.status_code == 200
        assert db.one("SELECT status FROM invoices WHERE id=?",
                      (d["id"],))["status"] == "deposit_paid"
        # retried event is idempotent
        r = pub.post("/webhooks/stripe", content=body,
                     headers={"stripe-signature": _stripe_sig(body, "whsec_test")})
        assert r.json().get("duplicate") is True
        assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?",
                      (d["id"],))["n"] == 1

        # balance payment settles the invoice
        body = _checkout_event("evt_bal_1", d["id"], "balance", 65100)
        r = pub.post("/webhooks/stripe", content=body,
                     headers={"stripe-signature": _stripe_sig(body, "whsec_test")})
        assert r.status_code == 200
        d = db.one("SELECT * FROM invoices WHERE id=?", (d["id"],))
        assert d["status"] == "paid" and d["paid_at"]
        assert "Paid in full" in pub.get(f"/i/{d['slug']}").text


def test_reports_top_clients(admin):
    # Reports must rank clients by cash actually collected (Stripe payments are the
    # truth), not by invoiced/booked value — a client who never pays shouldn't top
    # the list. A repeat payer (>=2 paid projects) must be flagged. If the collected
    # total or the repeat signal is wrong here, the value leaderboard is misleading.
    admin.post("/admin/studio/clients",
               data={"name": "Lucia Vega", "company": "Osteria Vega",
                     "email": "lucia@osteriavega.com", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    # Two separate projects, each with a paid invoice → repeat booker.
    pids, iids = [], []
    for n, cents in (("Spring tasting", 600000), ("Autumn tasting", 400000)):
        admin.post(f"/admin/studio/clients/{c['id']}/projects",
                   data={"title": n}, follow_redirects=False)
        p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        pids.append(p["id"])
        iid = db.run("""INSERT INTO invoices (project_id, slug, title, line_items, total_cents)
                        VALUES (?,?,?,?,?)""",
                     (p["id"], f"inv-{p['id']}", "Invoice", "[]", cents))
        iids.append(iid)
        db.run("""INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                  amount_cents, kind) VALUES (?,?,?,?,?)""",
               (iid, f"evt_{iid}", f"cs_{iid}", cents, "full"))

    page = admin.get("/admin/reports").text
    assert "Top clients" in page
    assert "Osteria Vega" in page
    assert "$10,000" in page  # 600000 + 400000 cents collected
    # Two paid projects → repeat badge.
    block = page.split("Osteria Vega", 1)[1][:200]
    assert "repeat" in block

    # Clean up everything created so the latest-invoice / aggregate counts the
    # downstream lifecycle tests depend on revert to their fixtures.
    for iid in iids:
        db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
        db.run("DELETE FROM invoices WHERE id=?", (iid,))
    r = admin.post(f"/admin/studio/clients/{c['id']}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_reports_range_toggle(admin):
    # Reports headline numbers must scope to the selected range (month/quarter/
    # YTD/last-year) — the same pills the Income page uses. An unknown range must
    # fall back to YTD, not 500. If the toggle silently ignores ?range=, the page
    # would always show YTD and the pills would be decorative lies.
    for key, label in (("month", "This month"), ("quarter", "Quarter"),
                       ("ytd", "YTD"), ("lastyear", "Last year")):
        page = admin.get(f"/admin/reports?range={key}").text
        assert "fin-range-pill" in page
        assert f'href="/admin/reports?range={key}"' in page
        # the active pill carries fin-range-on next to its own key
        on = page.split(f'?range={key}', 1)[1][:60]
        assert "fin-range-on" in on
    # garbage range → YTD fallback, still 200
    bad = admin.get("/admin/reports?range=bogus")
    assert bad.status_code == 200
    assert "fin-range-on" in bad.text


def test_tasks_board_view(admin):
    # The strict-1:1 Tasks page is a 3-column board (Today / This week / Done),
    # bucketed server-side from due_date. Encodes WHY the buckets matter: a task
    # due today (or overdue) belongs in Today; an open task with no/later due
    # date belongs in This week. Guard the column labels and the bucketing.
    import datetime as _dt
    today_iso = _dt.date.today().isoformat()
    admin.post("/admin/tasks",
               data={"title": "Due-today board task", "due_date": today_iso},
               follow_redirects=False)
    admin.post("/admin/tasks", data={"title": "Undated board task"},
               follow_redirects=False)
    page = admin.get("/admin/tasks").text
    assert "tk-grid" in page
    for label in (">Today<", ">This week<", ">Done<"):
        assert label in page
    # the due-today task sorts ahead of the undated one (Today column renders
    # before This week) — both present, the urgent one first in document order.
    assert page.index("Due-today board task") < page.index("Undated board task")
    db.run("DELETE FROM tasks WHERE title IN "
           "('Due-today board task','Undated board task')")


def test_manage_nav_financials_expenses(admin):
    # Financials and Expenses must appear as first-class links in the Manage
    # sidebar (not palette-only), and their active-state guards must not overlap:
    # on the income page only Financials is highlighted; on the expenses/mileage
    # pages only Expenses is. A bad guard would light both at once and mislead.
    inc = admin.get("/admin/financials").text
    assert 'href="/admin/financials" title="Financials"' in inc
    assert 'href="/admin/financials/expenses" title="Expenses"' in inc
    # active financials link, inactive expenses link, on the income page
    assert 'href="/admin/financials" title="Financials" class="is-active"' in inc
    assert 'href="/admin/financials/expenses" title="Expenses"><' in inc

    exp = admin.get("/admin/financials/expenses").text
    assert 'href="/admin/financials/expenses" title="Expenses" class="is-active"' in exp
    assert 'href="/admin/financials" title="Financials"><' in exp


def test_invoice_receipt(admin):
    # A paid invoice must offer a printable receipt that lists the recorded
    # payments and totals — for the client's accountant. The receipt is a pure
    # read of the payments table (the source Stripe writes), so it can never
    # disagree with what was charged. No payments → no receipt (404).
    admin.post("/admin/studio/clients",
               data={"name": "Priya Anand", "company": "Saffron Counter",
                     "email": "priya@saffron.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Menu refresh"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    # Deposit then balance, both recorded → receipt shows two lines, paid in full.
    iid = db.run("""INSERT INTO invoices (project_id, slug, title, line_items,
                                          total_cents, status, paid_at)
                    VALUES (?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "rcpt-test", "Menu refresh shoot", "[]", 500000, "paid"))
    inv = db.one("SELECT * FROM invoices WHERE id=?", (iid,))
    for kind, cents in (("deposit", 200000), ("balance", 300000)):
        db.run("""INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
                  amount_cents, kind) VALUES (?,?,?,?,?)""",
               (iid, f"evt_{kind}_{iid}", f"cs_{kind}_{iid}", cents, kind))

    r = admin.get(f"/i/{inv['slug']}/receipt")
    assert r.status_code == 200
    assert "Deposit" in r.text and "Balance" in r.text
    assert "$2000.00" in r.text and "$3000.00" in r.text
    assert "$5000.00" in r.text  # total paid
    assert "Paid in full" in r.text
    # The invoice page links to the receipt once a payment exists.
    assert f"/i/{inv['slug']}/receipt" in admin.get(f"/i/{inv['slug']}").text

    # An invoice with no payments has no receipt.
    jid = db.run("""INSERT INTO invoices (project_id, slug, title, line_items,
                                          total_cents, status)
                    VALUES (?,?,?,?,?,?)""",
                 (p["id"], "rcpt-empty", "Unpaid", "[]", 100000, "sent"))
    assert admin.get("/i/rcpt-empty/receipt").status_code == 404

    db.run("DELETE FROM payments WHERE invoice_id=?", (iid,))
    db.run("DELETE FROM invoices WHERE id IN (?,?)", (iid, jid))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_convert(admin):
    # Once a client accepts a proposal, one click should spawn the matching draft
    # contract + draft invoice instead of rebuilding both by hand. Both land as
    # drafts (Kevin still reviews/sends — nothing is charged); the invoice copies
    # the proposal's line items + total verbatim and the contract body carries the
    # accepted total. A proposal that isn't accepted yet can't be converted.
    admin.post("/admin/studio/clients",
               data={"name": "Marco Reyes", "company": "Ember Room",
                     "email": "marco@ember.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Dinner service shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Full-day shoot", "qty": 1, "unit_cents": 180000}]'
    pid = db.run("""INSERT INTO proposals (project_id, slug, title, line_items,
                                           total_cents, status, accepted_at)
                    VALUES (?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "conv-test", "Dinner proposal", items, 180000, "accepted"))

    # the accepted proposal page offers the convert action
    page = admin.get(f"/admin/studio/proposals/{pid}")
    assert page.status_code == 200
    assert f"/admin/studio/proposals/{pid}/convert" in page.text

    r = admin.post(f"/admin/studio/proposals/{pid}/convert", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/studio/projects/{p['id']}"

    ct = db.one("SELECT * FROM contracts WHERE project_id=? ORDER BY id DESC LIMIT 1",
                (p["id"],))
    inv = db.one("SELECT * FROM invoices WHERE project_id=? ORDER BY id DESC LIMIT 1",
                 (p["id"],))
    assert ct and ct["status"] == "draft"
    assert "$1800.00" in ct["body"]  # accepted total merged into the contract body
    assert inv and inv["status"] == "draft"
    assert inv["total_cents"] == 180000 and inv["line_items"] == items

    # a proposal that hasn't been accepted can't be converted
    qid = db.run("""INSERT INTO proposals (project_id, slug, title, line_items,
                                           total_cents, status)
                    VALUES (?,?,?,?,?,?)""",
                 (p["id"], "conv-draft", "Draft proposal", items, 180000, "sent"))
    assert admin.post(f"/admin/studio/proposals/{qid}/convert",
                      follow_redirects=False).status_code == 400

    db.run("DELETE FROM contracts WHERE id=?", (ct["id"],))
    db.run("DELETE FROM invoices WHERE id=?", (inv["id"],))
    db.run("DELETE FROM proposals WHERE id IN (?,?)", (pid, qid))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_proposal_duplicate(admin):
    # A locked proposal (sent/accepted/declined) can be cloned into a fresh
    # editable draft — the revise-and-re-send path. The copy carries the same
    # title/intro/line items but its own slug and status='draft'; the original
    # is left untouched.
    admin.post("/admin/studio/clients",
               data={"name": "Lena Voss", "company": "Copper Pot",
                     "email": "lena@copper.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Brunch menu shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Half-day shoot", "qty": 1, "unit_cents": 90000}]'
    src = db.run("""INSERT INTO proposals (project_id, slug, title, intro, line_items,
                    total_cents, status, sent_at)
                    VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "dup-src", "Brunch proposal", "Hi Lena", items, 90000,
                  "declined"))

    # the locked proposal page offers the duplicate action
    page = admin.get(f"/admin/studio/proposals/{src}")
    assert f"/admin/studio/proposals/{src}/duplicate" in page.text

    r = admin.post(f"/admin/studio/proposals/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert new_id != src
    new = db.one("SELECT * FROM proposals WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "dup-src"
    assert (new["title"] == "Brunch proposal" and new["intro"] == "Hi Lena"
            and new["line_items"] == items and new["total_cents"] == 90000)
    # original is untouched
    assert db.one("SELECT status FROM proposals WHERE id=?", (src,))["status"] == "declined"

    db.run("DELETE FROM proposals WHERE id IN (?,?)", (src, new_id))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_contract_duplicate(admin):
    # A locked/signed contract clones into a fresh editable draft: same body + title,
    # new slug, no hash or signature carried over. Original untouched.
    admin.post("/admin/studio/clients",
               data={"name": "Nadia Okafor", "company": "Olive & Ash",
                     "email": "nadia@oliveash.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Cookbook shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    src = db.run("""INSERT INTO contracts (project_id, slug, title, body, body_sha256,
                    status, signer_name, signer_ip, signed_at)
                    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "cdup-src", "Services Agreement", "BODY TEXT",
                  "a" * 64, "signed", "Nadia Okafor", "1.2.3.4"))

    page = admin.get(f"/admin/studio/contracts/{src}")
    assert f"/admin/studio/contracts/{src}/duplicate" in page.text
    r = admin.post(f"/admin/studio/contracts/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    new = db.one("SELECT * FROM contracts WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "cdup-src"
    assert new["title"] == "Services Agreement" and new["body"] == "BODY TEXT"
    assert new["body_sha256"] is None and new["signer_name"] is None
    assert db.one("SELECT status FROM contracts WHERE id=?", (src,))["status"] == "signed"

    db.run("DELETE FROM contracts WHERE id IN (?,?)", (src, new_id))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)


def test_invoice_duplicate(admin):
    # A paid invoice clones into a fresh draft copying line items/total/deposit/due/
    # terms — but no payments, Stripe session, or paid status. The original and the
    # payments recorded against it are left intact.
    admin.post("/admin/studio/clients",
               data={"name": "Ravi Shah", "company": "Tiffin Box",
                     "email": "ravi@tiffin.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Lunch service shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    items = '[{"label": "Half-day shoot", "qty": 1, "unit_cents": 90000}]'
    src = db.run("""INSERT INTO invoices (project_id, slug, title, line_items, total_cents,
                    deposit_cents, due_date, terms, status, paid_at)
                    VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "idup-src", "Lunch invoice", items, 90000, 30000,
                  "2026-07-01", "50% deposit, balance on delivery", "paid"))
    db.run("""INSERT INTO payments (invoice_id, stripe_event_id, stripe_session_id,
              amount_cents, kind) VALUES (?,?,?,?,?)""",
           (src, "evt_idup", "cs_idup", 90000, "full"))

    page = admin.get(f"/admin/studio/invoices/{src}")
    assert f"/admin/studio/invoices/{src}/duplicate" in page.text
    r = admin.post(f"/admin/studio/invoices/{src}/duplicate", follow_redirects=False)
    assert r.status_code == 303
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    new = db.one("SELECT * FROM invoices WHERE id=?", (new_id,))
    assert new["status"] == "draft" and new["slug"] != "idup-src"
    assert (new["title"] == "Lunch invoice" and new["line_items"] == items
            and new["total_cents"] == 90000 and new["deposit_cents"] == 30000
            and new["due_date"] == "2026-07-01"
            and new["terms"] == "50% deposit, balance on delivery")
    assert new["paid_at"] is None and new["stripe_session_id"] is None
    # the copy has no payments; the original keeps its one
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?",
                  (new_id,))["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM payments WHERE invoice_id=?",
                  (src,))["n"] == 1

    db.run("DELETE FROM payments WHERE invoice_id=?", (src,))
    db.run("DELETE FROM invoices WHERE id IN (?,?)", (src, new_id))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)


def test_contract_countersign(admin):
    # A client-signed contract can be countersigned once by the studio: typed name +
    # timestamp recorded alongside the client's, making the record bilateral. The
    # client's signature is untouched; a second countersign attempt is rejected.
    admin.post("/admin/studio/clients",
               data={"name": "Lena Brandt", "company": "Copper Spoon",
                     "email": "lena@copperspoon.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Tasting menu shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    src = db.run("""INSERT INTO contracts (project_id, slug, title, body, body_sha256,
                    status, signer_name, signer_ip, signed_at)
                    VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                 (p["id"], "csign-src", "Services Agreement", "BODY TEXT",
                  "b" * 64, "signed", "Lena Brandt", "5.6.7.8"))

    # the countersign form shows on a signed-but-not-countersigned contract
    page = admin.get(f"/admin/studio/contracts/{src}")
    assert f"/admin/studio/contracts/{src}/countersign" in page.text

    # blank name is rejected
    bad = admin.post(f"/admin/studio/contracts/{src}/countersign",
                     data={"countersigner_name": "   "}, follow_redirects=False)
    assert bad.status_code == 400

    r = admin.post(f"/admin/studio/contracts/{src}/countersign",
                   data={"countersigner_name": "Kevin Lee"}, follow_redirects=False)
    assert r.status_code == 303
    row = db.one("SELECT * FROM contracts WHERE id=?", (src,))
    assert row["countersigner_name"] == "Kevin Lee" and row["countersigned_at"]
    assert row["status"] == "signed" and row["signer_name"] == "Lena Brandt"

    # second countersign is refused
    dup = admin.post(f"/admin/studio/contracts/{src}/countersign",
                     data={"countersigner_name": "Kevin Lee"}, follow_redirects=False)
    assert dup.status_code == 400

    # a draft contract cannot be countersigned
    draft = db.run("""INSERT INTO contracts (project_id, slug, title, body, status)
                      VALUES (?,?,?,?, 'draft')""",
                   (p["id"], "csign-draft", "Draft", "X"))
    nope = admin.post(f"/admin/studio/contracts/{draft}/countersign",
                      data={"countersigner_name": "Kevin Lee"}, follow_redirects=False)
    assert nope.status_code == 400

    db.run("DELETE FROM contracts WHERE id IN (?,?)", (src, draft))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)


def test_admin_global_search(admin):
    # The search box is the jump-to across the admin. It must find a client by
    # business name and its project by title, and link straight to each. It must
    # also escape LIKE wildcards in the query — a bare "%" must NOT match every
    # record (that would make the box useless and leak the whole table).
    admin.post("/admin/studio/clients",
               data={"name": "Quinella Ostrowski", "company": "Zarzuela Cantina",
                     "email": "q@zarzuela.test", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Zarzuela tasting menu shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    page = admin.get("/admin/search", params={"q": "zarzuela"}).text
    assert "Zarzuela Cantina" in page
    assert f"/admin/studio/clients/{c['id']}" in page
    assert "Zarzuela tasting menu shoot" in page
    assert f"/admin/studio/projects/{p['id']}" in page

    # Nonsense query → no matches, not a 500.
    miss = admin.get("/admin/search", params={"q": "qzxnomatchqzx"})
    assert miss.status_code == 200 and "No matches" in miss.text

    # Wildcard escape: "%" is a literal here, so the just-made client must NOT show.
    wild = admin.get("/admin/search", params={"q": "%"}).text
    assert "Zarzuela Cantina" not in wild

    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_testimonial_self_submit(admin):
    # A client must be able to write their own testimonial via a tokened /t/{slug}
    # link, and it MUST land unpublished — the marketing site only shows moderated
    # quotes. If a self-submission published itself, an unreviewed quote could go
    # live. Also: the link is one-shot (re-POST is an idempotent thank-you, never a
    # second testimonial row).
    admin.post("/admin/studio/clients",
               data={"name": "Marco Rossi", "company": "Trattoria Rossi",
                     "email": "marco@trattoriarossi.com", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Dinner menu shoot"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # Admin raises the request link; the project page surfaces it.
    admin.post(f"/admin/studio/projects/{p['id']}/testimonial-request",
               data={"gallery_id": ""}, follow_redirects=False)
    req = db.one("SELECT * FROM testimonial_requests ORDER BY id DESC LIMIT 1")
    assert req["project_id"] == p["id"] and req["submitted_at"] is None
    page = admin.get(f"/admin/studio/projects/{p['id']}").text
    assert f"/t/{req['slug']}" in page and "awaiting client" in page

    # Client opens the form (greeted by name) and submits.
    form = admin.get(f"/t/{req['slug']}").text
    assert "Trattoria Rossi" in form and "Share your experience" in form
    r = admin.post(f"/t/{req['slug']}",
                   data={"quote": "Marco's plates have never looked better.",
                         "attribution_name": "Marco Rossi",
                         "business": "Trattoria Rossi"},
                   follow_redirects=False)
    assert r.status_code == 303
    t = db.one("SELECT * FROM testimonials ORDER BY id DESC LIMIT 1")
    assert t["published"] == 0  # lands unpublished for moderation
    assert t["quote"].startswith("Marco's plates")
    req = db.one("SELECT * FROM testimonial_requests WHERE id=?", (req["id"],))
    assert req["submitted_at"] and req["testimonial_id"] == t["id"]

    # Thank-you state, and a re-POST does not create a second testimonial.
    assert "your words have been received" in admin.get(f"/t/{req['slug']}").text.lower()
    n_before = db.one("SELECT COUNT(*) AS n FROM testimonials")["n"]
    admin.post(f"/t/{req['slug']}",
               data={"quote": "second", "attribution_name": "x", "business": ""},
               follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM testimonials")["n"] == n_before

    # Moderation surfacing: while it's unpublished, the admin home nudges to review
    # it and the testimonials list flags it as client-submitted. Without this the
    # self-submission has no inbox and would go unnoticed.
    assert "awaiting publish" in admin.get("/admin/home").text
    tlist = admin.get("/admin/studio/testimonials").text
    assert "from client" in tlist
    # Publishing clears the nudge (it only fires on unpublished client quotes).
    db.run("UPDATE testimonials SET published=1 WHERE id=?", (t["id"],))
    assert "awaiting publish" not in admin.get("/admin/home").text

    # Clean up so downstream order-coupled tests keep their fixtures.
    db.run("DELETE FROM testimonials WHERE id=?", (t["id"],))
    db.run("DELETE FROM testimonial_requests WHERE id=?", (req["id"],))
    admin.post(f"/admin/studio/clients/{c['id']}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is None


def test_email_send(admin, monkeypatch):
    from app import config, mailer
    inv = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    data = {"to": "dana@bistro.com", "subject": "Invoice — Spring menu shoot",
            "message": "Hi Dana, link inside."}

    # not configured → 503, nothing logged
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data,
                   follow_redirects=False)
    assert r.status_code == 503

    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body:
                        sent.append((to, subject, body)))

    # drafts can't be emailed (client link would 404)
    r = admin.post(f"/admin/studio/projects/{inv['project_id']}/invoices",
                   follow_redirects=False)
    draft = db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
    r = admin.post(f"/admin/studio/invoices/{draft['id']}/email", data=data,
                   follow_redirects=False)
    assert r.status_code == 400 and not sent

    # real send is logged with project linkage
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data,
                   follow_redirects=False)
    assert r.status_code == 303 and len(sent) == 1
    assert sent[0][0] == "dana@bistro.com"
    e = db.one("SELECT * FROM emails_log ORDER BY id DESC LIMIT 1")
    assert (e["doc_kind"] == "invoice" and e["doc_id"] == inv["id"]
            and e["project_id"] == inv["project_id"]
            and e["to_email"] == "dana@bistro.com")

    # bogus kind 404s, SMTP failure surfaces as 502 and is not logged
    assert admin.post(f"/admin/studio/payments/{inv['id']}/email", data=data,
                      follow_redirects=False).status_code == 404
    n_before = db.one("SELECT COUNT(*) AS n FROM emails_log")["n"]
    def boom(*a): raise OSError("smtp down")
    monkeypatch.setattr(mailer, "send", boom)
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/email", data=data,
                   follow_redirects=False)
    assert r.status_code == 502
    assert db.one("SELECT COUNT(*) AS n FROM emails_log")["n"] == n_before


def test_notion_sync(monkeypatch):
    from app import config, notion_sync
    inv = db.one("""SELECT i.* FROM invoices i WHERE i.status='paid'
                    ORDER BY i.id DESC LIMIT 1""")
    assert inv, "earlier test left a paid invoice"

    # send + webhook enqueued sync jobs
    assert db.one("""SELECT COUNT(*) AS n FROM jobs
                     WHERE kind='notion_sync_invoice'""")["n"] >= 3

    # no token / no page id → clean skip, no HTTP
    calls = []
    monkeypatch.setattr(notion_sync, "_patch_page",
                        lambda pid, props: calls.append((pid, props)))
    notion_sync.sync_invoice(inv["id"])
    assert not calls

    # with token + page id → exact property payload (Odysseus contract)
    monkeypatch.setattr(config, "NOTION_TOKEN", "secret_test")
    db.run("UPDATE projects SET notion_page_id='abc123' WHERE id=?",
           (inv["project_id"],))
    notion_sync.sync_invoice(inv["id"])
    pid, props = calls[0]
    assert pid == "abc123"
    assert props == {
        "Invoice Amount": {"number": 1151.0},
        "Deposit Amount": {"number": 500.0},
        "Invoice Paid": {"checkbox": True},
        "Deposit Paid": {"checkbox": True},
    }


def test_gallery_delivery_email(admin, monkeypatch):
    from app import config, mailer
    gid = db.run("INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
                 ("DeliveryMail01", "Tasting Menu", "5678"))
    data = {"to": "owner@bistro.com", "subject": "Your photos are ready — Tasting Menu",
            "message": f"link {config.BASE_URL}/g/DeliveryMail01 PIN 5678"}

    # unpublished: no form on the page, send refused (link would 404)
    assert "Send delivery email" not in admin.get(f"/admin/galleries/{gid}").text
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 400

    db.run("UPDATE galleries SET published=1 WHERE id=?", (gid,))
    page = admin.get(f"/admin/galleries/{gid}").text
    assert "Send delivery email" in page
    assert "/g/DeliveryMail01" in page and "PIN: 5678" in page
    # "Copy link + PIN" button carries the URL + PIN as a data-copy payload
    # (newline encoded as &#10;) so a tiny inline JS can shove it into the clipboard
    assert "Copy link + PIN" in page
    assert 'data-copy="' in page
    assert "/g/DeliveryMail01&#10;PIN: 5678" in page
    assert 'class="copy-feedback' in page
    # template kind selector with 3 prefilled options
    assert 'id="email-kind"' in page
    assert 'value="delivery"' in page and 'value="proofing"' in page \
        and 'value="final"' in page
    # each carries the prefilled subject + body in data-* attrs
    assert "Time to pick your selects" in page  # proofing subject
    assert "Final edits delivered" in page      # final subject
    # the proofing body explains the tap-heart flow
    assert "Tap the heart on each photo" in page

    # not configured → 503, nothing logged
    monkeypatch.setattr(mailer, "configured", lambda: False)
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 503

    # configured → sends and logs (doc_kind 'other' — the schema's catch-all)
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, reply_to="":
                        sent.append((to, subject, body)))
    r = admin.post(f"/admin/galleries/{gid}/email", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert sent[0][0] == "owner@bistro.com" and "PIN 5678" in sent[0][2]
    row = db.one("SELECT * FROM emails_log WHERE doc_kind='other' AND doc_id=?", (gid,))
    assert row["to_email"] == "owner@bistro.com"

    # SMTP failure → 502, no second log row
    def boom(*a, **kw): raise OSError("smtp down")
    monkeypatch.setattr(mailer, "send", boom)
    assert admin.post(f"/admin/galleries/{gid}/email", data=data).status_code == 502
    assert db.one("SELECT COUNT(*) AS n FROM emails_log WHERE doc_kind='other' "
                  "AND doc_id=?", (gid,))["n"] == 1


def test_final_email_auto_advances_project(admin, monkeypatch):
    from app import mailer
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, reply_to="": None)

    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)",
                 ("Mara Sun", "mara@cafe.com"))
    pid = db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
                 (cid, "Spring shoot", "session_planning"))
    gid = db.run("INSERT INTO galleries (slug, title, pin, project_id, published) "
                 "VALUES (?,?,?,?,1)",
                 ("FinalEmail0001", "Spring shoot", "1234", pid))
    data = {"to": "mara@cafe.com", "subject": "x", "message": "y"}

    # kind=delivery (default) → status unchanged
    r = admin.post(f"/admin/galleries/{gid}/email",
                   data={**data, "email_kind": "delivery"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"

    # kind=proofing → status unchanged (proofing is a prompt, not a hand-off)
    admin.post(f"/admin/galleries/{gid}/email",
               data={**data, "email_kind": "proofing"},
               follow_redirects=False)
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "session_planning"

    # kind=final + project in pre-delivered state → auto-advance to 'project_closed'
    admin.post(f"/admin/galleries/{gid}/email",
               data={**data, "email_kind": "final"},
               follow_redirects=False)
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "project_closed"

    # already-'project_closed' → no churn (idempotent re-sends are fine)
    admin.post(f"/admin/galleries/{gid}/email",
               data={**data, "email_kind": "final"},
               follow_redirects=False)
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "project_closed"

    # 'archived' is NEVER auto-overwritten by a final-email — Kevin's archive
    # signal is intentional and should survive a re-fire of the hand-off email.
    db.run("UPDATE projects SET status='archived' WHERE id=?", (pid,))
    admin.post(f"/admin/galleries/{gid}/email",
               data={**data, "email_kind": "final"},
               follow_redirects=False)
    assert db.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "archived"

    # final email on a gallery with NO linked project → just sends, no crash
    gid2 = db.run("INSERT INTO galleries (slug, title, pin, published) "
                  "VALUES (?,?,?,1)", ("FinalNoProj001", "Loose", "1234"))
    r = admin.post(f"/admin/galleries/{gid2}/email",
                   data={**data, "email_kind": "final"},
                   follow_redirects=False)
    assert r.status_code == 303


def test_gallery_notion_writeback(admin, monkeypatch):
    from app import config, notion_sync
    project = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    assert project, "earlier test left a project"
    gid = db.run("INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
                 ("WritebackSlug1", "Writeback", "1234"))

    def save(published=True, project_id=project["id"]):
        return admin.post(f"/admin/galleries/{gid}/settings",
                          data={"title": "Writeback", "pin": "1234",
                                "published": "true" if published else "",
                                "project_id": project_id or ""},
                          follow_redirects=False)

    def n_jobs():
        return db.one("""SELECT COUNT(*) AS n FROM jobs WHERE kind='notion_sync_gallery'
                         AND json_extract(payload,'$.gallery_id')=?""", (gid,))["n"]

    # unpublished or unlinked saves enqueue nothing
    assert save(published=False).status_code == 303 and n_jobs() == 0
    assert save(project_id=None).status_code == 303 and n_jobs() == 0

    # publish flip with a project → one job; re-saving published is quiet
    save()
    assert n_jobs() == 1
    save()
    assert n_jobs() == 1

    # the job patches Gallery URL on the project's Notion session page
    calls = []
    monkeypatch.setattr(notion_sync, "_patch_page",
                        lambda pid, props: calls.append((pid, props)))
    monkeypatch.setattr(config, "NOTION_TOKEN", "secret_test")
    db.run("UPDATE projects SET notion_page_id='sess42' WHERE id=?", (project["id"],))
    notion_sync.sync_gallery(gid)
    assert calls == [("sess42",
                      {"Gallery URL": {"url": f"{config.BASE_URL}/g/WritebackSlug1"}})]

    # unpublishing later → clean skip, no HTTP
    calls.clear()
    db.run("UPDATE galleries SET published=0 WHERE id=?", (gid,))
    notion_sync.sync_gallery(gid)
    assert not calls


def test_portal_lifecycle(admin):
    import datetime as dt
    import time
    from app import jobs, presets
    crop_slugs = [ps["slug"] for ps in presets.active()]
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    c = db.one("SELECT * FROM clients ORDER BY id LIMIT 1")
    a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))

    # link gallery to the studio client, with captions
    r = admin.post(f"/admin/galleries/{g['id']}/settings",
                   data={"title": g["title"], "pin": "1234", "published": "true",
                         "client_id": str(c["id"]),
                         "captions": "Golden hour plating shot #foodie"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT client_id FROM galleries WHERE id=?",
                  (g["id"],))["client_id"] == c["id"]

    # usage rights on the client
    admin.post(f"/admin/studio/clients/{c['id']}",
               data={"name": c["name"], "company": c["company"] or "",
                     "usage_rights": "Social + web, 12 months."},
               follow_redirects=False)

    # brand asset upload — allowlist enforced
    r = admin.post(f"/admin/studio/clients/{c['id']}/brand",
                   files=[("files", ("logo.png", _jpeg_bytes(64, 64), "image/png")),
                          ("files", ("evil.exe", b"MZ", "application/octet-stream"))],
                   follow_redirects=False)
    assert r.status_code == 303
    brand = db.all_("SELECT * FROM brand_assets WHERE client_id=?", (c["id"],))
    assert len(brand) == 1 and brand[0]["filename"] == "logo.png"
    assert admin.get(
        f"/admin/studio/clients/{c['id']}/brand/{brand[0]['id']}").status_code == 200

    # create portal (idempotent: second create rejected), backfills crop jobs
    r = admin.post(f"/admin/studio/clients/{c['id']}/portal", follow_redirects=False)
    assert r.status_code == 303
    assert admin.post(f"/admin/studio/clients/{c['id']}/portal",
                      follow_redirects=False).status_code == 400
    portal = db.one("SELECT * FROM portals WHERE client_id=?", (c["id"],))
    assert len(portal["slug"]) >= 12 and not portal["published"]

    # admin client page shows the portal link, no visits yet
    page = admin.get(f"/admin/studio/clients/{c['id']}")
    assert portal["slug"] in page.text
    # "Copy link + PIN" button is wired with URL + encoded-newline PIN payload
    assert "Copy link + PIN" in page.text
    assert f"/portal/{portal['slug']}&#10;PIN: {portal['pin']}" in page.text
    assert "never visited" in page.text

    # crops finish (favorite from the gallery flow + backfill)
    stem = a["stored"].rsplit(".", 1)[0]
    for _ in range(50):
        if all((jobs.crops_dir(g["id"]) / f"{stem}_{n}.jpg").is_file()
               for n in crop_slugs):
            break
        time.sleep(0.2)
    assert (jobs.crops_dir(g["id"]) / f"{stem}_1x1.jpg").is_file()

    with TestClient(app) as pub:
        # unpublished portal 404s
        assert pub.get(f"/portal/{portal['slug']}").status_code == 404
        admin.post(f"/admin/studio/clients/{c['id']}/portal/publish",
                   data={"published": "true"}, follow_redirects=False)

        # PIN gate: page, wrong PIN, lockout uses negative ids (gallery untouched)
        r = pub.get(f"/portal/{portal['slug']}")
        assert r.status_code == 200 and "PIN" in r.text
        assert pub.post(f"/portal/{portal['slug']}/pin",
                        data={"pin": "0000"}).status_code == 401
        assert db.one("SELECT gallery_id FROM pin_attempts")["gallery_id"] == -portal["id"]
        for _ in range(4):
            pub.post(f"/portal/{portal['slug']}/pin", data={"pin": "0000"})
        assert pub.post(f"/portal/{portal['slug']}/pin",
                        data={"pin": portal["pin"]}).status_code == 429
        db.run("DELETE FROM pin_attempts", ())

        # no cookie → media gated
        assert pub.get(f"/portal/{portal['slug']}/thumb/{a['id']}").status_code == 403
        assert pub.get(f"/portal/{portal['slug']}/crops.zip").status_code == 403

        # right PIN → portal renders all four sections
        r = pub.post(f"/portal/{portal['slug']}/pin", data={"pin": portal["pin"]},
                     follow_redirects=False)
        assert r.status_code == 303
        r = pub.get(f"/portal/{portal['slug']}")
        assert r.status_code == 200
        assert f"/g/{g['slug']}" in r.text                       # gallery link
        assert "Golden hour plating shot" in r.text              # captions
        assert "Social + web, 12 months." in r.text              # usage rights
        assert "logo.png" in r.text                              # brand asset
        assert f"/portal/{portal['slug']}/thumb/{a['id']}" in r.text  # crop tile
        # section-count pills tell the client at a glance what's in each section
        n_gal = db.one("SELECT COUNT(*) AS n FROM galleries WHERE client_id=? "
                       "AND published=1", (c["id"],))["n"]
        n_brand = db.one("SELECT COUNT(*) AS n FROM brand_assets WHERE client_id=?",
                         (c["id"],))["n"]
        assert f'class="section-count">{n_gal}<' in r.text   # Galleries (N)
        assert f'class="section-count">{n_brand}<' in r.text  # Brand assets (N)
        # caption-meta line surfaces the "tap to expand" hint when any gallery has captions
        assert "include caption drafts" in r.text
        # the favorites-summary line aggregates ALL faves across the client's
        # published galleries — one heart, one count, both numbers right.
        # Re-fav to ensure a known state (prior tests may have shuffled).
        existing = db.one("""SELECT f.asset_id FROM favorites f
                             JOIN assets ax ON ax.id=f.asset_id
                             JOIN galleries gx ON gx.id=ax.gallery_id
                             WHERE gx.client_id=? AND gx.published=1
                               AND ax.kind='photo' AND ax.status='ready'""",
                          (c["id"],))
        if not existing:
            v = db.one("SELECT id FROM visitors WHERE gallery_id=? LIMIT 1",
                       (g["id"],))
            if v:
                db.run("INSERT OR IGNORE INTO favorites (visitor_id, asset_id) "
                       "VALUES (?,?)", (v["id"], a["id"]))
        n_faves = db.one(
            """SELECT COUNT(DISTINCT f.asset_id) AS n FROM favorites f
               JOIN assets ax ON ax.id=f.asset_id
               JOIN galleries gx ON gx.id=ax.gallery_id
               WHERE gx.client_id=? AND gx.published=1
                 AND ax.kind='photo' AND ax.status='ready'""", (c["id"],))["n"]
        assert n_faves >= 1, "fixture chain should have left at least one favorite"
        # Re-fetch the portal so the summary reflects current state
        r2 = pub.get(f"/portal/{portal['slug']}")
        assert f"{n_faves} favorited photo" in r2.text
        assert "across 1 gallery" in r2.text
        # "request different formats" CTA deep-links to /contact with the
        # client's business prefilled + a canned message tailored to count
        assert "Need different formats" in r2.text
        assert "prefill=gallery_formats" in r2.text
        assert f"count={n_faves}" in r2.text
        # "Share this portal" mailto link with subject + body carrying the
        # portal URL + PIN (url-encoded). Useful for forwarding to a teammate.
        assert "Share this portal" in r2.text
        assert "mailto:?subject=" in r2.text
        # the PIN survives URL-encoding (digits are safe chars; colon + space encoded)
        assert f"PIN%3A%20{portal['pin']}" in r2.text
        # the portal URL appears in the encoded body — quote() leaves '/' safe
        assert f"/portal/{portal['slug']}" in r2.text

        # "NEW since last visit" pill: pin both the original gallery's
        # created_at AND the portal's last_visit to known offsets so the
        # comparison is deterministic even on fast SSD-backed machines where
        # 'datetime(now)' might collapse to the same second within a single
        # test. Original = 1h ago, last_visit = 30m ago, new gallery = "now".
        db.run("UPDATE galleries SET created_at=datetime('now', '-1 hour') "
               "WHERE id=?", (g["id"],))
        db.run("UPDATE portals SET last_visit=datetime('now', '-30 minutes') "
               "WHERE id=?", (portal["id"],))
        new_gid = db.run("INSERT INTO galleries (slug, title, pin, client_id, "
                         "published) VALUES (?,?,?,?,1)",
                         ("PortalNewPill01", "Fresh delivery", "1234", c["id"]))
        page = pub.get(f"/portal/{portal['slug']}").text
        # the fresh gallery carries a NEW pill; the original (created before
        # the rewound last_visit) does not. Anchor each row on its unique slug
        # since titles can appear elsewhere (e.g. in crop tile figcaptions).
        fresh_start = page.index(f"/g/PortalNewPill01")
        fresh_row = page[fresh_start:page.index("</li>", fresh_start)]
        assert 'class="new-pill"' in fresh_row
        orig_start = page.index(f"/g/{g['slug']}")
        orig_row = page[orig_start:page.index("</li>", orig_start)]
        assert "new-pill" not in orig_row
        # the what's-new header summarizes the same delta — "1 new gallery"
        assert "portal-changelog" in page
        assert "1 new gallery" in page
        assert "since you visited" in page

        # Subsequent visit (last_visit just got bumped to "now") → no NEW
        # pills until something new lands again. The header flips to the
        # muted "nothing new since" copy.
        page = pub.get(f"/portal/{portal['slug']}").text
        assert "new-pill" not in page
        assert "Nothing new since you visited" in page

        # remove the fresh gallery so the rest of the test sees a clean state
        db.run("DELETE FROM galleries WHERE id=?", (new_gid,))

        # crop + thumb + brand downloads
        assert pub.get(f"/portal/{portal['slug']}/thumb/{a['id']}").status_code == 200
        for ratio in crop_slugs:
            r = pub.get(f"/portal/{portal['slug']}/crop/{a['id']}/{ratio}")
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        # untrusted slug token: unknown ratio 404s
        assert pub.get(
            f"/portal/{portal['slug']}/crop/{a['id']}/16x9").status_code == 404
        # an inactive preset's slug also 404s — validation reads presets.active(),
        # so deactivating a preset immediately stops its token from resolving
        db.run("UPDATE crop_presets SET active=0 WHERE slug='9x16'")
        assert pub.get(
            f"/portal/{portal['slug']}/crop/{a['id']}/9x16").status_code == 404
        db.run("UPDATE crop_presets SET active=1 WHERE slug='9x16'")
        assert pub.get(
            f"/portal/{portal['slug']}/brand/{brand[0]['id']}").status_code == 200

        # crops ZIP: page links it, bundle has every ratio of the favorite,
        # second request reuses the cached file
        import io
        import zipfile
        from pathlib import Path as P
        from app import config as cfg
        page = pub.get(f"/portal/{portal['slug']}")
        assert f"/portal/{portal['slug']}/crops.zip" in page.text
        z = pub.get(f"/portal/{portal['slug']}/crops.zip")
        assert z.status_code == 200
        assert z.headers["content-type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        for ratio in crop_slugs:
            assert f"{P(a['filename']).stem}_{ratio}.jpg" in names
        zips = list(cfg.ZIP_DIR.glob(f"p{portal['id']}-*.zip"))
        assert len(zips) == 1
        mtime = zips[0].stat().st_mtime_ns
        assert pub.get(f"/portal/{portal['slug']}/crops.zip").status_code == 200
        assert zips[0].stat().st_mtime_ns == mtime  # served from cache, not rebuilt

        # visit tracking: authed page views count (PIN-gate + media fetches
        # don't). Original portal-lifecycle: 2; +1 NEW-pill first visit;
        # +1 NEW-pill second visit; +1 fav-summary re-fetch. Total 5.
        v = db.one("SELECT visits, last_visit FROM portals WHERE id=?", (portal["id"],))
        assert v["visits"] == 5 and v["last_visit"]
    page = admin.get(f"/admin/studio/clients/{c['id']}")
    assert "5 visits" in page.text
    # portal-audit line summarizes everything for the client at a glance
    assert 'class="muted portal-audit"' in page.text
    # the audit slice should mention "published gallery" with the count nearby
    audit_start = page.text.index('class="muted portal-audit"')
    audit = page.text[audit_start:page.text.index("</p>", audit_start)]
    assert "<strong>1</strong> published gallery" in audit
    assert "<strong>&hearts; 1</strong> favorite" in audit
    assert "KB brand" in audit
    # per-gallery row shows asset + fav counts
    assert "<th>Assets</th>" in page.text and "<th>Favorites</th>" in page.text

    # brand delete removes row + file
    from app import config as cfg
    stored = cfg.BRAND_DIR / str(c["id"]) / brand[0]["stored"]
    assert stored.is_file()
    admin.post(f"/admin/studio/clients/{c['id']}/brand/{brand[0]['id']}/delete",
               follow_redirects=False)
    assert not db.one("SELECT 1 AS x FROM brand_assets WHERE id=?", (brand[0]["id"],))
    assert not stored.exists()


def test_contact_prefill():
    with TestClient(app) as pub:
        # no prefill → form renders blank (no canned message)
        r = pub.get("/contact")
        assert r.status_code == 200 and 'name="business"' in r.text
        assert "additional formats" not in r.text

        # gallery_formats kind → friendly canned message + business carried
        r = pub.get("/contact?prefill=gallery_formats&business=Cafe+Lune&count=12")
        assert r.status_code == 200
        assert 'value="Cafe Lune"' in r.text
        assert "additional formats" in r.text
        assert "12 selects" in r.text  # plural

        # unknown prefill kind → no canned message, but business still carries
        r = pub.get("/contact?prefill=unknown&business=Other+Place&count=5")
        assert 'value="Other Place"' in r.text
        assert "additional formats" not in r.text

        # singular: 1 select (no trailing 's' between 'select' and ' I')
        r = pub.get("/contact?prefill=gallery_formats&business=Solo&count=1")
        assert "1 select I" in r.text

        # gallery_question kind → message names the gallery (drives the
        # email-gate "Have a question?" link, ship #44)
        r = pub.get("/contact?prefill=gallery_question&gallery=Spring+Menu")
        assert "question about the &#34;Spring Menu&#34;" in r.text or \
               'question about the "Spring Menu"' in r.text
        # missing gallery name → falls through without prefilled message
        r = pub.get("/contact?prefill=gallery_question")
        assert "question about" not in r.text


def test_pipeline_dashboard(admin):
    # The pipeline summary strip lives on the Activity view (the board itself is
    # strict-1:1 kanban). The per-stage overdue chip is the overdue indicator.
    page = admin.get("/admin/studio/activity")
    assert page.status_code == 200
    assert "Retainer Paid <strong>1</strong>" in page.text  # Dana's project sits at retainer paid
    assert "nothing outstanding" in page.text         # her invoice is fully paid
    # No overdue *indicator* when nothing is overdue: the per-stage chip only
    # renders for a stage that actually has past-due invoices.
    assert "pipeline-overdue" not in page.text

    # a sent invoice past its due date flags the stage chip and the summary
    p = db.one("SELECT id, status FROM projects ORDER BY id LIMIT 1")
    iid = db.run("""INSERT INTO invoices (project_id, slug, title, total_cents,
                    status, due_date) VALUES (?,?,?,?,?,?)""",
                 (p["id"], "overdue-test-slug", "Late", 50000, "sent", "2000-01-01"))
    page = admin.get("/admin/studio/activity").text
    assert "1 overdue" in page
    # per-stage chip: the project's current stage shows "(1 overdue)" inline
    # so Kevin can see which bucket of the pipeline is stuck.
    assert 'class="warn pipeline-overdue">(1 overdue)' in page
    # paid invoices stop counting even with a past due date
    db.run("UPDATE invoices SET status='paid' WHERE id=?", (iid,))
    assert "pipeline-overdue" not in admin.get("/admin/studio/activity").text
    db.run("DELETE FROM invoices WHERE id=?", (iid,))


def test_marketing_site(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))

    with TestClient(app) as pub:
        # marketing pages render and are indexable; everything else stays noindex
        for path in ("/", "/portfolio", "/about", "/contact", "/book",
                     "/work", "/services"):
            r = pub.get(path)
            assert r.status_code == 200
            assert "x-robots-tag" not in r.headers, path
            assert 'content="index, follow"' in r.text
        assert "x-robots-tag" in pub.get(f"/g/{g['slug']}").headers
        r = pub.get("/robots.txt")
        assert r.status_code == 200 and "Disallow: /g/" in r.text
        assert "x-robots-tag" not in r.headers
        assert "Sitemap: " in r.text and "/sitemap.xml" in r.text

        # sitemap lists exactly the indexable pages, crawlable itself
        r = pub.get("/sitemap.xml")
        assert r.status_code == 200 and "xml" in r.headers["content-type"]
        assert "x-robots-tag" not in r.headers
        from app import config as cfg
        for path in ("/", "/portfolio", "/about", "/contact", "/book",
                     "/work", "/services"):
            assert f"<loc>{cfg.BASE_URL}{path}</loc>" in r.text
        assert "/g/" not in r.text and "/admin" not in r.text

        # OG card present, but no og:image while nothing is starred
        r = pub.get("/")
        assert 'property="og:title"' in r.text and "og:image" not in r.text
        assert 'content="summary"' in r.text

        # portfolio gating: unflagged asset is not served publicly
        assert pub.get(f"/site/img/{a['id']}").status_code == 404
        admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/portfolio",
                   follow_redirects=False)
        assert db.one("SELECT portfolio FROM assets WHERE id=?",
                      (a["id"],))["portfolio"] == 1
        assert pub.get(f"/site/img/{a['id']}").status_code == 200
        # tiles carry data-web for the lightbox, and the overlay ships with the page
        r = pub.get("/portfolio")
        assert f'data-web="/site/img/{a["id"]}"' in r.text
        assert 'id="lightbox"' in r.text and "lightbox.js" in r.text
        # slideshow ▶ ships on the marketing lightbox too (fav/dl stay gallery-only)
        assert 'class="lb-play"' in r.text and 'class="lb-fav"' not in r.text
        r = pub.get("/")
        assert f'data-web="/site/img/{a["id"]}"' in r.text
        assert 'id="lightbox"' in r.text
        # starred photo becomes the OG share image
        assert f'property="og:image" content="{cfg.BASE_URL}/site/img/{a["id"]}"' in r.text
        assert 'content="summary_large_image"' in r.text
        # toggle off hides it again
        admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/portfolio",
                   follow_redirects=False)
        assert pub.get(f"/site/img/{a['id']}").status_code == 404


def test_inquiry_form(monkeypatch):
    from app import config, mailer
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = []
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, reply_to="":
                        sent.append((to, subject, body, reply_to)))

    with TestClient(app) as pub:
        # honeypot filled → pretend success, store nothing, send nothing
        r = pub.post("/contact", data={"name": "Bot", "email": "b@spam.com",
                                       "message": "buy now", "website": "spam.com"})
        assert r.status_code == 200 and "Thanks" in r.text
        assert db.one("SELECT COUNT(*) AS n FROM inquiries")["n"] == 0 and not sent

        # bad email rejected
        r = pub.post("/contact", data={"name": "Sam", "email": "not-an-email",
                                       "message": "hi"})
        assert r.status_code == 400

        # real inquiry: stored + emailed to Kevin with Reply-To the visitor
        r = pub.post("/contact", data={"name": "Sam Owner", "email": "sam@taqueria.com",
                                       "business": "Taqueria Luz",
                                       "message": "Need a menu shoot in July."})
        assert r.status_code == 200 and "Thanks" in r.text
        q = db.one("SELECT * FROM inquiries ORDER BY id DESC LIMIT 1")
        assert q["name"] == "Sam Owner" and q["emailed"] == 1
        to, subject, body, reply_to = sent[0]
        assert to == "kevin@example.com" and reply_to == "sam@taqueria.com"
        assert "Taqueria Luz" in body and "menu shoot" in body

        # SMTP failure: row kept with emailed=0, visitor still thanked
        def boom(*a, **kw): raise OSError("smtp down")
        monkeypatch.setattr(mailer, "send", boom)
        r = pub.post("/contact", data={"name": "Pat", "email": "pat@cafe.com",
                                       "message": "Brand partner info?"})
        assert r.status_code == 200 and "Thanks" in r.text
        q = db.one("SELECT * FROM inquiries ORDER BY id DESC LIMIT 1")
        assert q["name"] == "Pat" and q["emailed"] == 0


def test_inquiries_admin_view(admin):
    iid = db.run("INSERT INTO inquiries (name, email, business, message, emailed) "
                 "VALUES (?,?,?,?,0)",
                 ("Robin Chef", "robin@bistro.com", "Bistro Vert", "Spring menu?"))

    # the inquiry surfaces in the unified inbox; selecting it shows the business
    # as the thread name, the contact name beneath, and the real convert action.
    r = admin.get(f"/admin/inbox?sel={iid}")
    assert r.status_code == 200
    assert "Bistro Vert" in r.text and "Robin Chef" in r.text and "Spring menu?" in r.text
    assert f'action="/admin/studio/inquiries/{iid}/client"' in r.text
    assert "Create client &amp; project" in r.text

    # one click creates a client carrying the inquiry context
    r = admin.post(f"/admin/studio/inquiries/{iid}/client", follow_redirects=False)
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients WHERE email='robin@bistro.com'")
    assert c["name"] == "Robin Chef" and c["company"] == "Bistro Vert"
    assert "Spring menu?" in c["notes"]
    assert r.headers["location"] == f"/admin/studio/clients/{c['id']}"
    # the inquiry now carries a converted_at timestamp + client backref
    conv = db.one("SELECT converted_at, converted_client_id, converted_project_id "
                  "FROM inquiries WHERE id=?", (iid,))
    assert conv["converted_at"] and conv["converted_client_id"] == c["id"]
    assert conv["converted_project_id"] is None  # contact-kind, no project
    # converted inquiries leave the default inbox and land in the archived tab,
    # where the context pane links straight to the spawned client record
    assert "Bistro Vert" not in admin.get("/admin/inbox").text
    page = admin.get(f"/admin/inbox?tab=archived&sel={iid}").text
    assert "Open converted record" in page
    assert f'href="/admin/studio/clients/{c["id"]}"' in page

    # idempotent: same email → redirect to the existing client, no duplicate
    r = admin.post(f"/admin/studio/inquiries/{iid}/client", follow_redirects=False)
    assert r.headers["location"] == f"/admin/studio/clients/{c['id']}"
    assert db.one("SELECT COUNT(*) AS n FROM clients WHERE email='robin@bistro.com'")["n"] == 1

    # undo: clears the conversion stamps but LEAVES the spawned client alone
    # (it may already carry edits, brand assets, projects by the time Kevin
    # realizes the misclick).
    r = admin.post(f"/admin/studio/inquiries/{iid}/unconvert",
                   follow_redirects=False)
    assert r.status_code == 303
    conv = db.one("SELECT converted_at, converted_client_id, converted_project_id "
                  "FROM inquiries WHERE id=?", (iid,))
    assert conv["converted_at"] is None
    assert conv["converted_client_id"] is None
    # client still exists — the unconvert is NOT a cascade delete
    assert db.one("SELECT id FROM clients WHERE id=?", (c["id"],)) is not None
    # the inquiry re-appears in the default inbox as actionable again
    page = admin.get(f"/admin/inbox?sel={iid}").text
    assert "Bistro Vert" in page
    assert "Create client &amp; project" in page

    assert admin.post("/admin/studio/inquiries/99999/client").status_code == 404
    assert admin.post("/admin/studio/inquiries/99999/unconvert").status_code == 404


def test_booking_flow(monkeypatch, admin):
    # The public booking surface is the scheduler: GET /book lists event types,
    # GET /book/{slug} renders a slot picker, POST /book/{slug} claims a slot.
    # A confirmed real-shoot booking find-or-creates a Studio client + project and
    # emails both sides. (The old free-text inquiry form at bare /book is gone.)
    import datetime as dt
    from app import config, mailer, scheduling as S
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, reply_to="", ics=None:
                        sent.append((to, subject, body, reply_to)))

    # Seed a real-shoot event type (creates_notion_session=1 → spawns a project)
    # with Mon–Fri 9:00–17:00 availability and 1h notice so a near slot is open.
    eid = db.run("""INSERT INTO event_types
        (slug, name, duration_min, min_notice_hours, booking_window_days,
         max_per_day, creates_notion_session, location, active)
        VALUES (?,?,?,?,?,?,?,?,1)""",
        ("fb-shoot", "Food Shoot", 60, 1, 60, 0, 1, "On-site"))
    for wd in range(5):
        db.run("INSERT INTO availability_rules (event_type_id, weekday, start_min, "
               "end_min) VALUES (?,?,?,?)", (eid, wd, 540, 1020))

    et = S.event_by_slug("fb-shoot")
    day = dt.date.today() + dt.timedelta(days=3)   # ≥3d out so notice never clips
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    slots = S.slots_for_day(et, day)
    assert slots, "seed produced no open slots"
    start = slots[0]["utc"]

    def n_bookings(status=None):
        if status:
            return db.one("SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=? "
                          "AND status=?", (eid, status))["n"]
        return db.one("SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=?",
                      (eid,))["n"]

    with TestClient(app) as pub:
        # nav lifts the booking link on every site page
        for path in ("/", "/portfolio", "/about", "/contact", "/book"):
            assert 'href="/book"' in pub.get(path).text, path

        # /book lists the active event; /book/{slug} renders its picker form
        assert "Food Shoot" in pub.get("/book").text
        r = pub.get("/book/fb-shoot")
        # bare GET renders the slot-picker calendar (the POST confirm form only
        # appears once a time is selected); its day links carry the slug
        assert r.status_code == 200 and "Food Shoot" in r.text
        assert 'href="/book/fb-shoot?' in r.text
        # unknown slug → 404
        assert pub.get("/book/nope").status_code == 404

        # invalid email → 400, nothing stored
        r = pub.post("/book/fb-shoot", data={"name": "Mara", "email": "not-email",
                     "start": start, "tz": "America/New_York"})
        assert r.status_code == 400
        assert n_bookings() == 0

        # honeypot pretends success silently (303 → /book), nothing stored
        r = pub.post("/book/fb-shoot", data={"name": "Bot", "email": "b@spam.com",
                     "start": start, "tz": "America/New_York", "website": "spam.com"},
                     follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/book"
        assert n_bookings() == 0

        # happy path: 303 → /booking/{token}; booking confirmed + F&B intake stored
        r = pub.post("/book/fb-shoot", data={
            "name": "Mara Chef", "email": "Booking-Flow@Test.cafe", "phone": "555-0100",
            "start": start, "tz": "America/New_York", "notes": "Spring menu launch.",
            "venue_address": "12 Vine St", "dish_count": "40",
            "parking_notes": "Loading dock out back", "style_refs": "bright, airy",
            "onsite_contact": "Lou 555-0199"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/booking/")
        token = r.headers["location"].rsplit("/", 1)[-1]
        b = db.one("SELECT * FROM bookings WHERE token=?", (token,))
        assert b and b["status"] == "confirmed" and b["start_utc"] == start
        assert b["email"] == "booking-flow@test.cafe"   # normalized to lowercase
        assert b["venue_address"] == "12 Vine St" and b["dish_count"] == "40"
        # confirmation emails fired to both client and Kevin
        assert any(to == "booking-flow@test.cafe" for to, *_ in sent)
        assert any(to == "kevin@example.com" for to, *_ in sent)

        # double-book the same instant → 409 (slot taken), no second booking
        r = pub.post("/book/fb-shoot", data={"name": "Dup", "email": "d@cafe.com",
                     "start": start, "tz": "America/New_York"})
        assert r.status_code == 409
        assert n_bookings("confirmed") == 1

    # the booking auto-linked a Studio client + project (real-shoot event type)
    assert b["client_id"] and b["project_id"]
    c = db.one("SELECT * FROM clients WHERE id=?", (b["client_id"],))
    assert c["email"] == "booking-flow@test.cafe"
    p = db.one("SELECT * FROM projects WHERE id=?", (b["project_id"],))
    assert p["shoot_date"] == start[:10] and "Food Shoot" in p["title"]
    assert p["status"] == "inquiry_received"

    # admin studio surfaces the auto-created project on the pipeline board
    assert p["title"] in admin.get("/admin/studio").text

    # Tear down everything this test seeded — the suite shares one module DB, and
    # a left-behind confirmed booking (upcoming shoot_date) would pollute the
    # empty-calendar baselines in the studio strip / conflict tests downstream.
    # FK order: bookings + inquiries hold refs to client/project, so clear them
    # before the rows they point at.
    db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
    if b["inquiry_id"]:
        db.run("DELETE FROM inquiries WHERE id=?", (b["inquiry_id"],))
    db.run("DELETE FROM projects WHERE id=?", (b["project_id"],))
    db.run("DELETE FROM clients WHERE id=?", (b["client_id"],))
    db.run("DELETE FROM availability_rules WHERE event_type_id=?", (eid,))
    db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_details_persistence_wiring(admin):
    # the static helper is loaded by base.html and reachable through the tunnel
    # path equivalent the dev TestClient serves
    with TestClient(app) as pub:
        js = pub.get("/static/details_persist.js").text
    assert "localStorage" in js and "details[id]" in js
    # base.html ships the script tag with the cache-buster query
    page = admin.get("/admin/galleries").text
    assert "details_persist.js?v=" in page
    # NOTE: the dashboard orphan-picker <details id="d-orphans"> was stripped for
    # strict-1:1 (header->pills->grid). The persistence wiring it exercised is
    # still proven below by the gallery-detail Settings/Send-email <details>.
    # The orphan-linker re-homes in the phase-2 re-link.

    # gallery admin pages expose the Settings + Send delivery email details IDs
    g = db.one("SELECT id FROM galleries ORDER BY id LIMIT 1")
    gpage = admin.get(f"/admin/galleries/{g['id']}").text
    assert 'id="g-settings"' in gpage
    # Send delivery email block only appears for published galleries
    db.run("UPDATE galleries SET published=1 WHERE id=?", (g["id"],))
    gpage = admin.get(f"/admin/galleries/{g['id']}").text
    assert 'id="g-send-email"' in gpage


def test_dashboard_unlinked_warning(admin):
    # The orphan-gallery warning (published galleries with no studio client) was
    # stripped from the strict-1:1 galleries grid (prototype card has no warn
    # glyph) and re-homed to the Home dashboard, where the studio's other
    # needs-attention nudges live. Same inline one-click link-client picker.
    import re
    def n_warned():
        m = re.search(r"(\d+) published galler", admin.get("/admin/home").text)
        return int(m.group(1)) if m else 0
    baseline = n_warned()

    # unpublished gallery with no client → does NOT bump the count (could be a
    # draft, not worth nagging about)
    gid_draft = db.run("INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
                       ("unlinked-draft-1", "Loose draft", "1234"))
    assert n_warned() == baseline

    # publish it → count bumps by 1, inline picker now offers this gallery
    db.run("UPDATE galleries SET published=1 WHERE id=?", (gid_draft,))
    assert n_warned() == baseline + 1
    page = admin.get("/admin/home").text
    assert "unlinked-warn" in page  # strip is now visible regardless of baseline
    # the orphan picker strip is the single place orphans surface now — the
    # per-card warning glyph was dropped in the strict-1:1 grid (prototype card
    # has no warn icon). The strip lists this gallery with an inline link picker.
    assert f"/admin/galleries/{gid_draft}/link-client" in page

    # use the inline picker to link to a client — count drops back, strip clears
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Linker Co",))
    r = admin.post(f"/admin/galleries/{gid_draft}/link-client",
                   data={"client_id": str(cid)}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT client_id FROM galleries WHERE id=?",
                  (gid_draft,))["client_id"] == cid
    assert n_warned() == baseline
    page = admin.get("/admin/home").text
    assert f"/admin/galleries/{gid_draft}/link-client" not in page
    # link-client refuses bogus client_id; the gallery's client_id isn't touched
    r = admin.post(f"/admin/galleries/{gid_draft}/link-client",
                   data={"client_id": "999999"}, follow_redirects=False)
    assert r.status_code == 400
    assert db.one("SELECT client_id FROM galleries WHERE id=?",
                  (gid_draft,))["client_id"] == cid

    # ship #53's force-delete of a client unlinks galleries → count returns
    admin.post(f"/admin/studio/clients/{cid}/delete",
               data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT client_id FROM galleries WHERE id=?",
                  (gid_draft,))["client_id"] is None
    assert n_warned() == baseline + 1

    # cleanup
    db.run("DELETE FROM galleries WHERE id=?", (gid_draft,))


def test_client_delete_safety(admin):
    from app import config as cfg

    # empty client: deletes cleanly without force
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Empty Co",))
    r = admin.post(f"/admin/studio/clients/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (cid,)) is None

    # client with brand assets: refused without force; force-delete tears down
    # the brand dir on disk too.
    cid2 = db.run("INSERT INTO clients (name) VALUES (?)", ("Brand Co",))
    bdir = cfg.BRAND_DIR / str(cid2)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "logo.png").write_bytes(b"x")
    db.run("INSERT INTO brand_assets (client_id, filename, stored, bytes) "
           "VALUES (?,?,?,?)", (cid2, "logo.png", "logo.png", 1))
    r = admin.post(f"/admin/studio/clients/{cid2}/delete",
                   follow_redirects=False)
    assert r.status_code == 400
    assert "brand asset" in r.json()["detail"]
    assert db.one("SELECT id FROM clients WHERE id=?", (cid2,)) is not None
    r = admin.post(f"/admin/studio/clients/{cid2}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (cid2,)) is None
    assert not bdir.exists()  # disk cleanup

    # client with linked gallery: refused; force unlinks the gallery (no
    # ON DELETE clause on galleries.client_id → manual UPDATE).
    cid3 = db.run("INSERT INTO clients (name) VALUES (?)", ("Linked Co",))
    gid = db.run("INSERT INTO galleries (slug, title, pin, client_id) "
                 "VALUES (?,?,?,?)", ("client-del-glink", "Linked", "1234", cid3))
    r = admin.post(f"/admin/studio/clients/{cid3}/delete",
                   follow_redirects=False)
    assert r.status_code == 400 and "linked galler" in r.json()["detail"]
    r = admin.post(f"/admin/studio/clients/{cid3}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (cid3,)) is None
    # gallery survives, unlinked
    surv = db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))
    assert surv is not None and surv["client_id"] is None
    db.run("DELETE FROM galleries WHERE id=?", (gid,))  # tidy up

    # client with portal visits: blocked + listed
    cid4 = db.run("INSERT INTO clients (name) VALUES (?)", ("Visited Co",))
    db.run("INSERT INTO portals (client_id, slug, pin, visits) VALUES (?,?,?,5)",
           (cid4, "client-del-portal", "1234"))
    r = admin.post(f"/admin/studio/clients/{cid4}/delete",
                   follow_redirects=False)
    assert r.status_code == 400 and "portal with 5 visits" in r.json()["detail"]
    # client detail page shows the same blockers + a button that carries force
    page = admin.get(f"/admin/studio/clients/{cid4}").text
    assert "portal with 5 visits" in page
    assert 'name="force" value="1"' in page
    # cleanup
    admin.post(f"/admin/studio/clients/{cid4}/delete", data={"force": "1"},
               follow_redirects=False)

    # client with favorites in a linked gallery
    cid5 = db.run("INSERT INTO clients (name) VALUES (?)", ("Faved Co",))
    gid5 = db.run("INSERT INTO galleries (slug, title, pin, client_id, published) "
                  "VALUES (?,?,?,?,1)",
                  ("client-del-favs", "Faved", "1234", cid5))
    aid5 = db.run("INSERT INTO assets (gallery_id, kind, filename, stored, status) "
                  "VALUES (?,?,?,?,?)",
                  (gid5, "photo", "f.jpg", "favfile.jpg", "ready"))
    vid5 = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
                  (gid5, "vtok-cldel"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid5, aid5))
    r = admin.post(f"/admin/studio/clients/{cid5}/delete",
                   follow_redirects=False)
    assert r.status_code == 400 and "favorite" in r.json()["detail"]
    db.run("DELETE FROM galleries WHERE id=?", (gid5,))
    db.run("DELETE FROM clients WHERE id=?", (cid5,))

    # 404 on unknown id
    assert admin.post("/admin/studio/clients/99999/delete",
                      follow_redirects=False).status_code == 404


def test_gallery_delete(admin):
    from app import config as cfg
    admin.post("/admin/galleries", data={"title": "Doomed", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/galleries/{g['id']}/upload",
               files=[("files", ("bye.jpg", _jpeg_bytes(), "image/jpeg"))])
    media_dir = cfg.MEDIA_DIR / str(g["id"])
    assert media_dir.is_dir()

    # delete button only offered while unpublished; published galleries refuse
    # (two-step on purpose — a live client link shouldn't vanish on one click)
    assert "Delete gallery" in admin.get(f"/admin/galleries/{g['id']}").text
    db.run("UPDATE galleries SET published=1 WHERE id=?", (g["id"],))
    assert "Delete gallery" not in admin.get(f"/admin/galleries/{g['id']}").text
    assert admin.post(f"/admin/galleries/{g['id']}/delete",
                      follow_redirects=False).status_code == 400
    db.run("UPDATE galleries SET published=0 WHERE id=?", (g["id"],))

    # deleting the cover asset clears the dangling cover_asset_id
    a = db.one("SELECT id FROM assets WHERE gallery_id=?", (g["id"],))
    admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/cover")
    assert db.one("SELECT cover_asset_id FROM galleries WHERE id=?",
                  (g["id"],))["cover_asset_id"] == a["id"]
    admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/delete")
    assert db.one("SELECT cover_asset_id FROM galleries WHERE id=?",
                  (g["id"],))["cover_asset_id"] is None

    r = admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g["id"],))
    assert not db.one("SELECT 1 AS x FROM assets WHERE gallery_id=?", (g["id"],))
    assert not media_dir.exists()
    assert admin.post(f"/admin/galleries/{g['id']}/delete",
                      follow_redirects=False).status_code == 404

    # portal-favorites safety: an unpublished gallery linked to a client with
    # a portal AND has favorited photos can't be silently deleted (would break
    # the client's social-crops view). Require force=1 as opt-in.
    admin.post("/admin/galleries", data={"title": "PortalSafetyTest",
                                          "client_name": ""},
               follow_redirects=False)
    g2 = db.one("SELECT * FROM galleries WHERE title='PortalSafetyTest'")
    admin.post(f"/admin/galleries/{g2['id']}/upload",
               files=[("files", ("safe.jpg", _jpeg_bytes(), "image/jpeg"))])
    a2 = db.one("SELECT id FROM assets WHERE gallery_id=?", (g2["id"],))
    # plant a client + portal + linked favorite
    safety_cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Safety Co",))
    db.run("INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
           (safety_cid, "safety-portal-slug", "1234"))
    db.run("UPDATE galleries SET client_id=? WHERE id=?", (safety_cid, g2["id"]))
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
                 (g2["id"], "vtok-safety"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, a2["id"]))

    # gallery admin page surfaces the portal-fav count + safety copy
    page = admin.get(f"/admin/galleries/{g2['id']}").text
    assert "with 1 portal fav" in page
    # plain delete refused with explanatory 400
    r = admin.post(f"/admin/galleries/{g2['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 400
    assert "social-crops" in r.json()["detail"] or "social-crops" in r.text
    # gallery still exists after the refused delete
    assert db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g2["id"],))

    # force=1 lets it through
    r = admin.post(f"/admin/galleries/{g2['id']}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert not db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g2["id"],))


def test_asset_reorder(admin):
    admin.post("/admin/galleries", data={"title": "Ordered", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/galleries/{g['id']}/upload",
               files=[("files", (f"{n}.jpg", _jpeg_bytes(), "image/jpeg")) for n in "abc"])

    def order():  # same ORDER BY the public gallery uses — this IS the client-facing order
        return [r["id"] for r in db.all_(
            "SELECT id FROM assets WHERE gallery_id=? ORDER BY position, id", (g["id"],))]

    a1, a2, a3 = order()
    # move last one earlier; whole section gets renumbered from the legacy all-zero state
    r = admin.post(f"/admin/galleries/{g['id']}/assets/{a3}/move",
                   data={"dir": "left"}, follow_redirects=False)
    assert r.status_code == 303
    assert order() == [a1, a3, a2]
    # edge is a no-op, not an error
    admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/move", data={"dir": "left"})
    assert order() == [a1, a3, a2]
    admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/move", data={"dir": "right"})
    assert order() == [a3, a1, a2]
    # bad direction 400s, unknown asset 404s
    assert admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/move",
                      data={"dir": "up"}, follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/galleries/{g['id']}/assets/999999/move",
                      data={"dir": "left"}, follow_redirects=False).status_code == 404
    # arrows render on the admin grid
    assert "Move earlier" in admin.get(f"/admin/galleries/{g['id']}").text

    # bulk section assignment: two at once, third untouched (stays in the section
    # it was uploaded into — the gallery's default first section)
    default_sec = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (g["id"],))["id"]
    s = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Drinks'", (g["id"],))
    r = admin.post(f"/admin/galleries/{g['id']}/assets/bulk-section",
                   data={"section_id": str(s["id"]), "asset_ids": [str(a1), str(a2)]},
                   follow_redirects=False)
    assert r.status_code == 303
    secs = {row["id"]: row["section_id"] for row in db.all_(
        "SELECT id, section_id FROM assets WHERE gallery_id=?", (g["id"],))}
    assert secs[a1] == s["id"] and secs[a2] == s["id"] and secs[a3] == default_sec
    # empty section_id moves back to (none)
    admin.post(f"/admin/galleries/{g['id']}/assets/bulk-section",
               data={"section_id": "", "asset_ids": [str(a1)]})
    assert db.one("SELECT section_id FROM assets WHERE id=?", (a1,))["section_id"] is None
    # a section the gallery doesn't own is rejected
    assert admin.post(f"/admin/galleries/{g['id']}/assets/bulk-section",
                      data={"section_id": "999999", "asset_ids": [str(a1)]},
                      follow_redirects=False).status_code == 400


def test_section_rename_reorder(admin):
    admin.post("/admin/galleries", data={"title": "Sectioned", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")

    def order():  # public gallery chapters render in this order
        return [r["name"] for r in db.all_(
            "SELECT name FROM sections WHERE gallery_id=? ORDER BY position, id",
            (g["id"],))]

    names = order()
    assert names[0] == "Hero Dishes"  # F&B presets seeded
    first = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Hero Dishes'",
                   (g["id"],))

    # rename keeps assets attached (no delete/re-add dance)
    r = admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/rename",
                   data={"name": "Signature Dishes"}, follow_redirects=False)
    assert r.status_code == 303
    assert order()[0] == "Signature Dishes"
    assert admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/rename",
                      data={"name": "  "}, follow_redirects=False).status_code == 400

    # move down swaps with the neighbor; edge moves no-op
    admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move",
               data={"dir": "down"})
    assert order()[1] == "Signature Dishes"
    admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move",
               data={"dir": "up"})
    admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move",
               data={"dir": "up"})  # already first — no-op
    assert order()[0] == "Signature Dishes"

    # bad input
    assert admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move",
                      data={"dir": "sideways"}, follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/galleries/{g['id']}/sections/999999/move",
                      data={"dir": "up"}, follow_redirects=False).status_code == 404


def test_upload_defaults_to_first_section(admin):
    """New uploads land in the gallery's first section (display order), not the
    catch-all 'More'. Explicit choices are still honored; a gallery with no
    sections keeps the clean None default."""
    admin.post("/admin/galleries", data={"title": "Default Section Gal", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    secs = db.all_("SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id",
                   (g["id"],))
    assert len(secs) >= 2  # F&B presets seed sections on create

    # no section_id given → lands in the first section, not unsectioned
    admin.post(f"/admin/galleries/{g['id']}/upload",
               files=[("files", ("hero.jpg", _jpeg_bytes(), "image/jpeg"))])
    a = db.one("SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1",
               (g["id"],))
    assert a["section_id"] == secs[0]["id"]

    # an explicit section_id is honored, not overridden by the default
    admin.post(f"/admin/galleries/{g['id']}/upload?section_id={secs[1]['id']}",
               files=[("files", ("interior.jpg", _jpeg_bytes(), "image/jpeg"))])
    a = db.one("SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1",
               (g["id"],))
    assert a["section_id"] == secs[1]["id"]

    # sectionless gallery → section_id stays None (current clean default preserved)
    db.run("DELETE FROM sections WHERE gallery_id=?", (g["id"],))
    admin.post(f"/admin/galleries/{g['id']}/upload",
               files=[("files", ("loose.jpg", _jpeg_bytes(), "image/jpeg"))])
    a = db.one("SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1",
               (g["id"],))
    assert a["section_id"] is None


def test_section_jump_nav(admin):
    import time
    admin.post("/admin/galleries", data={"title": "Navved", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        admin.post(f"/admin/galleries/{g['id']}/upload",
                   files=[("files", (f"{n}.jpg", _jpeg_bytes(), "image/jpeg")) for n in "ab"])
        for _ in range(50):
            rows = db.all_("SELECT status FROM assets WHERE gallery_id=?", (g["id"],))
            if rows and all(r["status"] == "ready" for r in rows):
                break
            time.sleep(0.2)
        assert all(r["status"] == "ready" for r in rows)

    a1, a2 = [r["id"] for r in db.all_(
        "SELECT id FROM assets WHERE gallery_id=? ORDER BY id", (g["id"],))]
    hero = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Hero Dishes'",
                  (g["id"],))
    admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/section",
               data={"section_id": str(hero["id"])})
    # a2 to the unsectioned "More" bucket so we have one section + More = 2 targets
    # (uploads now default into the first section, so push it back out)
    admin.post(f"/admin/galleries/{g['id']}/assets/bulk-section",
               data={"section_id": "", "asset_ids": [str(a2)]})
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": "Navved", "pin": "5151", "published": "true"})

    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "5151"})
        # one populated section + unsectioned "More" = 2 targets → nav renders
        r = pub.get(f"/g/{g['slug']}")
        assert "section-nav" in r.text
        assert f'href="#sec-{hero["id"]}"' in r.text and 'id="sec-more"' in r.text

        # per-section ZIP: heading carries ↓, email gate first, then exact bundle
        assert f'/g/{g["slug"]}/download/section/{hero["id"]}' in r.text
        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}",
                     follow_redirects=False)
        assert r2.status_code == 303  # no email yet → gate
        # /download?section=N must render the gate (catches decorator-misplacement)
        gate = pub.get(f"/g/{g['slug']}/download?section={hero['id']}")
        assert gate.status_code == 200 and 'name="section"' in gate.text
        assert f'value="{hero["id"]}"' in gate.text
        pub.post(f"/g/{g['slug']}/email",
                 data={"email": "nav@bistro.com", "section": str(hero["id"])},
                 follow_redirects=False)
        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}")
        assert r2.headers["content-type"] == "application/zip"
        assert zipfile.ZipFile(io.BytesIO(r2.content)).namelist() == ["a.jpg"]
        # empty and foreign sections refuse
        empty = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Drinks'",
                       (g["id"],))
        assert pub.get(f"/g/{g['slug']}/download/section/{empty['id']}").status_code == 404
        assert pub.get(f"/g/{g['slug']}/download/section/999999").status_code == 404

        # collapse everything into one chapter → nav disappears (nothing to jump between)
        admin.post(f"/admin/galleries/{g['id']}/assets/{a2}/section",
                   data={"section_id": str(hero["id"])})
        r = pub.get(f"/g/{g['slug']}")
        assert "section-nav" not in r.text and f'id="sec-{hero["id"]}"' in r.text

        # section content changed → new content-keyed bundle, old rev pruned
        from app import config
        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}")
        assert sorted(zipfile.ZipFile(io.BytesIO(r2.content)).namelist()) == ["a.jpg", "b.jpg"]
        assert len(list(config.ZIP_DIR.glob(f"g{g['id']}-s{hero['id']}-*.zip"))) == 1


def test_jobs_admin_view(admin):
    import time

    # plant a failed job whose handler no-ops cleanly on retry (asset gone)
    jid = db.run("INSERT INTO jobs (kind, payload, status, attempts, error) VALUES "
                 "('social_crops', '{\"asset_id\": 999999}', 'failed', 3, 'boom')")

    # dashboard badge + jobs page surface the failure (R14: no silent failures).
    # The 1:1 reskin moved the badge from the old nav strip into the Galleries
    # topbar subtitle as an oxblood warning linking to /admin/jobs.
    assert "1 job failed" in admin.get("/admin/galleries").text
    page = admin.get("/admin/jobs")
    assert page.status_code == 200
    assert "social_crops" in page.text and "boom" in page.text
    assert f"/admin/jobs/{jid}/retry" in page.text

    # retry requeues, resets attempts, and the job runs to done
    # (fresh lifespan: earlier tests' nested TestClient exits stop the pool)
    with TestClient(app):
        r = admin.post(f"/admin/jobs/{jid}/retry", follow_redirects=False)
        assert r.status_code == 303
        for _ in range(50):
            if db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "done":
                break
            time.sleep(0.1)
    j = db.one("SELECT status, error, attempts FROM jobs WHERE id=?", (jid,))
    assert j["status"] == "done" and j["error"] is None and j["attempts"] == 1

    # only failed jobs are retryable
    assert admin.post(f"/admin/jobs/{jid}/retry",
                      follow_redirects=False).status_code == 404


def test_case_studies(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    photos = db.all_("SELECT * FROM assets WHERE gallery_id=? AND kind='photo'",
                     (g["id"],))
    assert photos, "fixture should leave at least one photo to star"

    with TestClient(app) as pub:
        # before publishing: /work is empty, /work/{slug} 404s, sitemap silent
        r = pub.get("/work")
        assert r.status_code == 200 and "New work is being curated" in r.text
        assert pub.get(f"/work/{g['slug']}").status_code == 404
        sm = pub.get("/sitemap.xml").text
        assert f"/work/{g['slug']}" not in sm

        # star a photo + fill case-study fields via the admin settings form
        admin.post(f"/admin/galleries/{g['id']}/assets/{photos[0]['id']}/portfolio",
                   follow_redirects=False)
        admin.post(f"/admin/galleries/{g['id']}/settings",
                   data={"title": g["title"], "client_name": g["client_name"] or "",
                         "pin": g["pin"], "expires_at": "", "published": "true",
                         "captions": "", "cs_published": "true",
                         "cs_tagline": "Spring menu for Café Lune",
                         "cs_brief": "A 40-dish refresh shot over two days.",
                         "cs_credits": "Chef: Mara Sun\nStylist: Lou Mendez",
                         "cs_location": "Cleveland, OH"})

        # /work index lists the study with hero + tagline; tile links to /work/{slug}
        r = pub.get("/work")
        assert "Spring menu for Café Lune" in r.text
        assert f'href="/work/{g["slug"]}"' in r.text
        assert f'/site/img/{photos[0]["id"]}' in r.text
        assert 'content="index, follow"' in r.text and "x-robots-tag" not in r.headers

        # /work/{slug} renders brief, credits, location, photo, and OG/SEO meta
        r = pub.get(f"/work/{g['slug']}")
        assert r.status_code == 200
        assert "x-robots-tag" not in r.headers
        assert "Spring menu for Café Lune" in r.text
        assert "40-dish refresh" in r.text
        assert "Chef: Mara Sun" in r.text and "Stylist: Lou Mendez" in r.text
        assert "Cleveland, OH" in r.text
        from app import config as cfg
        assert (f'property="og:image" content="{cfg.BASE_URL}'
                f'/site/img/{photos[0]["id"]}"') in r.text
        assert 'property="og:type" content="article"' in r.text
        assert 'name="description"' in r.text and "40-dish refresh" in r.text
        # brief in the og:description too (first 200 chars)
        assert 'property="og:description" content="A 40-dish refresh' in r.text

        # sitemap now lists the case study; robots.txt unchanged (no exclusion needed)
        sm = pub.get("/sitemap.xml").text
        assert f"<loc>{cfg.BASE_URL}/work/{g['slug']}</loc>" in sm
        assert f"<loc>{cfg.BASE_URL}/work</loc>" in sm

        # noindex on a non-/work prefix stays noindex (middleware is path-prefixed)
        assert "x-robots-tag" in pub.get(f"/g/{g['slug']}").headers

        # unpublishing the case study hides it again, without touching the client gallery
        admin.post(f"/admin/galleries/{g['id']}/settings",
                   data={"title": g["title"], "client_name": g["client_name"] or "",
                         "pin": g["pin"], "expires_at": "", "published": "true",
                         "captions": "", "cs_tagline": "", "cs_brief": "",
                         "cs_credits": "", "cs_location": ""})
        assert pub.get(f"/work/{g['slug']}").status_code == 404
        assert "New work is being curated" in pub.get("/work").text
        # client gallery still serves — the case-study flag is independent
        assert pub.get(f"/g/{g['slug']}").status_code == 200


def test_share_debugger(admin):
    from app import config

    # baseline: marketing pages always listed; case-studies section only when
    # there's at least one published study
    r = admin.get("/admin/share")
    assert r.status_code == 200
    assert "Marketing pages" in r.text
    # all marketing-page paths present
    for path in ("/", "/portfolio", "/work", "/services", "/about",
                 "/book", "/contact"):
        assert f"{config.BASE_URL}{path}" in r.text, path
    # per-row debugger links (Facebook + LinkedIn + OpenGraph.xyz)
    assert "developers.facebook.com/tools/debug" in r.text
    assert "linkedin.com/post-inspector" in r.text
    assert "opengraph.xyz/url/" in r.text
    # URLs are url-encoded so colons + slashes survive the inspector links
    assert "https%3A" in r.text or "http%3A" in r.text

    # the 1:1 reskin dropped the old Galleries nav strip; Share is now reached
    # from any admin page via the ⌘K command palette (a JS-built CMDS entry).
    assert '"/admin/share"' in admin.get("/admin/galleries").text

    # publish a case study and confirm it appears with its specific OG values
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    a = db.one("SELECT id FROM assets WHERE gallery_id=? AND kind='photo'",
               (g["id"],))
    # ensure starred (the toggle endpoint XORs, which would unstar if a prior
    # test already starred this asset)
    db.run("UPDATE assets SET portfolio=1 WHERE id=?", (a["id"],))
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": g["title"], "client_name": g["client_name"] or "",
                     "pin": g["pin"], "expires_at": "", "published": "true",
                     "captions": "", "cs_published": "true",
                     "cs_tagline": "Spring dish series",
                     "cs_brief": "A two-day shoot covering the spring menu refresh.",
                     "cs_credits": "", "cs_location": "Asheville, NC"})
    saved = db.one("SELECT cs_published, cs_location, cs_tagline FROM galleries WHERE id=?",
                   (g["id"],))
    assert saved["cs_published"] == 1
    assert saved["cs_location"] == "Asheville, NC"
    assert saved["cs_tagline"] == "Spring dish series"
    r = admin.get("/admin/share")
    assert "Case studies" in r.text
    assert f"/work/{g['slug']}" in r.text
    assert "Spring dish series" in r.text  # cs_tagline became og:title
    assert "spring menu refresh" in r.text  # cs_brief became description
    # Jinja escapes the comma differently? No — just look for Asheville
    assert "Asheville" in r.text
    # a hero photo thumb shows as the og:image preview (whichever portfolio-
    # starred asset is newest for this gallery, not necessarily `a`)
    import re
    assert re.search(r'/site/img/\d+\?variant=thumb', r.text)

    # unpublish → case study drops off the debugger
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": g["title"], "client_name": g["client_name"] or "",
                     "pin": g["pin"], "expires_at": "", "published": "true",
                     "captions": "", "cs_tagline": "", "cs_brief": "",
                     "cs_credits": "", "cs_location": ""})
    r = admin.get("/admin/share")
    assert "Spring dish series" not in r.text


def test_section_captions(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    # Seed a fresh section + asset so this test doesn't depend on prior
    # tests' shuffling. Public gallery skips sections with no ready assets.
    sec_id = db.run("INSERT INTO sections (gallery_id, name, position) "
                    "VALUES (?,?,?)",
                    (g["id"], "Captioned Chapter", 99))
    db.run("INSERT INTO assets (gallery_id, section_id, kind, filename, "
           "stored, status) VALUES (?,?,?,?,?,?)",
           (g["id"], sec_id, "photo", "cap.jpg",
            "cafe1234deadbeef.jpg", "ready"))
    sec = {"id": sec_id, "name": "Captioned Chapter"}

    with TestClient(app) as pub:
        # baseline: no caption → no <p class="section-caption"> rendered
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]},
                 follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert "section-caption" not in r.text

    # admin sets a caption
    admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
               data={"caption": "Hero dishes from the spring menu."},
               follow_redirects=False)
    assert db.one("SELECT caption FROM sections WHERE id=?",
                  (sec["id"],))["caption"] == "Hero dishes from the spring menu."

    # admin gallery page shows the caption pre-filled in the form
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert 'name="caption"' in r.text
    assert 'value="Hero dishes from the spring menu."' in r.text

    # public gallery renders the caption under the section heading
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]},
                 follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert 'class="section-caption"' in r.text
        assert "Hero dishes from the spring menu." in r.text
        # caption sits between the section h2 and the grid div
        heading_at = r.text.index(f'id="sec-{sec["id"]}"')
        grid_at = r.text.index("Hero dishes from the spring menu.")
        assert heading_at < grid_at

    # clearing caption (empty string) → NULL → not rendered
    admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
               data={"caption": ""}, follow_redirects=False)
    assert db.one("SELECT caption FROM sections WHERE id=?",
                  (sec["id"],))["caption"] is None
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]},
                 follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert "section-caption" not in r.text
        assert "Hero dishes from the spring menu." not in r.text

    # whitespace-only collapses to NULL too
    admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
               data={"caption": "   "}, follow_redirects=False)
    assert db.one("SELECT caption FROM sections WHERE id=?",
                  (sec["id"],))["caption"] is None


def test_portfolio_tag_filter(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    # plant 3 fresh portfolio-eligible photos in this gallery
    ids = []
    for i in range(3):
        aid = db.run("INSERT INTO assets (gallery_id, kind, filename, stored, "
                     "status, portfolio) VALUES (?,?,?,?,?,?)",
                     (g["id"], "photo", f"p{i}.jpg",
                      f"feedface0{i}feedface.jpg", "ready", 1))
        ids.append(aid)

    with TestClient(app) as pub:
        # baseline: no tags → no filter chip nav; tiles render flat
        r = pub.get("/portfolio")
        assert r.status_code == 200
        assert "portfolio-filter" not in r.text
        assert "pf-chip" not in r.text
        # untagged tiles don't carry data-tag
        for aid in ids:
            assert f'data-web="/site/img/{aid}"' in r.text
            assert f'data-tag=' not in r.text or f'data-tag="" data-web="/site/img/{aid}"' not in r.text

    # admin sets tags via the tag endpoint
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[0]}/tag",
               data={"portfolio_tag": "Dishes"}, follow_redirects=False)
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[1]}/tag",
               data={"portfolio_tag": "Dishes"}, follow_redirects=False)
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[2]}/tag",
               data={"portfolio_tag": "Drinks"}, follow_redirects=False)
    # db round-trip
    assert db.one("SELECT portfolio_tag FROM assets WHERE id=?",
                  (ids[0],))["portfolio_tag"] == "Dishes"

    # admin gallery page: portfolio-starred tiles render a tag form pre-filled;
    # the datalist of suggestions is also present once
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert 'list="tag-suggestions"' in r.text
    assert r.text.count('id="tag-suggestions"') == 1
    assert 'value="Dishes"' in r.text and 'value="Drinks"' in r.text

    with TestClient(app) as pub:
        r = pub.get("/portfolio")
        # filter chips render with per-tag counts + an "All" chip
        assert 'class="portfolio-filter"' in r.text
        assert 'data-filter=""' in r.text and ">All" in r.text  # 'All' chip
        # alphabetical: Dishes (2) before Drinks (1)
        assert r.text.index('data-filter="dishes"') < r.text.index('data-filter="drinks"')
        # per-tag counts visible
        assert ">Dishes" in r.text and "(2)" in r.text
        assert ">Drinks" in r.text and "(1)" in r.text
        # tiles carry lowercased tag attrs
        assert f'data-tag="dishes"' in r.text
        assert f'data-tag="drinks"' in r.text
        # filter chip data-filter is lowercased to match
        assert 'data-filter="dishes"' in r.text
        assert 'data-filter="drinks"' in r.text

    # clearing a tag (empty string) → DB stores NULL, chip count drops
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[1]}/tag",
               data={"portfolio_tag": ""}, follow_redirects=False)
    assert db.one("SELECT portfolio_tag FROM assets WHERE id=?",
                  (ids[1],))["portfolio_tag"] is None
    with TestClient(app) as pub:
        r = pub.get("/portfolio")
        assert ">Dishes" in r.text and "(1)" in r.text   # was 2, now 1
        assert ">Drinks" in r.text and "(1)" in r.text

    # unstarring a photo removes it from the public count (and the grid)
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[0]}/portfolio",
               follow_redirects=False)
    with TestClient(app) as pub:
        r = pub.get("/portfolio")
        # Dishes tag now has nothing → chip gone (we only show tags actually in use)
        assert 'data-filter="dishes"' not in r.text
        assert 'data-filter="drinks"' in r.text  # Drinks still has the lone tagged photo


def test_proofing_mode(admin):
    # own gallery so the proofing section starts clean — uploads elsewhere now
    # default into the first section, which would pollute a shared gallery's counts
    admin.post("/admin/galleries", data={"title": "Proofing", "client_name": ""},
               follow_redirects=False)
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": "Proofing", "pin": "7777", "published": "true"})
    g = db.one("SELECT * FROM galleries WHERE id=?", (g["id"],))
    sec = db.one("SELECT id FROM sections WHERE gallery_id=? ORDER BY position LIMIT 1",
                 (g["id"],))
    # park 3 assets in this section
    for i in range(3):
        db.run("INSERT INTO assets (gallery_id, section_id, kind, filename, "
               "stored, status) VALUES (?,?,?,?,?,?)",
               (g["id"], sec["id"], "photo", f"d{i}.jpg",
                f"deadbeef0{i}deadbeef.jpg", "ready"))
    assets = db.all_("SELECT id FROM assets WHERE gallery_id=? AND section_id=? "
                     "ORDER BY id", (g["id"], sec["id"]))

    # admin sets proof_target=2
    admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
               data={"proof_target": "2"}, follow_redirects=False)
    assert db.one("SELECT proof_target FROM sections WHERE id=?",
                  (sec["id"],))["proof_target"] == 2
    # admin gallery page reflects target + the live picks count badge
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert 'name="proof_target"' in r.text and 'value="2"' in r.text
    assert "0 / 2 picked" in r.text

    # public visitor unlocks the gallery and starts picking
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]},
                 follow_redirects=False)
        # gallery page renders the per-visitor progress label
        r = pub.get(f"/g/{g['slug']}")
        assert 'id="proof-' + str(sec["id"]) + '"' in r.text
        assert "0 of 2 picked" in r.text
        # tile carries data-section so the lightbox can locate the live label
        assert f'data-section="{sec["id"]}"' in r.text

        # pick 1 → 200 + heart updated + OOB progress jumps to 1 of 2
        r = pub.post(f"/g/{g['slug']}/fav/{assets[0]['id']}")
        assert r.status_code == 200
        assert "fav-btn faved" in r.text
        assert f'id="proof-{sec["id"]}"' in r.text and "1 of 2 picked" in r.text

        # pick 2 → at target, label flips to ok class
        r = pub.post(f"/g/{g['slug']}/fav/{assets[1]['id']}")
        assert r.status_code == 200
        assert "2 of 2 picked" in r.text and "proof-progress ok" in r.text

        # 3rd pick → REFUSED with 409 + HX-Trigger toast event; not stored
        r = pub.post(f"/g/{g['slug']}/fav/{assets[2]['id']}")
        assert r.status_code == 409
        assert "proof-cap" in r.headers["hx-trigger"]
        assert '"target":2' in r.headers["hx-trigger"]
        # 3rd asset stays unfaved
        assert db.one("SELECT COUNT(*) AS n FROM favorites f "
                      "JOIN assets a ON a.id=f.asset_id "
                      "WHERE a.id=?", (assets[2]["id"],))["n"] == 0

        # unfav one → progress drops to 1 of 2; can now pick the 3rd
        r = pub.post(f"/g/{g['slug']}/fav/{assets[0]['id']}")
        assert r.status_code == 200 and "1 of 2 picked" in r.text
        r = pub.post(f"/g/{g['slug']}/fav/{assets[2]['id']}")
        assert r.status_code == 200 and "2 of 2 picked" in r.text

    # admin badge flips to "ready" once the target is hit
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert "2 / 2 ready" in r.text

    # clearing the target unblocks unlimited faves and removes the label
    admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
               data={"proof_target": ""}, follow_redirects=False)
    assert db.one("SELECT proof_target FROM sections WHERE id=?",
                  (sec["id"],))["proof_target"] is None
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]},
                 follow_redirects=False)
        # now we can fav the leftover asset[0] without trouble — target is gone
        r = pub.post(f"/g/{g['slug']}/fav/{assets[0]['id']}")
        assert r.status_code == 200
        # the response has no OOB progress fragment (no proof_target)
        assert 'id="proof-' not in r.text
        # public gallery page also drops the badge
        r = pub.get(f"/g/{g['slug']}")
        assert 'id="proof-' + str(sec["id"]) + '"' not in r.text

    # bad input: non-numeric target → 400
    assert admin.post(f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
                      data={"proof_target": "twelve"},
                      follow_redirects=False).status_code == 400


def test_today_consolidated_view(admin):
    # baseline: page renders with friendly empty-states even when nothing
    # has happened in the last 24h
    page = admin.get("/admin/today").text
    assert "Today" in page and "last 24h" in page
    # nav cross-link from dashboard
    assert 'href="/admin/today"' in admin.get("/admin").text

    # seed one of each kind of activity (relative to "now" via SQLite default)
    iid = db.run("INSERT INTO inquiries (name, email, business, message, kind, "
                 "service, shoot_date) VALUES (?,?,?,?,?,?,?)",
                 ("Today Tester", "today@cafe.com", "Bistro Today",
                  "test booking", "booking", "Photography", "2026-06-20"))
    g = db.one("SELECT id FROM galleries ORDER BY id LIMIT 1")
    db.run("INSERT INTO downloads (gallery_id, asset_id) VALUES (?,?)",
           (g["id"], None))   # full-zip download (asset NULL)
    # a single visitor + fav seeded for the favorites-by-gallery roll-up
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
                 (g["id"], "tok-today"))
    a = db.one("SELECT id FROM assets WHERE gallery_id=? LIMIT 1", (g["id"],))
    db.run("INSERT OR IGNORE INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, a["id"]))
    db.run("INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, "
           "subject) VALUES (NULL, 'other', NULL, ?, ?)",
           ("today-recipient@cafe.com", "Hi from /admin/today test"))

    page = admin.get("/admin/today").text
    # summary line carries the totals; quiet-day state is gated out
    assert "<b>1</b>" in page  # at least one of each counted
    assert "Quiet day" not in page
    assert "today@cafe.com" in page
    assert "today-recipient@cafe.com" in page
    assert "Hi from /admin/today test" in page
    # inquiry shows the booking-kind icon + service + date
    assert "Photography" in page and "2026-06-20" in page
    # gallery link in downloads section
    assert f'href="/admin/galleries/{g["id"]}/activity"' in page
    # favorites roll-up shows the gallery + heart (count depends on prior
    # fixture state, just verify the section renders this gallery with a heart)
    fav_section = page[page.index("Favorites (by gallery)"):]
    assert "&hearts;" in fav_section
    assert f'href="/admin/galleries/{g["id"]}"' in fav_section
    # sent email gets its kind badge
    assert 'class="kind-badge kind-other"' in page

    # cleanup
    db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_sent_emails_log(admin):
    # baseline: page renders even with no rows
    r = admin.get("/admin/sent")
    assert r.status_code == 200
    assert "No emails sent yet" in r.text or "total send" in r.text  # one or the other

    # seed a project + client for context, then plant a handful of emails_log rows
    cid = db.run("INSERT INTO clients (name, company, email) VALUES (?,?,?)",
                 ("Sent-Log Co", "Bistro Lune", "sl@cafe.com"))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)",
                 (cid, "Spring shoot"))
    for kind, subj in [("proposal", "Your proposal — Spring shoot"),
                       ("contract", "Sign here — Spring shoot"),
                       ("invoice", "Invoice #001 — Spring shoot"),
                       ("other", "Your photos are ready — Spring shoot")]:
        db.run("INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject) "
               "VALUES (?,?,?,?,?)", (pid, kind, 1, "sl@cafe.com", subj))

    # the same rows surface on the project detail page's Email activity section,
    # filtered to this project, with the same kind badges + cross-link
    proj_page = admin.get(f"/admin/studio/projects/{pid}").text
    assert "Email activity" in proj_page
    assert "Your proposal — Spring shoot" in proj_page
    assert "Sign here — Spring shoot" in proj_page
    assert 'class="kind-badge kind-proposal"' in proj_page
    assert 'class="kind-badge kind-contract"' in proj_page
    assert 'class="kind-badge kind-invoice"' in proj_page
    assert 'class="kind-badge kind-other"' in proj_page
    assert 'href="/admin/sent"' in proj_page
    assert "4 sends" in proj_page

    r = admin.get("/admin/sent")
    assert "Your proposal — Spring shoot" in r.text
    assert "Sign here — Spring shoot" in r.text
    assert "Invoice #001 — Spring shoot" in r.text
    # kind badges render distinct classes per kind
    assert 'class="kind-badge kind-proposal"' in r.text
    assert 'class="kind-badge kind-contract"' in r.text
    assert 'class="kind-badge kind-invoice"' in r.text
    assert 'class="kind-badge kind-other"' in r.text
    # project link surfaces; client context shows alongside
    assert f'href="/admin/studio/projects/{pid}"' in r.text
    assert "Spring shoot" in r.text and "Sent-Log Co" in r.text

    # nav cross-links between captured + sent
    assert 'href="/admin/sent"' in admin.get("/admin/emails").text
    # the redesigned home (strict-1:1 prototype) dropped the old utility nav
    # strip from its top bar; Sent is now reached from home via the ⌘K command
    # palette (a JS-built CMDS entry), so assert that route is present there.
    assert '"/admin/sent"' in admin.get("/admin").text

    # pagination: 60 more rows → first page is the latest 50, "Older" link shows
    for i in range(60):
        db.run("INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, "
               "subject) VALUES (?,?,?,?,?)",
               (pid, "other", i, "sl@cafe.com", f"Page subject {i}"))
    r = admin.get("/admin/sent")
    assert "Older" in r.text
    assert "Page subject 59" in r.text  # latest sub on page 1
    assert "Page subject 0" not in r.text  # oldest pushed to page 2

    r = admin.get("/admin/sent?offset=50")
    assert "Newer" in r.text
    assert "Page subject 0" in r.text

    # email_doc endpoint already records to emails_log when called legit;
    # the view picks up that activity without any extra plumbing — verified by
    # spot-checking that one of our seeded rows survives a sort by created_at
    assert r.status_code == 200


def test_inquiry_form_rate_limit(monkeypatch):
    from app import config, mailer, security
    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, reply_to="", ics=None: None)
    # Wipe any prior pin_attempts so this test is isolated
    db.run("DELETE FROM pin_attempts WHERE gallery_id IN (?,?)",
           (security.INQUIRY_BUCKET_CONTACT, security.INQUIRY_BUCKET_BOOK))

    import datetime as dt
    contact_data = lambda i: {"name": f"User{i}", "email": f"u{i}@cafe.com",
                              "business": "Cafe", "message": "hello"}

    with TestClient(app) as pub:
        # /contact: 3 succeed, 4th is throttled with 429
        for i in range(3):
            r = pub.post("/contact", data=contact_data(i))
            assert r.status_code == 200 and "Thanks" in r.text, i
        r = pub.post("/contact", data=contact_data(99))
        assert r.status_code == 429
        assert "chance to reply" in r.text
        # the throttled submit must not store another inquiry row
        assert db.one("SELECT COUNT(*) AS n FROM inquiries "
                      "WHERE email=?", ("u99@cafe.com",))["n"] == 0

        # /book (the scheduler) has its OWN throttle bucket — bookings still go
        # through even though /contact was throttled. Seed a bookable event +
        # a day of open slots (idempotent: the suite shares one module DB).
        from app import scheduling as S
        if not S.event_by_slug("rl-book"):
            _eid = db.run("INSERT INTO event_types (slug, name, duration_min, "
                          "min_notice_hours, booking_window_days, active) "
                          "VALUES ('rl-book','Rate Test',60,1,60,1)")
            for _wd in range(5):
                db.run("INSERT INTO availability_rules (event_type_id, weekday, "
                       "start_min, end_min) VALUES (?,?,?,?)", (_eid, _wd, 540, 1020))
        _et = S.event_by_slug("rl-book")
        _day = dt.date.today() + dt.timedelta(days=3)
        while _day.weekday() >= 5:
            _day += dt.timedelta(days=1)
        _slots = S.slots_for_day(_et, _day)
        assert len(_slots) >= 4, "need >=4 open slots to prove the booking throttle"
        # first 3 bookings (distinct slots) succeed; the 4th trips the BOOK bucket
        for i in range(3):
            r = pub.post("/book/rl-book", data={
                "name": f"User{i}", "email": f"u{i}@cafe.com",
                "start": _slots[i]["utc"], "tz": "America/New_York"},
                follow_redirects=False)
            assert r.status_code == 303, (i, r.status_code)
        r = pub.post("/book/rl-book", data={
            "name": "User99", "email": "u99@cafe.com",
            "start": _slots[3]["utc"], "tz": "America/New_York"})
        assert r.status_code == 429
        assert "booked a few times" in r.text

        # honeypot still wins silently — doesn't decrement counter, doesn't 429
        # (we wipe and start fresh to confirm honeypot bypasses the throttle path)
        db.run("DELETE FROM pin_attempts WHERE gallery_id IN (?,?)",
               (security.INQUIRY_BUCKET_CONTACT, security.INQUIRY_BUCKET_BOOK))
        for _ in range(5):
            r = pub.post("/contact", data={**contact_data(1), "website": "bot.com"})
            assert r.status_code == 200 and "Thanks" in r.text

        # Validation failure (bad email) does NOT consume a token — only
        # successful sends record the attempt. Otherwise a single typo could
        # lock you out before any inquiry lands.
        db.run("DELETE FROM pin_attempts WHERE gallery_id=?",
               (security.INQUIRY_BUCKET_CONTACT,))
        for _ in range(10):
            r = pub.post("/contact", data={"name": "Bad", "email": "not-email",
                                           "message": "x"})
            assert r.status_code == 400
        # Now 3 legit succeed — the typos never burned the budget
        for i in range(3):
            r = pub.post("/contact", data=contact_data(100 + i))
            assert r.status_code == 200, i

    # Tear down the rl-book bookings + auto-linked clients/inquiries so the
    # leftover confirmed slots don't collide with downstream studio/conflict
    # tests that share this module DB. FK order: drop the bookings (which point
    # at clients + inquiries) before the rows they reference.
    _cids = [r["client_id"] for r in db.all_(
        "SELECT DISTINCT client_id FROM bookings WHERE event_type_id=? "
        "AND client_id IS NOT NULL", (_et["id"],))]
    _iids = [r["inquiry_id"] for r in db.all_(
        "SELECT inquiry_id FROM bookings WHERE event_type_id=? "
        "AND inquiry_id IS NOT NULL", (_et["id"],))]
    db.run("DELETE FROM bookings WHERE event_type_id=?", (_et["id"],))
    for _iid in _iids:
        db.run("DELETE FROM inquiries WHERE id=?", (_iid,))
    for _cid in _cids:
        db.run("DELETE FROM clients WHERE id=?", (_cid,))


def test_lightbox_doubletap_gesture():
    # Smoke-check that the lightbox.js the server actually serves carries the
    # double-tap-to-fav wiring + the shared triggerFav helper. We can't run
    # touch events in a TestClient, so verify the gesture hooks are present
    # in the served JS — that's the contract this ship cares about.
    with TestClient(app) as pub:
        js = pub.get("/static/lightbox.js").text
    assert "triggerFav" in js
    # the gesture handler tracks 2D motion + time, looks for a second tap
    # within 350ms on the .lb-stage, and gates by .dataset.fav so marketing
    # tiles (no fav target) silently no-op.
    assert "lastTap" in js
    assert "lb-stage" in js
    assert "350" in js
    # the fav helper is wired to the .lb-fav click too — same path either way
    assert "favBtn.addEventListener(\"click\", triggerFav)" in js
    # touch-action: manipulation on .lb-stage to block iOS double-tap-zoom
    css = pub.get("/static/mise.css").text
    assert "touch-action: manipulation" in css


def test_studio_portal_hint(admin):
    # The per-client portal-engagement hint ("👁 2h ago" / "never visited" / "no
    # portal") moved with the clients table to the Studio clients sub-view
    # (/admin/studio/clients) in the board-first strict-1:1 rewrite; the board
    # tab itself is now pipeline-only.
    import datetime as dt

    # client with no portal → "no portal"
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Portal Hint Cafe",))
    r = admin.get("/admin/studio/clients")
    assert r.status_code == 200
    # slice the clients table row so we only check this client's hint
    row_start = r.text.index("Portal Hint Cafe")
    row = r.text[row_start:r.text.index("</tr>", row_start)]
    assert "no portal" in row

    # add an unpublished portal (visits=0) → "never visited"
    db.run("INSERT INTO portals (client_id, slug, pin) VALUES (?,?,?)",
           (cid, "portal-hint-aaaa", "1234"))
    row = (lambda t: t[t.index("Portal Hint Cafe"):
                       t.index("</tr>", t.index("Portal Hint Cafe"))])(admin.get("/admin/studio/clients").text)
    assert "never visited" in row

    # set last_visit to 2 hours ago → "👁 2h ago"
    two_hours = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=2)).isoformat()
    db.run("UPDATE portals SET visits=5, last_visit=? WHERE client_id=?",
           (two_hours, cid))
    row = (lambda t: t[t.index("Portal Hint Cafe"):
                       t.index("</tr>", t.index("Portal Hint Cafe"))])(admin.get("/admin/studio/clients").text)
    assert "👁" in row and "2h ago" in row

    # 5 minutes ago → "Xm ago"
    five_min = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=5)).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (five_min, cid))
    row = (lambda t: t[t.index("Portal Hint Cafe"):
                       t.index("</tr>", t.index("Portal Hint Cafe"))])(admin.get("/admin/studio/clients").text)
    assert "5m ago" in row or "4m ago" in row  # tolerant to second-edge

    # 3 days ago → "3d ago"
    three_days = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=3)).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (three_days, cid))
    row = (lambda t: t[t.index("Portal Hint Cafe"):
                       t.index("</tr>", t.index("Portal Hint Cafe"))])(admin.get("/admin/studio/clients").text)
    assert "3d ago" in row

    # 45 days ago → falls back to ISO date
    long_ago = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45)).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (long_ago, cid))
    row = (lambda t: t[t.index("Portal Hint Cafe"):
                       t.index("</tr>", t.index("Portal Hint Cafe"))])(admin.get("/admin/studio/clients").text)
    # ISO date (YYYY-MM-DD) appears in the hint
    iso = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45)).date().isoformat()
    assert iso in row


def test_dashboard_proofing_status(admin):
    # The strict-1:1 grid card carries a single derived status badge. A published
    # gallery whose targeted proofing sections are still short of their pick count
    # reads "Proofing"; once every targeted section hits target it flips to
    # "Delivered". This replaces the old "✓ selects in" badge — same signal,
    # rendered in the prototype's status-pill shape.
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) "
                 "VALUES (?,?,?,1)", ("SelectsBadge001", "Loose pickin", "1234"))

    def card():
        # anchor on the grid card (last href — the orphan picker may list it
        # earlier) and read to the card's closing </a>
        page = admin.get("/admin/galleries").text
        start = page.rindex(f"/admin/galleries/{gid}")
        return page[start:page.index("</a>", start)]

    # no proofing sections at all → nothing pending → Delivered
    assert ">Delivered<" in card()

    # add a targeted section with assets but no picks → Proofing
    sid = db.run("INSERT INTO sections (gallery_id, name, position, proof_target) "
                 "VALUES (?,?,?,?)", (gid, "Hero", 0, 2))
    aids = [db.run("INSERT INTO assets (gallery_id, section_id, kind, filename, "
                   "stored, status) VALUES (?,?,?,?,?,?)",
                   (gid, sid, "photo", f"p{i}.jpg",
                    f"selbadge0{i}.jpg", "ready")) for i in range(3)]
    assert ">Proofing<" in card()

    # one fav of two needed → still short → Proofing
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
                 (gid, "vtok-badge"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, aids[0]))
    assert ">Proofing<" in card()

    # hit the target → proofing complete → Delivered
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, aids[1]))
    assert ">Delivered<" in card()

    # a SECOND targeted section that's still empty → partially done → Proofing
    sid2 = db.run("INSERT INTO sections (gallery_id, name, position, proof_target) "
                  "VALUES (?,?,?,?)", (gid, "Drinks", 1, 2))
    db.run("INSERT INTO assets (gallery_id, section_id, kind, filename, stored, "
           "status) VALUES (?,?,?,?,?,?)",
           (gid, sid2, "photo", "d.jpg", "selbadge99.jpg", "ready"))
    assert ">Proofing<" in card()

    # zero-target sections don't count (proof_target=0 is the "off" sentinel) →
    # only the first (complete) section remains → Delivered
    db.run("UPDATE sections SET proof_target=0 WHERE id=?", (sid2,))
    assert ">Delivered<" in card()


def _spark_rect_count(html: str) -> int:
    """Count sparkline bars only — nav SVG icons also use <rect>."""
    start = html.index('class="sparklines"')
    end = html.index("</section>", start)
    return html[start:end].count("<rect")


def test_studio_sparklines(admin):
    # baseline: studio loads with 3 sparkline cards regardless of state
    page = admin.get("/admin/studio/activity").text
    assert "Inquiries" in page and "Downloads" in page and "Favorites" in page
    assert page.count('class="spark-card"') == 3
    assert page.count("spark-svg") == 3
    # default = 7-day window → 7 x 3 = 21 <rect> bars inside sparklines only
    # (nav SVG icons also contain <rect> — scope to sparklines section)
    assert _spark_rect_count(page) == 21
    # 3-button window picker, 7d active by default
    assert 'href="/admin/studio/activity?days=7"' in page
    assert 'href="/admin/studio/activity?days=30"' in page
    assert 'href="/admin/studio/activity?days=90"' in page
    assert page.count("spark-window-active") == 1
    # active class is on the 7d button
    seven_idx = page.index('href="/admin/studio/activity?days=7"')
    line_end = page.index("</a>", seven_idx)
    assert "spark-window-active" in page[seven_idx:line_end]

    # ?days=30 → 30 x 3 = 90 bars + the 30d button becomes active
    page30 = admin.get("/admin/studio/activity?days=30").text
    assert _spark_rect_count(page30) == 90
    thirty_idx = page30.index('href="/admin/studio/activity?days=30"')
    assert "spark-window-active" in page30[thirty_idx:page30.index("</a>", thirty_idx)]

    # ?days=90 → 90 x 3 = 270 bars
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=90").text) == 270

    # bogus values clamp to the nearest allowed bucket: 999 → 90, 2 → 7, "abc" → 7
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=999").text) == 270
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=2").text) == 21
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=abc").text) == 21
    # 15 sits closer to 7 than 30 → clamps to 7
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=15").text) == 21
    # 22 sits closer to 30 than 7 → clamps to 30
    assert _spark_rect_count(admin.get("/admin/studio/activity?days=22").text) == 90

    # seed a fresh inquiry → today's bar grows to >= 1
    db.run("INSERT INTO inquiries (name, email, message) VALUES (?,?,?)",
           ("Spark Tester", "spark@cafe.com", "test message"))
    page = admin.get("/admin/studio/activity").text
    import re
    m = re.search(r'<strong>(\d+)</strong> Inquiries', page)
    assert m and int(m.group(1)) >= 1


def test_spark_series_buckets_on_local_evening_boundary(admin):
    # Regression for the evening-EDT undercount. A row created late local-evening
    # is stored as the NEXT day in UTC; the sparkline must still bucket it on the
    # LOCAL calendar day so it lands inside the local-built window. Pre-fix the
    # window came from Python-local `today` but rows bucketed by UTC date('now'),
    # so after ~8 PM EDT a fresh row fell onto tomorrow's UTC bar and vanished.
    # Deterministic by construction (fixed anchor + engineered timestamp) and
    # green under BOTH TZ=UTC and TZ=America/New_York — it would only fail on the
    # old UTC-bucketing code, and only under a non-UTC TZ. Uses a before/after
    # delta because the module-scoped DB carries other tests' inquiries.
    import datetime as _dt
    from app.admin.studio import _spark_series
    anchor = _dt.date(2026, 6, 13)
    # 23:30 on the anchor's LOCAL day -> a different UTC date in any TZ behind UTC,
    # while date(created_at,'localtime') stays the anchor day.
    local_dt = _dt.datetime.combine(anchor, _dt.time(23, 30))
    created_utc = local_dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    before, _ = _spark_series("inquiries", anchor, 7)
    db.run("INSERT INTO inquiries (name, email, message, created_at) VALUES (?,?,?,?)",
           ("Evening Boundary", "evening@cafe.com", "late local", created_utc))
    after, _ = _spark_series("inquiries", anchor, 7)
    # the evening row lands on the anchor's (local) bar — the last bucket
    assert after[-1] == before[-1] + 1


def test_invoice_overdue_judged_on_local_wall_clock(admin, monkeypatch):
    # Financial-boundary regression. An invoice is overdue only when its due_date
    # is past on the OPERATOR'S WALL CLOCK (localtime, the canonical studio clock),
    # never UTC — judging on UTC declares an invoice overdue hours early in the
    # evening EDT once UTC rolls past midnight, a wrong statement about a client.
    # Deterministic: the studio clock _today() is frozen to a fixed anchor (no
    # SQLite date('now'), so no dependence on run time or TZ). An invoice due ON
    # the anchor is still due-today => NOT overdue; due the day BEFORE => overdue.
    import datetime as _dt
    from app.admin import studio
    anchor = _dt.date(2026, 6, 13)
    monkeypatch.setattr(studio, "_today", lambda: anchor)

    admin.post("/admin/studio/clients",
               data={"name": "Overdue Chef", "company": "Wall Clock Bistro"},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    pid = db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
                 (c["id"], "Overdue Boundary Project", "contract_signed"))
    iid = db.run("""INSERT INTO invoices (project_id, slug, title, total_cents,
                                          due_date, status)
                    VALUES (?,?,?,?,?,?)""",
                 (pid, "ovd-boundary-12345", "Boundary Invoice", 50000,
                  anchor.isoformat(), "sent"))

    def row(page):
        # Projects render as board <article> cards; scope the overdue check to
        # this project's card so a stray "overdue" elsewhere can't fool it. The
        # card surfaces an overdue invoice via its step pill ("N overdue").
        i = page.index("Overdue Boundary Project")
        return page[page.rindex("<article", 0, i):page.index("</article>", i)]

    # due ON the wall-clock anchor -> still due today -> NOT overdue
    # ("1 overdue" is the step-pill text; bare "overdue" also lives in data-search)
    assert "1 overdue" not in row(admin.get("/admin/studio").text)

    # due the day BEFORE the anchor -> genuinely past -> overdue
    db.run("UPDATE invoices SET due_date=? WHERE id=?",
           ((anchor - _dt.timedelta(days=1)).isoformat(), iid))
    assert "1 overdue" in row(admin.get("/admin/studio").text)


def test_studio_proofing_waiting(admin):
    # baseline: nothing in the proofing-waiting strip → section hidden
    r = admin.get("/admin/studio/activity")
    assert "Proofing waiting" not in r.text

    # set up: client → project → published gallery linked to project → proofing
    # section with 3 ready assets but only 1 fav (target 3, picks 1 → 2 remaining)
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Mara Sun",))
    pid = db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
                 (cid, "Spring shoot — proofing", "session_planning"))
    gid = db.run("INSERT INTO galleries (slug, title, pin, project_id, published) "
                 "VALUES (?,?,?,?,1)",
                 ("ProofWaiting001", "Spring shoot", "1234", pid))
    sid = db.run("INSERT INTO sections (gallery_id, name, position, proof_target) "
                 "VALUES (?,?,?,?)", (gid, "Hero Dishes", 0, 3))
    asset_ids = []
    for i in range(3):
        asset_ids.append(db.run(
            "INSERT INTO assets (gallery_id, section_id, kind, filename, "
            "stored, status) VALUES (?,?,?,?,?,?)",
            (gid, sid, "photo", f"d{i}.jpg",
             f"deadbeef0{i}deadbeef.jpg", "ready")))
    # one visitor faved one photo → 1 of 3 picked, 2 remaining
    vid = db.run("INSERT INTO visitors (gallery_id, token, email) "
                 "VALUES (?,?,?)", (gid, "vtoken-proof-1", "mara@cafe.com"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, asset_ids[0]))

    # the all-projects table below renders every project too — slice the
    # proofing-waiting strip specifically so assertions only watch the chip
    def waiting_strip(text):
        if 'aria-label="Proofing waiting"' not in text:
            return ""
        start = text.index('aria-label="Proofing waiting"')
        return text[start:text.index("</section>", start)]

    r = admin.get("/admin/studio/activity")
    assert "Proofing waiting" in r.text
    strip = waiting_strip(r.text)
    assert "Spring shoot — proofing" in strip
    assert "1 chapter" in strip and "2 picks remaining" in strip
    # chip links to the gallery admin (where the Proofing prompt email lives)
    assert f'href="/admin/galleries/{gid}"' in strip

    # client picks the remaining two → section satisfied → project drops off
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, asset_ids[1]))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)",
           (vid, asset_ids[2]))
    assert "Spring shoot — proofing" not in waiting_strip(admin.get("/admin/studio/activity").text)

    # archived project → never surfaces even with unfilled proofing
    db.run("DELETE FROM favorites WHERE visitor_id=?", (vid,))
    assert "Spring shoot — proofing" in waiting_strip(admin.get("/admin/studio/activity").text)
    db.run("UPDATE projects SET status='archived' WHERE id=?", (pid,))
    assert "Spring shoot — proofing" not in waiting_strip(admin.get("/admin/studio/activity").text)

    # unpublished gallery → no nudge (client can't see it yet, nothing to proof)
    db.run("UPDATE projects SET status='session_planning' WHERE id=?", (pid,))
    db.run("UPDATE galleries SET published=0 WHERE id=?", (gid,))
    assert "Spring shoot — proofing" not in waiting_strip(admin.get("/admin/studio/activity").text)


def test_studio_upcoming_strip(admin):
    import datetime as dt
    today = dt.date.today()

    # baseline: empty strip → muted "Nothing on the calendar" copy
    r = admin.get("/admin/studio/activity")
    assert r.status_code == 200
    assert "Upcoming shoots" in r.text and "Nothing on the calendar" in r.text

    cid = db.run("INSERT INTO clients (name, company, email) VALUES (?,?,?)",
                 ("Mara Sun", "Café Lune", "mara@cafe.com"))
    # 4 projects spanning the upcoming window + a control out of range
    plans = [
        ("Today launch",        today.isoformat(),                              "inquiry_received", "today",   True),
        ("Tomorrow shoot",      (today + dt.timedelta(days=1)).isoformat(),     "proposal_sent",  "tomorrow",  True),
        ("Next week shoot",     (today + dt.timedelta(days=8)).isoformat(),     "contract_signed","in 8d",     True),
        ("Overdue not shooting",(today - dt.timedelta(days=3)).isoformat(),     "proposal_sent",  "3d ago",    True),
        ("Way out — skip",      (today + dt.timedelta(days=30)).isoformat(),    "inquiry_received", "in 30d",  False),
        ("Long past — skip",    (today - dt.timedelta(days=30)).isoformat(),    "session_planning","30d ago",  False),
        ("No shoot date — skip", None,                                          "inquiry_received", "—",       False),
    ]
    for title, sdate, status, _label, _in_strip in plans:
        db.run("INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
               (cid, title, status, sdate))

    r = admin.get("/admin/studio/activity")
    # isolate the upcoming-shoots strip specifically — there's also a
    # proofing-waiting strip on the page if any gallery has unfilled proofing.
    sec_start = r.text.index('aria-label="Upcoming shoots"')
    strip_start = r.text.index('class="upcoming-strip"', sec_start)
    strip_end = r.text.index("</ul>", strip_start)
    strip = r.text[strip_start:strip_end]

    for title, _, _, label, in_strip in plans:
        if in_strip:
            assert title in strip, title
            assert label in strip, f"{title}: missing '{label}'"
        else:
            assert title not in strip, title

    # color classes applied appropriately
    assert "upcoming-today" in strip and "upcoming-overdue" in strip
    # overdue + still pre-shooting → "not yet shooting" warn
    assert "not yet shooting" in strip
    # chip links to project detail
    overdue = db.one("SELECT id FROM projects WHERE title='Overdue not shooting'")
    assert f'href="/admin/studio/projects/{overdue["id"]}"' in strip

    # archived projects don't surface even if in-window
    db.run("UPDATE projects SET status='archived' WHERE title='Today launch'")
    r = admin.get("/admin/studio/activity")
    sec_start = r.text.index('aria-label="Upcoming shoots"')
    strip = r.text[r.text.index('class="upcoming-strip"', sec_start):
                   r.text.index("</ul>", r.text.index('class="upcoming-strip"', sec_start))]
    assert "Today launch" not in strip


def test_studio_booking_conflicts(admin):
    import datetime as dt
    today = dt.date.today()

    # baseline: no shoot_date set on any project → conflicts strip absent
    db.run("UPDATE projects SET shoot_date=NULL")
    db.run("DELETE FROM inquiries")
    r = admin.get("/admin/studio/activity")
    assert r.status_code == 200
    assert "Booking conflicts" not in r.text

    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Mara Conflict",))
    d_collide = (today + dt.timedelta(days=5)).isoformat()
    d_solo = (today + dt.timedelta(days=6)).isoformat()
    d_far = (today + dt.timedelta(days=120)).isoformat()  # outside +90 window

    # two projects on the same upcoming date → conflict
    pid_a = db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
                   "VALUES (?,?,?,?)", (cid, "Salt Bar shoot", "contract_signed", d_collide))
    pid_b = db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
                   "VALUES (?,?,?,?)", (cid, "Curate breakfast", "inquiry_received", d_collide))
    # solo upcoming → no collision
    db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
           "VALUES (?,?,?,?)", (cid, "Solo gig", "inquiry_received", d_solo))
    # archived on a collision date → ignored
    db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
           "VALUES (?,?,?,?)", (cid, "Archived ghost", "archived", d_collide))
    # far out → outside window, ignored
    db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
           "VALUES (?,?,?,?)", (cid, "Far future", "inquiry_received", d_far))

    r = admin.get("/admin/studio/activity")
    sec_start = r.text.index('aria-label="Booking conflicts"')
    sec_end = r.text.index("</section>", sec_start)
    sec = r.text[sec_start:sec_end]

    assert "Salt Bar shoot" in sec and "Curate breakfast" in sec
    assert d_collide in sec
    assert "Solo gig" not in sec  # only one item on that date
    assert "Archived ghost" not in sec
    assert "Far future" not in sec
    assert f'href="/admin/studio/projects/{pid_a}"' in sec
    assert f'href="/admin/studio/projects/{pid_b}"' in sec
    assert "2 on one day" in sec

    # project + non-converted booking inquiry on the same date → also a conflict
    d_inq = (today + dt.timedelta(days=10)).isoformat()
    db.run("INSERT INTO projects (client_id, title, status, shoot_date) "
           "VALUES (?,?,?,?)", (cid, "Tasting menu", "proposal_sent", d_inq))
    db.run("INSERT INTO inquiries (name, email, message, kind, shoot_date, service) "
           "VALUES (?,?,?,?,?,?)",
           ("Drop-in chef", "chef@x.com", "Need photos", "booking", d_inq, "Photography"))
    # converted inquiry on the SAME date → ignored (already accounted for as a project elsewhere)
    db.run("INSERT INTO inquiries (name, email, message, kind, shoot_date, service, "
           "converted_at) VALUES (?,?,?,?,?,?, datetime('now'))",
           ("Already booked", "ab@x.com", "", "booking", d_inq, "Videography"))

    r = admin.get("/admin/studio/activity")
    sec_start = r.text.index('aria-label="Booking conflicts"')
    sec_end = r.text.index("</section>", sec_start)
    sec = r.text[sec_start:sec_end]
    assert "Tasting menu" in sec and "Drop-in chef" in sec
    assert "inquiry (not yet converted)" in sec
    assert "Already booked" not in sec


def test_client_lifetime_rollup(admin):
    # a brand-new client with no invoices and no delivered shoots → strip absent
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Rollup Cafe",))
    r = admin.get(f"/admin/studio/clients/{cid}")
    assert r.status_code == 200
    # rollup now lives in the .pgtop topbar subtitle; absent when there's no money/delivery
    assert "paid lifetime" not in r.text

    # one closed project, one archived (both count as delivered), one inquiry (doesn't)
    pid = db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
                 (cid, "Spring menu", "project_closed"))
    db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
           (cid, "Old gig", "archived"))
    db.run("INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
           (cid, "Pitch", "inquiry_received"))

    # a draft invoice (excluded from invoiced) + a sent invoice (counted)
    db.run("""INSERT INTO invoices (project_id, slug, title, total_cents, status)
              VALUES (?,?,?,?,?)""", (pid, "rollup-draft", "Draft", 99999, "draft"))
    iid = db.run("""INSERT INTO invoices (project_id, slug, title, total_cents, status)
                    VALUES (?,?,?,?,?)""", (pid, "rollup-sent", "Issued", 100000, "sent"))
    # a partial deposit payment — paid is the ground truth, leaving an outstanding balance
    db.run("""INSERT INTO payments (invoice_id, amount_cents, kind) VALUES (?,?,?)""",
           (iid, 40000, "deposit"))

    r = admin.get(f"/admin/studio/clients/{cid}")
    s_start = r.text.index('class="pgtop-sub"')
    sec = r.text[s_start:r.text.index("</p>", s_start)]
    assert "$400.00</b> paid lifetime" in sec
    assert "$1000.00 invoiced" in sec       # draft's $999.99 excluded
    assert "across 1 invoice" in sec        # draft not counted
    assert "$600.00 outstanding" in sec
    assert "<b>2</b> shoots delivered" in sec  # delivered + archived, not lead


def test_testimonials(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")

    with TestClient(app) as pub:
        # baseline: home, services, work-detail (when published) have NO testimonials block
        # — the partial returns nothing if the list is empty, not an empty header.
        for path in ("/", "/services"):
            r = pub.get(path)
            assert "testimonial-list" not in r.text, path
            assert '"@type": "Review"' not in r.text, path

        # admin: create a published general testimonial (no gallery) + a gallery-scoped one,
        # plus one unpublished general one that should never surface
        admin.post("/admin/studio/testimonials",
                   data={"quote": "They captured our menu better than we imagined.",
                         "attribution_name": "Mara Sun", "business": "Owner, Café Lune",
                         "gallery_id": "", "position": "0", "published": "true"},
                   follow_redirects=False)
        admin.post("/admin/studio/testimonials",
                   data={"quote": "Spring shoot felt effortless.",
                         "attribution_name": "Lou Mendez", "business": "Bistro Vert",
                         "gallery_id": str(g["id"]), "position": "0",
                         "published": "true"},
                   follow_redirects=False)
        admin.post("/admin/studio/testimonials",
                   data={"quote": "Draft only — should not show.",
                         "attribution_name": "Sam Draft",
                         "gallery_id": "", "position": "0"},  # no published flag
                   follow_redirects=False)

        # home + services now show the general one (and only the general one)
        for path in ("/", "/services"):
            r = pub.get(path)
            assert "captured our menu better" in r.text, path
            assert "Mara Sun" in r.text and "Café Lune" in r.text
            assert "Spring shoot felt effortless" not in r.text, path
            assert "Draft only" not in r.text, path
            # Schema.org Review JSON-LD for SEO rich results
            assert '"@type": "Review"' in r.text
            assert '"@type": "Person"' in r.text

    # publish the case study + verify the gallery-scoped testimonial shows there
    admin.post(f"/admin/galleries/{g['id']}/assets/"
               f"{db.one('SELECT id FROM assets WHERE gallery_id=? AND kind=' + chr(34) + 'photo' + chr(34), (g['id'],))['id']}"
               f"/portfolio", follow_redirects=False)
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": g["title"], "client_name": g["client_name"] or "",
                     "pin": g["pin"], "expires_at": "", "published": "true",
                     "captions": "", "cs_published": "true",
                     "cs_tagline": "Test case study",
                     "cs_brief": "Brief.", "cs_credits": "", "cs_location": ""})
    with TestClient(app) as pub:
        r = pub.get(f"/work/{g['slug']}")
        assert "Spring shoot felt effortless" in r.text
        assert "Lou Mendez" in r.text
        # The general testimonial does NOT appear on the case-study page
        assert "captured our menu better" not in r.text
        # heading is gallery-scoped
        assert "From" in r.text  # "From {client or 'this project'}"

    # admin list + update + delete flow
    r = admin.get("/admin/studio/testimonials")
    assert r.status_code == 200
    assert "Mara Sun" in r.text and "Lou Mendez" in r.text and "Sam Draft" in r.text
    sam = db.one("SELECT id FROM testimonials WHERE attribution_name='Sam Draft'")
    admin.post(f"/admin/studio/testimonials/{sam['id']}",
               data={"quote": "Now published.", "attribution_name": "Sam Drafted",
                     "business": "", "gallery_id": "", "position": "0",
                     "published": "true"}, follow_redirects=False)
    row = db.one("SELECT * FROM testimonials WHERE id=?", (sam["id"],))
    assert row["published"] == 1 and row["attribution_name"] == "Sam Drafted"
    admin.post(f"/admin/studio/testimonials/{sam['id']}/delete",
               follow_redirects=False)
    assert db.one("SELECT id FROM testimonials WHERE id=?", (sam["id"],)) is None

    # deleting a gallery unbinds testimonials (FK ON DELETE SET NULL)
    # — first unpublish the gallery so delete_gallery accepts it
    admin.post(f"/admin/galleries/{g['id']}/settings",
               data={"title": g["title"], "client_name": g["client_name"] or "",
                     "pin": g["pin"], "expires_at": "", "published": "",
                     "captions": "", "cs_published": "", "cs_tagline": "",
                     "cs_brief": "", "cs_credits": "", "cs_location": ""})
    admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)
    lou = db.one("SELECT gallery_id FROM testimonials WHERE attribution_name='Lou Mendez'")
    assert lou is not None and lou["gallery_id"] is None


def test_services_page():
    from app.public.site import SERVICES

    with TestClient(app) as pub:
        r = pub.get("/services")
        assert r.status_code == 200
        # all three categories + their tiers render
        for s in SERVICES:
            assert s["title"] in r.text, s["key"]
            for t in s["tiers"]:
                # every tier name should appear at least 3 times (once per category)
                # but we just need each card present per service
                assert f">{t['name']}<" in r.text
        # tier count is 9 (3 categories × 3 tiers)
        assert r.text.count("svc-tier ") + r.text.count('svc-tier"') >= 9
        # middle tier flagged as "Most picked" (UX nudge), 3 times
        assert r.text.count("Most picked") == 3
        # Quoting-first: no flat prices on the page — tiers list inclusions only
        # and every quote is tailored on /contact. (Prices live in SERVICES for
        # the admin proposal presets, not the public page.)
        for s in SERVICES:
            for t in s["tiers"]:
                assert f'${t["price_cents"] // 100:,}' not in r.text, (s["key"], t["name"])
        # Quoting-first: tier + foot CTAs route to /contact ("Request a quote"),
        # not a flat-rate /book. Secondary "See past work" routes to /portfolio.
        assert r.text.count('href="/contact"') >= 4   # 9 tier CTAs + foot CTA + lede
        assert 'href="/portfolio"' in r.text
        # nav from any other site page links to /services
        assert 'href="/services"' in pub.get("/").text
        # SEO bits
        assert 'name="description"' in r.text and "Asheville" in r.text
        assert 'property="og:title"' in r.text


def test_faq_block():
    from app.public.site import FAQS

    with TestClient(app) as pub:
        # both /book and /contact carry the same accordion + JSON-LD schema
        for path in ("/book", "/contact"):
            r = pub.get(path)
            assert r.status_code == 200
            # every Q renders as one <details>; spot-check distinctive substrings
            # (Jinja escapes apostrophes in the rendered HTML so don't full-match)
            assert r.text.count('<details class="faq-item">') == len(FAQS), path
            assert "turnaround on edited images" in r.text, path
            assert "7&#8211;10 business days" in r.text or "7–10 business days" in r.text
            assert "food stylist" in r.text and "usage rights" in r.text
            # FAQPage structured data for Google rich results — apostrophes are
            # JSON-escaped (raw ' or literal), not HTML-escaped, inside <script>
            assert '"@type": "FAQPage"' in r.text
            assert '"@type": "Question"' in r.text
            assert '"acceptedAnswer"' in r.text
            # links to /contact (so visitors can escalate beyond the FAQ)
            assert 'href="/contact"' in r.text
        # other marketing pages don't carry the FAQ (different intent)
        assert '"@type": "FAQPage"' not in pub.get("/portfolio").text
        assert '"@type": "FAQPage"' not in pub.get("/").text


def test_license_lifecycle(admin):
    import json as _json

    # holder client
    admin.post("/admin/studio/clients",
               data={"name": "Licensing Chef", "company": "Moat Bistro",
                     "email": "moat@bistro.com"}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")

    # create a license → one 'create' audit row
    r = admin.post(f"/admin/studio/clients/{c['id']}/licenses",
                   data={"title": "Spring menu — social"}, follow_redirects=False)
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    assert lic["holder_client_id"] == c["id"]
    assert lic["coverage_scope"] == "holder_only"  # the schema default
    assert lic["status"] == "draft" and lic["published"] == 0
    created = db.all_("""SELECT * FROM audit_log WHERE entity_type='license'
                         AND entity_id=? AND action='create'""", (lic["id"],))
    assert len(created) == 1

    # detail page renders
    assert admin.get(f"/admin/studio/licenses/{lic['id']}").status_code == 200

    # update with a real change → 'update' audit row carries the diff
    r = admin.post(f"/admin/studio/licenses/{lic['id']}",
                   data={"title": "Spring menu — social + web",
                         "usage_tier": "extended", "exclusivity": "non_exclusive",
                         "coverage_scope": "holder_only", "fee": "1500.00",
                         "territory": ["US", "worldwide"],
                         "channels": ["website", "social_organic"],
                         "ends_on": "2099-01-01", "published": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert lic["fee_cents"] == 150000 and lic["usage_tier"] == "extended"
    assert lic["published"] == 1
    assert set(_json.loads(lic["territory"])) == {"US", "worldwide"}
    upd = db.one("""SELECT diff_json FROM audit_log WHERE entity_type='license'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
                 (lic["id"],))
    diff = _json.loads(upd["diff_json"])
    assert "usage_tier" in diff and diff["usage_tier"] == ["standard", "extended"]
    assert "fee_cents" in diff

    # a no-op update writes NO new audit row (append-only stays clean)
    before = db.one("""SELECT COUNT(*) AS n FROM audit_log
                       WHERE entity_type='license' AND entity_id=?""", (lic["id"],))["n"]
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "usage_tier": "extended",
                     "exclusivity": "non_exclusive", "coverage_scope": "holder_only",
                     "fee": "1500.00", "territory": ["US", "worldwide"],
                     "channels": ["website", "social_organic"],
                     "ends_on": "2099-01-01", "published": "1"},
               follow_redirects=False)
    after = db.one("""SELECT COUNT(*) AS n FROM audit_log
                      WHERE entity_type='license' AND entity_id=?""", (lic["id"],))["n"]
    assert after == before

    # status change → its own audit row + status persisted
    admin.post(f"/admin/studio/licenses/{lic['id']}/status",
               data={"status": "active"}, follow_redirects=False)
    assert db.one("SELECT status FROM licenses WHERE id=?", (lic["id"],))["status"] == "active"
    sc = db.one("""SELECT diff_json FROM audit_log WHERE entity_type='license'
                   AND entity_id=? AND action='status_change' ORDER BY id DESC LIMIT 1""",
                (lic["id"],))
    assert _json.loads(sc["diff_json"])["status"] == ["draft", "active"]

    # bad status rejected
    assert admin.post(f"/admin/studio/licenses/{lic['id']}/status",
                      data={"status": "bogus"}, follow_redirects=False).status_code == 400

    # active + dated within 45d surfaces on the dashboard + licenses strips
    db.run("UPDATE licenses SET ends_on=date('now','+10 days') WHERE id=?", (lic["id"],))
    assert lic["title"] in admin.get("/admin/studio/licenses").text
    assert "expiring" in admin.get("/admin/studio/activity").text.lower()

    # 'specific' coverage syncs the join table inside the same tx
    other = db.run("INSERT INTO clients (name) VALUES (?)", ("Sister Venue",))
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "usage_tier": "extended",
                     "exclusivity": "non_exclusive", "coverage_scope": "specific",
                     "fee": "1500.00", "cover_client_ids": [str(other)],
                     "published": "1"}, follow_redirects=False)
    covered = db.all_("SELECT client_id FROM license_clients WHERE license_id=?", (lic["id"],))
    assert [r["client_id"] for r in covered] == [other]

    # atomic soft-delete: deleted_at set, one soft_delete audit row, excluded
    # from the active list, but the audit trail survives (append-only)
    n_audit_before = db.one("""SELECT COUNT(*) AS n FROM audit_log
                               WHERE entity_type='license' AND entity_id=?""",
                            (lic["id"],))["n"]
    r = admin.post(f"/admin/studio/licenses/{lic['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT deleted_at FROM licenses WHERE id=?", (lic["id"],))["deleted_at"]
    assert admin.get(f"/admin/studio/licenses/{lic['id']}").status_code == 404
    assert lic["title"] not in admin.get("/admin/studio/licenses").text
    n_audit_after = db.one("""SELECT COUNT(*) AS n FROM audit_log
                              WHERE entity_type='license' AND entity_id=?""",
                           (lic["id"],))["n"]
    assert n_audit_after == n_audit_before + 1
    assert db.one("""SELECT action FROM audit_log WHERE entity_type='license'
                     AND entity_id=? ORDER BY id DESC LIMIT 1""",
                  (lic["id"],))["action"] == "soft_delete"


def test_license_holder_and_descendants_cascade(admin):
    """coverage_scope='holder_and_descendants' resolves through the Domain A
    client tree (clients.descendant_ids): a group-held license reaches every
    descendant venue, holder first. holder_only stays just the holder; 'specific'
    stays explicit-only (descendants are NOT auto-pulled). The detail page makes
    the resolved reach visible — proving the cascade, not just storing the flag."""
    import re
    from app.admin.licenses import effective_coverage

    def _cov_block(html):  # isolate the effective-coverage readout from the
        m = re.search(r'coverage-list">(.*?)</ul>', html, re.S)  # 'Also covers'
        return m.group(1) if m else ""                           # checkbox list

    group = db.run("INSERT INTO clients (name, company) VALUES (?,?)",
                   ("Hospitality Group", "BigCo"))
    region = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                    ("West Region", group))
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                   ("Harbor Venue", region))

    r = admin.post(f"/admin/studio/clients/{group}/licenses",
                   data={"title": "Group brand grant"}, follow_redirects=False)
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses WHERE holder_client_id=? ORDER BY id DESC LIMIT 1",
                 (group,))

    # schema default holder_only → only the holder is reached
    assert effective_coverage(db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))) == [group]
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "1 client reached" in page
    assert "Harbor Venue" not in _cov_block(page)  # descendant not yet reached

    # flip to holder_and_descendants → holder first, then descendants top-down
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "coverage_scope": "holder_and_descendants"},
               follow_redirects=False)
    row = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert effective_coverage(row) == [group, region, venue]
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "3 clients reached" in page
    block = _cov_block(page)
    assert "Harbor Venue" in block and "West Region" in block  # cascade is VISIBLE

    # 'specific' is explicit-only — the descendant venue is NOT auto-included
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "coverage_scope": "specific",
                     "cover_client_ids": [str(region)]},
               follow_redirects=False)
    row = db.one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
    assert effective_coverage(row) == [group, region]  # venue NOT pulled in
    assert "Harbor Venue" not in _cov_block(admin.get(
        f"/admin/studio/licenses/{lic['id']}").text)


def test_pricing_suggestion_math():
    """The Asheville rate card maps usage params -> a suggested licensing fee.
    Encodes WHY each driver matters: territory takes the MAX selected, channels
    add per-channel uplift (heavy > light), perpetual doubles, multi-year prorates,
    and the 'exclusive' tier already prices lockout so the exclusivity flag must
    NOT stack on it (else clients get double-charged)."""
    from app import pricing

    def lic(**kw):
        base = {"usage_tier": "standard", "territory": "[]", "channels": "[]",
                "exclusivity": "non_exclusive", "perpetual": 0,
                "starts_on": None, "ends_on": None}
        base.update(kw)
        return base

    assert pricing.suggest_license_fee(lic())["total_cents"] == 27500  # base only
    r = pricing.suggest_license_fee(lic(
        territory='["US"]',
        channels='["website","social_organic","social_paid"]'))
    assert r["territory_mult"] == 1.4
    assert r["channel_mult"] == 1.28      # website free + .08 light + .20 heavy
    assert r["total_cents"] == round(27500 * 1.4 * 1.28)
    # 'exclusive' tier: exclusivity flag does NOT stack (no double-count).
    et = pricing.suggest_license_fee(lic(usage_tier="exclusive", exclusivity="exclusive"))
    assert et["excl_mult"] == 1.0 and et["total_cents"] == 170000
    # exclusivity DOES multiply a non-exclusive tier.
    ex = pricing.suggest_license_fee(lic(usage_tier="extended", exclusivity="exclusive"))
    assert ex["excl_mult"] == 1.8 and ex["total_cents"] == round(60000 * 1.8)
    # perpetual doubles; territory is the MAX of those selected.
    pp = pricing.suggest_license_fee(lic(perpetual=1,
        territory='["local_metro","worldwide"]'))
    assert pp["term_mult"] == 2.0 and pp["territory_mult"] == 2.5
    assert pp["total_cents"] == round(27500 * 2.5 * 2.0)
    # ~17-month fixed term spans into year 2 -> +25%.
    y2 = pricing.suggest_license_fee(lic(starts_on="2026-01-01", ends_on="2027-06-01"))
    assert y2["term_mult"] == 1.25


def test_pricing_travel_market_rate_cards():
    """Charlotte and Raleigh are travel markets with their own base rate cards
    (Charlotte premium, Raleigh mid). Encodes WHY: only the per-tier base changes
    between markets — the usage multipliers are market-independent doctrine, so
    the same row priced in two markets differs ONLY by the base ratio. An unknown
    market falls back to Asheville rather than erroring (advisory, never blocking)."""
    from app import pricing

    def lic(**kw):
        base = {"usage_tier": "standard", "territory": "[]", "channels": "[]",
                "exclusivity": "non_exclusive", "perpetual": 0,
                "starts_on": None, "ends_on": None}
        base.update(kw)
        return base

    # Base-only suggestion picks the market's base card.
    assert pricing.suggest_license_fee(lic(), market="raleigh")["total_cents"] == 35000
    assert pricing.suggest_license_fee(lic(), market="charlotte")["total_cents"] == 42500
    # Multipliers are identical across markets — only the base scales.
    args = dict(territory='["worldwide"]', channels='["website","print"]')
    ash = pricing.suggest_license_fee(lic(**args), market="asheville")
    chr = pricing.suggest_license_fee(lic(**args), market="charlotte")
    assert ash["territory_mult"] == chr["territory_mult"] == 2.5
    assert ash["channel_mult"] == chr["channel_mult"] == 1.20
    assert chr["total_cents"] == round(42500 * 2.5 * 1.20)
    # The premium ratio is carried entirely by the base.
    assert chr["total_cents"] / ash["total_cents"] == 42500 / 27500
    # Unknown market -> Asheville fallback, but the breakdown reports what was asked.
    unk = pricing.suggest_license_fee(lic(), market="nashville")
    assert unk["base_cents"] == 27500 and unk["market"] == "nashville"


def test_license_suggestion_follows_client_market(admin):
    """The license detail page prices the suggestion in the HOLDER client's home
    market, not a hardcoded default. Encodes WHY: a Charlotte client's grant must
    quote the Charlotte rate card, so changing the client's market changes the
    suggested fee end-to-end (route -> pricing -> rendered page)."""
    cli = db.run("INSERT INTO clients (name) VALUES (?)", ("Charlotte Bistro",))
    # Move the client to Charlotte via the editor route (validates the vocab).
    r = admin.post(f"/admin/studio/clients/{cli}",
                   data={"name": "Charlotte Bistro", "market": "charlotte"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT market FROM clients WHERE id=?", (cli,))["market"] == "charlotte"
    admin.post(f"/admin/studio/clients/{cli}/licenses",
               data={"title": "Bistro web license"}, follow_redirects=False)
    lic = db.one("SELECT id FROM licenses WHERE holder_client_id=? "
                 "ORDER BY id DESC LIMIT 1", (cli,))
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "Suggested (charlotte rate card)" in page
    # An unknown market is rejected at the editor, not silently stored.
    bad = admin.post(f"/admin/studio/clients/{cli}",
                     data={"name": "Charlotte Bistro", "market": "atlantis"},
                     follow_redirects=False)
    assert bad.status_code == 400
    assert db.one("SELECT market FROM clients WHERE id=?", (cli,))["market"] == "charlotte"


def test_license_suggested_fee_is_advisory(admin):
    """The suggested fee is DISPLAY ONLY. It renders on the detail page, but the
    human-typed fee_cents is the source of truth — saving never lets the pricing
    engine overwrite what Kevin chose to charge (governance: AI suggests prices,
    it does not set them)."""
    cli = db.run("INSERT INTO clients (name) VALUES (?)", ("Asheville Cafe",))
    r = admin.post(f"/admin/studio/clients/{cli}/licenses",
                   data={"title": "Cafe web license"}, follow_redirects=False)
    assert r.status_code == 303
    lic = db.one("SELECT * FROM licenses WHERE holder_client_id=? "
                 "ORDER BY id DESC LIMIT 1", (cli,))
    page = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert "Suggested (asheville rate card)" in page
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "usage_tier": "standard", "fee": "123.45"},
               follow_redirects=False)
    row = db.one("SELECT fee_cents FROM licenses WHERE id=?", (lic["id"],))
    assert row["fee_cents"] == 12345  # untouched by the suggestion engine


def test_license_reverse_lookup_on_covered_client(admin):
    """The bottom-up inverse of the cascade: a venue's OWN page surfaces licenses
    it is reached by without holding. A holder_and_descendants grant on an
    ancestor shows as 'group cascade'; an explicit 'specific' grant held elsewhere
    that lists the venue shows as 'added explicitly'; a holder_only grant reaches
    nobody below, so it must NOT appear. Coverage must be visible from the covered
    side, not only the holder side (R14/R21) — otherwise a venue can't see what it
    may use without hunting every ancestor."""
    import re

    def _covered(html):  # isolate the 'Also covered by' table from the rest of
        m = re.search(r'Also covered by</h3>(.*?)<h2>', html, re.S)  # the page
        return m.group(1) if m else ""

    grp = db.run("INSERT INTO clients (name) VALUES (?)", ("Reverse Group",))
    reg = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                 ("Reverse Region", grp))
    ven = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                 ("Reverse Venue", reg))

    admin.post(f"/admin/studio/clients/{grp}/licenses",
               data={"title": "RL group grant"}, follow_redirects=False)
    lic = db.one("SELECT * FROM licenses WHERE holder_client_id=? "
                 "ORDER BY id DESC LIMIT 1", (grp,))

    # holder_only (create default) reaches nobody below → venue page shows nothing
    assert "RL group grant" not in _covered(admin.get(f"/admin/studio/clients/{ven}").text)

    # flip to holder_and_descendants → cascades down; venue shows it as a group grant
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": lic["title"], "coverage_scope": "holder_and_descendants"},
               follow_redirects=False)
    block = _covered(admin.get(f"/admin/studio/clients/{ven}").text)
    assert "RL group grant" in block         # the license itself
    assert "Reverse Group" in block          # holder named + linked
    assert "group cascade" in block          # relationship labelled
    # the HOLDER's own page does not list it under 'covered by' — it HOLDS it
    assert "RL group grant" not in _covered(admin.get(f"/admin/studio/clients/{grp}").text)

    # an explicit 'specific' grant held elsewhere, listing the venue → 'added explicitly'
    other = db.run("INSERT INTO clients (name) VALUES (?)", ("Other Holder",))
    admin.post(f"/admin/studio/clients/{other}/licenses",
               data={"title": "RL specific grant"}, follow_redirects=False)
    lic2 = db.one("SELECT * FROM licenses WHERE holder_client_id=? "
                  "ORDER BY id DESC LIMIT 1", (other,))
    admin.post(f"/admin/studio/licenses/{lic2['id']}",
               data={"title": lic2["title"], "coverage_scope": "specific",
                     "cover_client_ids": [str(ven)]}, follow_redirects=False)
    block = _covered(admin.get(f"/admin/studio/clients/{ven}").text)
    assert "RL specific grant" in block and "added explicitly" in block


def test_license_expiry_cue_on_detail(admin):
    """The detail page shows the SAME expiry urgency cue the list strip uses
    (shared expiry_cue helper / threshold), so the two surfaces never disagree.
    Display-only: within threshold → cue; far-out / perpetual → nothing;
    already past → 'lapsed', not 'expiring'."""
    admin.post("/admin/studio/clients",
               data={"name": "Expiry Cue Chef", "company": "Threshold Bistro"},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/licenses",
               data={"title": "Expiry cue license"}, follow_redirects=False)
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/licenses/{lic['id']}/status",
               data={"status": "active"}, follow_redirects=False)
    url = f"/admin/studio/licenses/{lic['id']}"

    # within threshold (active, dated, not perpetual) → the cue renders
    db.run("UPDATE licenses SET ends_on=date('now','+10 days'), perpetual=0 WHERE id=?",
           (lic["id"],))
    body = admin.get(url).text
    assert "License period:" in body and "expiring" in body

    # far out → silent (no cue)
    db.run("UPDATE licenses SET ends_on=date('now','+400 days') WHERE id=?", (lic["id"],))
    assert "License period:" not in admin.get(url).text

    # perpetual → silent even though a stray end date exists
    db.run("UPDATE licenses SET perpetual=1, ends_on=date('now','+10 days') WHERE id=?",
           (lic["id"],))
    assert "License period:" not in admin.get(url).text

    # already lapsed → shows 'lapsed', NOT 'expiring'
    db.run("UPDATE licenses SET perpetual=0, ends_on=date('now','-5 days') WHERE id=?",
           (lic["id"],))
    body = admin.get(url).text
    assert "lapsed" in body and "expiring" not in body


def test_audit_diff_renders_as_chips(admin):
    """Audit diffs are STORED as JSON (territory/channels are json-array strings),
    and that stored shape is the load-bearing append-only record — unchanged. The
    audit VIEW renders those values as chips, not raw ["..."] brackets."""
    import json as _json
    admin.post("/admin/studio/clients", data={"name": "Audit Chips Co"},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/licenses",
               data={"title": "Chips license"}, follow_redirects=False)
    lic = db.one("SELECT * FROM licenses ORDER BY id DESC LIMIT 1")
    # update with multi-value territory + channels → diff stores JSON-array strings
    admin.post(f"/admin/studio/licenses/{lic['id']}",
               data={"title": "Chips license", "usage_tier": "standard",
                     "exclusivity": "non_exclusive", "coverage_scope": "holder_only",
                     "fee": "0", "territory": ["US", "worldwide"],
                     "channels": ["website", "social_organic"]},
               follow_redirects=False)

    # the STORED diff is still raw JSON — the new value is a json-encoded array
    # string (the contract audit.log writes is untouched)
    row = db.one("""SELECT diff_json FROM audit_log WHERE entity_type='license'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
                 (lic["id"],))
    stored = _json.loads(row["diff_json"])
    assert _json.loads(stored["territory"][1]) == ["US", "worldwide"]

    # the VIEW renders chips, not the bracketed/escaped JSON string
    body = admin.get(f"/admin/studio/licenses/{lic['id']}").text
    assert '<span class="diff-chip">US</span>' in body
    assert '<span class="diff-chip">worldwide</span>' in body
    assert '<span class="diff-chip">website</span>' in body
    assert '[&#34;US&#34;,' not in body  # raw escaped-quote JSON must not leak through


def test_crop_preset_engine(client):
    """The render path consumes any crop_presets row generically: a new format
    is a new row, not new code. Proven by adding a 4th preset and rendering it
    with zero changes to imaging.make_crops."""
    import tempfile
    from pathlib import Path as P
    from app import imaging, presets

    # the 3 social ratios ship seeded + active, slugs match the on-disk filenames
    active = presets.active()
    seeded = {ps["slug"]: (ps["width"], ps["height"]) for ps in active}
    assert seeded == {"1x1": (1080, 1080), "4x5": (1080, 1350), "9x16": (1080, 1920)}

    src = P(tempfile.mkdtemp()) / "dish.jpg"
    src.write_bytes(_jpeg_bytes(2000, 1500))

    with tempfile.TemporaryDirectory() as d:
        out = P(d)
        written = imaging.make_crops(str(src), out, "dish", 85, active)
        assert sorted(written) == ["dish_1x1.jpg", "dish_4x5.jpg", "dish_9x16.jpg"]
        with Image.open(out / "dish_9x16.jpg") as im:
            assert im.size == (1080, 1920)

        # add a brand-new format as pure data — a wide menu-board crop — and
        # render again. No code change: the same render path picks it up.
        db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                            target_channel, sort)
                  VALUES ('3x2','Menu board (3:2)','3:2',1500,1000,'menu_print',40)""")
        try:
            written2 = imaging.make_crops(str(src), out, "dish", 85, presets.active())
            assert "dish_3x2.jpg" in written2
            with Image.open(out / "dish_3x2.jpg") as im:
                assert im.size == (1500, 1000)
        finally:
            db.run("DELETE FROM crop_presets WHERE slug='3x2'")


def test_brand_overlay_additive(client):
    """Load-bearing invariant: an overlay can only ADD pixels, never alter the
    base render. With every seeded preset at brand_overlay=0, passing an overlay
    spec must be byte-identical to overlay=None — same SHA-256 per file."""
    import hashlib
    import tempfile
    from pathlib import Path as P
    from app import imaging, presets

    def _hashes(out):
        return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                for f in sorted(out.glob("*.jpg"))}

    src = P(tempfile.mkdtemp()) / "dish.jpg"
    src.write_bytes(_jpeg_bytes(2000, 1500))
    logo = P(tempfile.mkdtemp()) / "logo.png"
    logo.write_bytes(_logo_png())
    overlay = {"path": str(logo), "position": "br", "opacity": 100,
               "scale_pct": 22, "margin_pct": 4}

    active = presets.active()  # 3 seeded presets, all brand_overlay=0
    assert all(ps["brand_overlay"] == 0 for ps in active)
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a, b = P(d1), P(d2)
        imaging.make_crops(str(src), a, "dish", 85, active)
        imaging.make_crops(str(src), b, "dish", 85, active, overlay=overlay)
        assert _hashes(a) == _hashes(b)


def test_brand_overlay_composites(client):
    """With brand_overlay=1 AND an overlay spec, the logo composites onto the
    crop; position + opacity honored. overlay=None on the same preset renders
    the untouched base (additive, never required)."""
    import tempfile
    from pathlib import Path as P
    from app import imaging, presets

    db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                        brand_overlay, sort)
              VALUES ('ov1','Overlay test','1:1',1000,1000,1,90)""")
    try:
        active = presets.active()
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(2000, 1500))
        logo = P(tempfile.mkdtemp()) / "logo.png"
        logo.write_bytes(_logo_png(300, 150, (0, 200, 255, 255)))
        ov = lambda op: {"path": str(logo), "position": "br", "opacity": op,
                         "scale_pct": 30, "margin_pct": 5}

        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            # overlay=None → base render, no logo anywhere
            imaging.make_crops(str(src), out, "base", 85, active)
            with Image.open(out / "base_ov1.jpg") as im:
                base_br = im.getpixel((800, 870))
                base_tl = im.getpixel((50, 50))

            # opacity 100 at br → logo colour bottom-right, top-left untouched
            imaging.make_crops(str(src), out, "full", 85, active, overlay=ov(100))
            with Image.open(out / "full_ov1.jpg") as im:
                full_br = im.getpixel((800, 870))
                full_tl = im.getpixel((50, 50))
            assert _close(full_tl, base_tl)               # outside the logo: unchanged
            assert _close(full_br, (0, 200, 255), 40)     # logo colour composited at br
            assert not _close(full_br, base_br, 40)       # br genuinely changed vs base

            # opacity 50 → br is a blend, distinct from full-opacity (opacity honored)
            imaging.make_crops(str(src), out, "half", 85, active, overlay=ov(50))
            with Image.open(out / "half_ov1.jpg") as im:
                half_br = im.getpixel((800, 870))
            assert not _close(half_br, full_br, 25)
    finally:
        db.run("DELETE FROM crop_presets WHERE slug='ov1'")


def test_overlay_contrast_scrim(client):
    """The brand overlay carries a contrast scrim: a soft dark halo derived from
    the logo's own alpha, composited UNDER the logo so a light wordmark stays
    legible on a bright dish. Detect it where the logo itself can't reach — a
    band just below the logo's lower edge is darkened by the halo, and that
    darkened pixel is NOT the logo colour (proving it's the scrim, not the mark)."""
    import tempfile
    from pathlib import Path as P
    from app import imaging, presets

    db.run("""INSERT INTO crop_presets (slug, name, ratio_label, width, height,
                                        brand_overlay, sort)
              VALUES ('scrim1','Scrim test','1:1',1000,1000,1,91)""")
    try:
        active = [ps for ps in presets.active() if ps["slug"] == "scrim1"]
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(2000, 1500))
        logo = P(tempfile.mkdtemp()) / "logo.png"
        logo.write_bytes(_logo_png(300, 150, (0, 200, 255, 255)))
        ov = {"path": str(logo), "position": "br", "opacity": 100,
              "scale_pct": 30, "margin_pct": 5}
        # logo lands at x:650..950, y:800..950; the scrim halo spills a few px
        # past the lower edge. Scan the band just below: scrim region, no logo.
        band = [(800, y) for y in range(951, 970)]
        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            imaging.make_crops(str(src), out, "base", 85, active)
            with Image.open(out / "base_scrim1.jpg") as im:
                base = im.getpixel((800, 500))  # uniform crop
            imaging.make_crops(str(src), out, "scrim", 85, active, overlay=ov)
            with Image.open(out / "scrim_scrim1.jpg") as im:
                darkest = min((im.getpixel(p) for p in band), key=sum)
        assert sum(darkest) < sum(base) - 60      # halo darkened the bare crop
        assert not _close(darkest, (0, 200, 255), 50)  # it's the scrim, not the logo
    finally:
        db.run("DELETE FROM crop_presets WHERE slug='scrim1'")


def test_brand_kit_admin(admin):
    """Admin kit model: raster-only upload, placement params persisted, the
    newest active kit resolves via brand_kits.overlay_for_client, scoped serve."""
    from app import brand_kits as bk

    cid = db.run("INSERT INTO clients (name, email) VALUES (?,?)",
                 ("Kit Co", "kit@example.com"))

    # non-raster logo rejected (EPS can't composite onto a JPEG)
    r = admin.post(f"/admin/studio/clients/{cid}/kits",
                   files={"logo": ("logo.eps", b"%!PS", "application/postscript")},
                   data={"position": "br", "opacity": 100, "scale_pct": 22, "margin_pct": 4},
                   follow_redirects=False)
    assert r.status_code == 415

    # PNG accepted with placement params
    r = admin.post(f"/admin/studio/clients/{cid}/kits",
                   files={"logo": ("logo.png", _logo_png(120, 60), "image/png")},
                   data={"label": "Primary", "position": "tl", "opacity": 80,
                         "scale_pct": 30, "margin_pct": 6},
                   follow_redirects=False)
    assert r.status_code == 303
    kit = db.one("SELECT * FROM brand_kits WHERE client_id=?", (cid,))
    assert kit["position"] == "tl" and kit["opacity"] == 80 and kit["active"] == 1

    # resolver hands the render path a plain spec dict
    spec = bk.overlay_for_client(cid)
    assert spec["position"] == "tl" and spec["scale_pct"] == 30
    assert os.path.isfile(spec["path"])

    # scoped serve: right client 200, wrong client 404
    assert admin.get(f"/admin/studio/clients/{cid}/kits/{kit['id']}/logo").status_code == 200
    assert admin.get(f"/admin/studio/clients/{cid + 9999}/kits/{kit['id']}/logo").status_code == 404

    # deactivate → resolver returns None (additive, never required)
    admin.post(f"/admin/studio/clients/{cid}/kits/{kit['id']}",
               data={"position": "tl", "opacity": 80, "scale_pct": 30,
                     "margin_pct": 6, "active": 0}, follow_redirects=False)
    assert bk.overlay_for_client(cid) is None


def test_client_tree_cycle_guards(admin):
    """set-parent route enforces both cycle guards: A->A and A->B->A are
    rejected, and a legitimate parent assignment is accepted."""
    from app import clients as ch

    a = db.run("INSERT INTO clients (name) VALUES (?)", ("Tree A",))
    b = db.run("INSERT INTO clients (name) VALUES (?)", ("Tree B",))

    # A->A: a client cannot be its own parent (422; DB CHECK is the backstop)
    r = admin.post(f"/admin/studio/clients/{a}/parent",
                   data={"parent_id": str(a)}, follow_redirects=False)
    assert r.status_code == 422

    # legitimate: B under A
    r = admin.post(f"/admin/studio/clients/{b}/parent",
                   data={"parent_id": str(a)}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (b,))["parent_id"] == a

    # A->B->A: A cannot adopt B as parent now that B is A's descendant (422)
    r = admin.post(f"/admin/studio/clients/{a}/parent",
                   data={"parent_id": str(b)}, follow_redirects=False)
    assert r.status_code == 422
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (a,))["parent_id"] is None

    # helper walks agree
    assert ch.ancestor_ids(b) == [a]
    assert ch.descendant_ids(a) == [b]

    # detach (empty parent_id clears it)
    r = admin.post(f"/admin/studio/clients/{b}/parent",
                   data={"parent_id": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT parent_id FROM clients WHERE id=?", (b,))["parent_id"] is None


def test_client_delete_child_blocker(admin):
    """A parent with children cannot be deleted — not even with force=1.
    Restructuring must go through set-parent, never a delete side-effect."""
    parent = db.run("INSERT INTO clients (name) VALUES (?)", ("Del Group",))
    child = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                   ("Del Venue", parent))

    # plain delete refused
    r = admin.post(f"/admin/studio/clients/{parent}/delete",
                   data={}, follow_redirects=False)
    assert r.status_code == 400
    # force=1 STILL refused (hard blocker)
    r = admin.post(f"/admin/studio/clients/{parent}/delete",
                   data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 400
    assert db.one("SELECT id FROM clients WHERE id=?", (parent,)) is not None

    # detach the child, then the parent deletes cleanly
    admin.post(f"/admin/studio/clients/{child}/parent",
               data={"parent_id": ""}, follow_redirects=False)
    r = admin.post(f"/admin/studio/clients/{parent}/delete",
                   data={}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (parent,)) is None


def test_brand_kit_cascade_nearest_ancestor(admin):
    """3-level tree group->region->venue: the venue inherits the NEAREST active
    ancestor's kit, not the root's. This is what depth-ordered ancestor_ids buys
    us — a 2-level test could not tell nearest from root."""
    from app import brand_kits as bk

    group = db.run("INSERT INTO clients (name) VALUES (?)", ("Casa Group",))
    region = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                    ("Casa West", group))
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                   ("Casa Downtown", region))

    # group kit at top-left, region kit at bottom-right; venue has none
    admin.post(f"/admin/studio/clients/{group}/kits",
               files={"logo": ("g.png", _logo_png(100, 50), "image/png")},
               data={"position": "tl", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
               follow_redirects=False)
    admin.post(f"/admin/studio/clients/{region}/kits",
               files={"logo": ("r.png", _logo_png(100, 50), "image/png")},
               data={"position": "br", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
               follow_redirects=False)

    # venue resolves the REGION kit (nearest), not the group's
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "br"
    assert f"/{region}/" in spec["path"]  # file resolved under the owning client

    # deactivate region's kit → venue falls back to the GROUP kit (next ancestor)
    rk = db.one("SELECT id FROM brand_kits WHERE client_id=?", (region,))
    admin.post(f"/admin/studio/clients/{region}/kits/{rk['id']}",
               data={"position": "br", "opacity": 100, "scale_pct": 20,
                     "margin_pct": 4, "active": 0}, follow_redirects=False)
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "tl"
    assert f"/{group}/" in spec["path"]

    # a venue with its OWN active kit prefers it over any ancestor
    admin.post(f"/admin/studio/clients/{venue}/kits",
               files={"logo": ("v.png", _logo_png(100, 50), "image/png")},
               data={"position": "c", "opacity": 100, "scale_pct": 20, "margin_pct": 4},
               follow_redirects=False)
    spec = bk.overlay_for_client(venue)
    assert spec is not None and spec["position"] == "c"
    assert f"/{venue}/" in spec["path"]


def test_delete_confirm_onsubmit_well_formed(admin):
    """The delete-client confirm() lives in an HTML attribute built from
    tojson, which emits a double-quoted string. If that attribute is itself
    double-quoted the value terminates early and the confirm dialog never
    fires — an irreversible delete loses its guard (found in the live
    walkthrough). The attribute must be single-quoted so tojson's output sits
    inside it intact. A nasty name (apostrophe + double-quote + ampersand)
    exercises every char tojson escapes."""
    import json
    import re

    nasty = 'O\'Brien "Smoke" & Oak'
    cid = db.run("INSERT INTO clients (name) VALUES (?)", (nasty,))

    def assert_intact(page, must_contain):
        # The only single-quoted onsubmit on the page is the delete-client form.
        # If someone reverts to a double-quoted attribute this match drops to
        # zero and the test fails.
        m = re.findall(r"onsubmit='([^']*)'", page)
        assert len(m) == 1, "delete-confirm onsubmit must be single-quoted (and unique)"
        val = m[0]
        assert val.startswith("return confirm(") and val.endswith(")")
        # The argument must be a valid JS/JSON string literal — json.loads
        # raises if the quoting is unbalanced or the value was truncated.
        arg = val[len("return confirm("):-1]
        msg = json.loads(arg)
        assert must_contain in msg
        # The broken double-double form must never reappear.
        assert 'onsubmit="return confirm("' not in page

    # no blocker → "Delete <name>? This is final." (name carries the nasty chars)
    page = admin.get(f"/admin/studio/clients/{cid}").text
    assert_intact(page, "This is final.")

    # with a child blocker → the WARNING summary, still well-formed
    child = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                   ("Child Venue", cid))
    page = admin.get(f"/admin/studio/clients/{cid}").text
    assert_intact(page, "child client")
    assert 'name="force" value="1"' in page

    db.run("DELETE FROM clients WHERE id=?", (child,))
    db.run("DELETE FROM clients WHERE id=?", (cid,))


def test_crop_preset_admin(admin):
    """Admin CRUD over crop_presets — the surface that makes the overlay engine
    and future delivery/print formats reachable without a DB edit. Every write
    lands an audit_log row (entity_type='crop_preset', R14: this table feeds the
    public render path); a no-op edit writes none (append-only stays clean); a
    slug with a path separator / space / quote is rejected cleanly, not 500'd;
    and slug is immutable on edit."""
    import json as _json

    # the list page renders with the seeded presets + the new nav link
    page = admin.get("/admin/studio/presets")
    assert page.status_code == 200
    assert "Crop presets" in page.text and "1x1" in page.text

    # add a new preset → row persisted with seeded defaults + a 'create' audit row
    r = admin.post("/admin/studio/presets",
                   data={"slug": "3x4test", "name": "Tall (3:4)", "ratio_label": "3:4",
                         "width": "1200", "height": "1600", "centering_x": "0.5",
                         "centering_y": "0.4", "target_channel": "pinterest", "sort": "50"},
                   follow_redirects=False)
    assert r.status_code == 303
    ps = db.one("SELECT * FROM crop_presets WHERE slug='3x4test'")
    assert ps and ps["width"] == 1200 and ps["height"] == 1600
    assert ps["active"] == 1 and ps["brand_overlay"] == 0  # schema-seeded defaults
    assert len(db.all_("""SELECT 1 FROM audit_log WHERE entity_type='crop_preset'
                          AND entity_id=? AND action='create'""", (ps["id"],))) == 1

    # bad slugs rejected cleanly (400, not a 500) and never persisted. The slug
    # is a filename key + URL token, so a path separator is the dangerous case.
    # (uppercase is normalized to lowercase, not rejected — so it's not here)
    for s in ["../etc", "a/b", "has space", 'quo"te', "dot.ted", ""]:
        rr = admin.post("/admin/studio/presets",
                        data={"slug": s, "name": "x", "ratio_label": "1:1",
                              "width": "100", "height": "100"}, follow_redirects=False)
        assert rr.status_code == 400, f"slug {s!r} must be rejected"
    assert not db.one("SELECT 1 FROM crop_presets WHERE slug='../etc'")

    # duplicate slug → clean 400, not a raw IntegrityError 500
    dup = admin.post("/admin/studio/presets",
                     data={"slug": "3x4test", "name": "dupe", "ratio_label": "3:4",
                           "width": "1200", "height": "1600"}, follow_redirects=False)
    assert dup.status_code == 400

    # bad dimensions / centering rejected cleanly
    assert admin.post("/admin/studio/presets",
                      data={"slug": "bad1", "name": "x", "ratio_label": "1:1",
                            "width": "0", "height": "100"},
                      follow_redirects=False).status_code == 400
    assert admin.post("/admin/studio/presets",
                      data={"slug": "bad2", "name": "x", "ratio_label": "1:1",
                            "width": "100", "height": "100", "centering_x": "2"},
                      follow_redirects=False).status_code == 400

    # edit with a real change → 'update' audit row with the diff; slug immutable
    # even if a slug field is smuggled into the form.
    r = admin.post(f"/admin/studio/presets/{ps['id']}",
                   data={"slug": "hacked", "name": "Tall portrait", "ratio_label": "3:4",
                         "width": "1200", "height": "1600", "centering_x": "0.5",
                         "centering_y": "0.4", "target_channel": "pinterest",
                         "sort": "55"}, follow_redirects=False)
    assert r.status_code == 303
    ps2 = db.one("SELECT * FROM crop_presets WHERE id=?", (ps["id"],))
    assert ps2["slug"] == "3x4test"  # NOT 'hacked' — slug never updated
    assert ps2["name"] == "Tall portrait" and ps2["sort"] == 55
    upd = db.one("""SELECT diff_json FROM audit_log WHERE entity_type='crop_preset'
                    AND entity_id=? AND action='update' ORDER BY id DESC LIMIT 1""",
                 (ps["id"],))
    diff = _json.loads(upd["diff_json"])
    assert diff["name"] == ["Tall (3:4)", "Tall portrait"]
    assert diff["sort"] == [50, 55]
    assert "slug" not in diff  # slug isn't a tracked editable field

    # a no-op edit (identical values) writes NO new audit row
    before = db.one("""SELECT COUNT(*) AS n FROM audit_log
                       WHERE entity_type='crop_preset' AND entity_id=?""",
                    (ps["id"],))["n"]
    admin.post(f"/admin/studio/presets/{ps['id']}",
               data={"name": "Tall portrait", "ratio_label": "3:4", "width": "1200",
                     "height": "1600", "centering_x": "0.5", "centering_y": "0.4",
                     "target_channel": "pinterest", "sort": "55"},
               follow_redirects=False)
    after = db.one("""SELECT COUNT(*) AS n FROM audit_log
                      WHERE entity_type='crop_preset' AND entity_id=?""",
                   (ps["id"],))["n"]
    assert after == before

    # overlay toggle flips the flag + lands its own audit row with the diff
    admin.post(f"/admin/studio/presets/{ps['id']}/overlay", follow_redirects=False)
    assert db.one("SELECT brand_overlay FROM crop_presets WHERE id=?",
                  (ps["id"],))["brand_overlay"] == 1
    ov = db.one("""SELECT diff_json FROM audit_log WHERE entity_type='crop_preset'
                   AND entity_id=? AND action='overlay_change' ORDER BY id DESC LIMIT 1""",
                (ps["id"],))
    assert _json.loads(ov["diff_json"])["brand_overlay"] == [0, 1]

    # active toggle flips + audit row; the trail (with the action) renders on the page
    admin.post(f"/admin/studio/presets/{ps['id']}/active", follow_redirects=False)
    assert db.one("SELECT active FROM crop_presets WHERE id=?", (ps["id"],))["active"] == 0
    assert "active_change" in admin.get("/admin/studio/presets").text

    # tidy up so the deactivate-invariant test below sees only the 3 seeded presets
    db.run("DELETE FROM crop_presets WHERE id=?", (ps["id"],))


def test_preset_deactivate_via_admin_holds_public_invariant(admin):
    """Slice D proved that deactivating a preset (via a direct DB write) makes
    portal.crop() 404 and drops it from crops_zip. This proves the SAME invariant
    when the deactivation comes through the NEW admin route — the admin must not
    be able to create a state that breaks the public render path, only a clean
    absence. (The route reads presets.active(); deactivation is the only off
    switch, there is no destructive delete.)"""
    from pathlib import Path as P

    from app import jobs, presets

    # Build a fully self-contained chain rather than leaning on suite-leftover
    # favorites (later delete-tests remove those): client → published gallery
    # linked to that client → a ready photo → a visitor who favorited it →
    # a published portal with a known PIN → a rendered crop on disk per preset.
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Crop Invariant Co",))
    gid = db.run("INSERT INTO galleries (slug, title, pin, client_id, published) "
                 "VALUES (?,?,?,?,1)",
                 ("CropInvariantGal01", "Crop invariant shoot", "1234", cid))
    stem = "cropinvariant0001"
    aid = db.run("INSERT INTO assets (gallery_id, kind, filename, stored, status) "
                 "VALUES (?,?,?,?,?)",
                 (gid, "photo", "plate.jpg", f"{stem}.jpg", "ready"))
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)",
                 (gid, "vtoken-crop-invariant"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aid))
    db.run("INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
           (cid, "CropInvariantPortal01", "4321"))
    p = db.one("SELECT * FROM portals WHERE slug='CropInvariantPortal01'")
    a = db.one("SELECT * FROM assets WHERE id=?", (aid,))

    # write a dummy rendered crop on disk for every active preset slug
    crops = jobs.crops_dir(gid)
    crops.mkdir(parents=True, exist_ok=True)
    active = presets.active()
    assert len(active) >= 2, "need >=2 active presets to prove surgical exclusion"
    for ps in active:
        (crops / f"{stem}_{ps['slug']}.jpg").write_bytes(_jpeg_bytes(64, 64))
    target = active[0]
    slug = target["slug"]

    with TestClient(app) as pub:
        pub.post(f"/portal/{p['slug']}/pin", data={"pin": p["pin"]}, follow_redirects=False)
        # active: the crop resolves and the zip bundles it
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 200
        z = pub.get(f"/portal/{p['slug']}/crops.zip")
        assert z.status_code == 200
        before = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert any(n.endswith(f"_{slug}.jpg") for n in before)

        # deactivate THROUGH THE ADMIN ROUTE (not a db.run UPDATE)
        r = admin.post(f"/admin/studio/presets/{target['id']}/active",
                       follow_redirects=False)
        assert r.status_code == 303
        assert db.one("SELECT active FROM crop_presets WHERE id=?",
                      (target["id"],))["active"] == 0

        # public path now refuses the slug cleanly and drops it from the zip,
        # while other active presets stay bundled (surgical, not all-or-nothing)
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 404
        z2 = pub.get(f"/portal/{p['slug']}/crops.zip")
        assert z2.status_code == 200
        after = zipfile.ZipFile(io.BytesIO(z2.content)).namelist()
        assert not any(n.endswith(f"_{slug}.jpg") for n in after)
        assert after, "other active presets should still bundle"

        # reactivate via admin → resolves again; no destructive state was created
        admin.post(f"/admin/studio/presets/{target['id']}/active", follow_redirects=False)
        assert pub.get(f"/portal/{p['slug']}/crop/{a['id']}/{slug}").status_code == 200


def test_client_children_roster(admin):
    """Read-only group->venue roster on the client page: a group with venues
    under it lists every descendant (top-down), while a childless client renders
    no roster at all (clean empty state). Completes the hierarchy — the parent
    selector is the venue->group direction, this is the inverse view."""
    group = db.run("INSERT INTO clients (name) VALUES (?)", ("Roster Group",))
    region = db.run("INSERT INTO clients (name, company, parent_id) VALUES (?,?,?)",
                    ("Roster West", "West Co", group))
    venue = db.run("INSERT INTO clients (name, parent_id) VALUES (?,?)",
                   ("Roster Bistro", region))
    lone = db.run("INSERT INTO clients (name) VALUES (?)", ("Roster Solo",))

    # the group's page lists BOTH descendants (region + grandchild venue)
    page = admin.get(f"/admin/studio/clients/{group}").text
    assert "Venues under this group" in page
    assert "Roster West" in page and "West Co" in page
    assert "Roster Bistro" in page
    assert f'href="/admin/studio/clients/{region}"' in page
    assert f'href="/admin/studio/clients/{venue}"' in page

    # a childless client renders no roster block whatsoever (clean empty state)
    solo = admin.get(f"/admin/studio/clients/{lone}").text
    assert "Venues under this group" not in solo

    # the leaf venue is also childless → no roster, even though it HAS a parent
    leaf = admin.get(f"/admin/studio/clients/{venue}").text
    assert "Venues under this group" not in leaf


def test_delivery_app_presets_are_data_not_code(admin):
    """The boundary thesis end-to-end: a new delivery channel (DoorDash, Uber
    Eats) is a new crop_presets ROW entered through the slice-5 admin UI, not new
    code. These rows render through the SAME imaging.make_crops as the seeded
    social ratios with zero render-path changes; sRGB/72dpi come from the schema
    defaults (the admin form doesn't touch them), brand_overlay stays off (a
    restaurant's own platform listing shouldn't carry the studio wordmark)."""
    import tempfile
    from pathlib import Path as P

    from app import imaging, presets

    # real platform specs: DoorDash menu/detail hero is 16:9 (min 1400×800);
    # Uber Eats cover/hero is 5:4 at 2880×2304. Entered THROUGH THE ADMIN ROUTE.
    rows = [
        {"slug": "doordash", "name": "DoorDash hero (16:9)", "ratio_label": "16:9",
         "width": "1920", "height": "1080", "target_channel": "doordash", "sort": "40"},
        {"slug": "ubereats", "name": "Uber Eats cover (5:4)", "ratio_label": "5:4",
         "width": "2880", "height": "2304", "target_channel": "ubereats", "sort": "50"},
    ]
    try:
        for r in rows:
            assert admin.post("/admin/studio/presets", data=r,
                              follow_redirects=False).status_code == 303

        dd = db.one("SELECT * FROM crop_presets WHERE slug='doordash'")
        ue = db.one("SELECT * FROM crop_presets WHERE slug='ubereats'")
        # schema defaults the admin UI never set — exactly the sRGB/72dpi spec,
        # no overlay (restaurant's own listing), active on creation
        for ps in (dd, ue):
            assert ps["color_space"] == "sRGB" and ps["dpi"] == 72
            assert ps["brand_overlay"] == 0 and ps["active"] == 1
        assert (dd["width"], dd["height"]) == (1920, 1080)
        assert (ue["width"], ue["height"]) == (2880, 2304)

        # they render through the EXISTING generic path, alongside the seeded
        # ratios, in one make_crops call — no per-channel branch, no code change
        src = P(tempfile.mkdtemp()) / "dish.jpg"
        src.write_bytes(_jpeg_bytes(3000, 2400))
        with tempfile.TemporaryDirectory() as d:
            out = P(d)
            written = imaging.make_crops(str(src), out, "dish", 85, presets.active())
            assert "dish_doordash.jpg" in written and "dish_ubereats.jpg" in written
            with Image.open(out / "dish_doordash.jpg") as im:
                assert im.size == (1920, 1080)  # 16:9, exact
            with Image.open(out / "dish_ubereats.jpg") as im:
                assert im.size == (2880, 2304)  # 5:4, exact
    finally:
        db.run("DELETE FROM crop_presets WHERE slug IN ('doordash','ubereats')")


def test_recurring_plan_draft_generation(admin):
    """Recurring billing slice 1: a plan is a template that GENERATES a draft
    invoice — it never sends or charges (manual-send doctrine intact). The plan
    keys off the calendar month; a second generate in the same period is a
    dedupe no-op so a double-click can't spawn duplicate invoices."""
    from app.admin.recurring import _period

    # fresh client + project so the plan list is isolated
    admin.post("/admin/studio/clients",
               data={"name": "Retainer Co", "company": "Monthly Bites",
                     "email": "ops@monthlybites.com", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Brand partner retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create plan from the project page form
    r = admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
                   data={"title": "Monthly content retainer"}, follow_redirects=False)
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    assert plan["project_id"] == proj["id"] and plan["active"] == 1
    assert plan["total_cents"] == 0 and plan["last_run_period"] is None

    # project page lists the plan
    assert "Monthly content retainer" in admin.get(
        f"/admin/studio/projects/{proj['id']}").text

    # generating with a zero total is refused (nothing to bill)
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate",
                   follow_redirects=False)
    assert r.status_code == 400

    # edit: line items + anchor day; total recalculates from the rows
    r = admin.post(f"/admin/studio/recurring/{plan['id']}",
                   data={"title": "Monthly content retainer",
                         "item_label_0": "Content day", "item_qty_0": "1",
                         "item_price_0": "1200",
                         "item_label_1": "Reels (3)", "item_qty_1": "3",
                         "item_price_1": "150",
                         "anchor_day": "5", "active": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    assert plan["total_cents"] == 165000 and plan["anchor_day"] == 5

    # anchor day outside 1–28 is rejected
    assert admin.post(f"/admin/studio/recurring/{plan['id']}",
                      data={"title": "x", "anchor_day": "31"},
                      follow_redirects=False).status_code == 400

    # generate → a DRAFT invoice linked to the plan, period stamped
    period = _period()
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate",
                   follow_redirects=False)
    assert r.status_code == 303
    inv = db.one("SELECT * FROM invoices WHERE recurring_plan_id=? "
                 "ORDER BY id DESC LIMIT 1", (plan["id"],))
    assert inv is not None
    assert inv["status"] == "draft"  # manual-send preserved — nothing auto-sends
    assert inv["total_cents"] == 165000
    assert period in inv["title"]
    assert db.one("SELECT last_run_period FROM recurring_plans WHERE id=?",
                  (plan["id"],))["last_run_period"] == period

    # second generate in the same period is a dedupe no-op (400), no new invoice
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/generate",
                   follow_redirects=False)
    assert r.status_code == 400
    assert db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?",
                  (plan["id"],))["n"] == 1

    # a paused plan refuses to generate
    db.run("UPDATE recurring_plans SET active=0, last_run_period=NULL WHERE id=?",
           (plan["id"],))
    assert admin.post(f"/admin/studio/recurring/{plan['id']}/generate",
                      follow_redirects=False).status_code == 400

    # soft-delete drops it from the project page but keeps the generated invoice
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT deleted_at FROM recurring_plans WHERE id=?",
                  (plan["id"],))["deleted_at"] is not None
    # the plan's own row link is gone (its name lingers only in the kept
    # invoice's title, which is expected — generated invoices survive)
    assert f"/admin/studio/recurring/{plan['id']}" not in admin.get(
        f"/admin/studio/projects/{proj['id']}").text
    assert db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?",
                  (plan["id"],))["n"] == 1


def test_recurring_scheduler_sweep(admin):
    """Slice 2 — the in-process scheduler: on the anchor day each month Mise
    auto-generates that period's DRAFT with no click (drafts only, manual-send
    doctrine intact). The sweep is date-driven (run_due_plans(today=...)) and
    idempotent — the last_run_period claim means a second sweep the same month,
    or an overlapping manual click, can never double-bill."""
    import datetime as dt

    from app.admin import recurring

    admin.post("/admin/studio/clients",
               data={"name": "Sweep Diner", "company": "Sweep Co",
                     "email": "ops@sweep.co", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Sweep retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/recurring/{plan['id']}",
               data={"title": "Sweep retainer", "item_label_0": "Content day",
                     "item_qty_0": "1", "item_price_0": "1000",
                     "anchor_day": "10", "active": "1"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    assert plan["total_cents"] == 100000 and plan["anchor_day"] == 10

    def invcount():
        return db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?",
                      (plan["id"],))["n"]

    # before the anchor day → the plan isn't due yet, nothing generated
    recurring.run_due_plans(today=dt.date(2026, 9, 9))
    assert invcount() == 0

    # on the anchor day → exactly one DRAFT, period stamped
    recurring.run_due_plans(today=dt.date(2026, 9, 10))
    assert invcount() == 1
    inv = db.one("SELECT * FROM invoices WHERE recurring_plan_id=? "
                 "ORDER BY id DESC LIMIT 1", (plan["id"],))
    assert inv["status"] == "draft" and "2026-09" in inv["title"]
    assert db.one("SELECT last_run_period FROM recurring_plans WHERE id=?",
                  (plan["id"],))["last_run_period"] == "2026-09"

    # a later sweep the SAME month is a no-op — the period claim dedupes
    recurring.run_due_plans(today=dt.date(2026, 9, 25))
    assert invcount() == 1

    # next month → a fresh draft
    recurring.run_due_plans(today=dt.date(2026, 10, 12))
    assert invcount() == 2
    assert "2026-10" in db.one(
        "SELECT title FROM invoices WHERE recurring_plan_id=? ORDER BY id DESC LIMIT 1",
        (plan["id"],))["title"]

    # paused → the sweep skips it entirely
    db.run("UPDATE recurring_plans SET active=0 WHERE id=?", (plan["id"],))
    recurring.run_due_plans(today=dt.date(2026, 11, 15))
    assert invcount() == 2

    # everything the sweep made is a DRAFT — it never sends or charges
    assert all(r["status"] == "draft" for r in db.all_(
        "SELECT status FROM invoices WHERE recurring_plan_id=?", (plan["id"],)))

    # leave no active plan lingering in the shared DB for later modules
    db.run("UPDATE recurring_plans SET deleted_at=datetime('now') WHERE id=?",
           (plan["id"],))


def test_retainer_draft_waiting_strip(admin):
    """Slice 3 — the manual-send safety valve: once slice 2's scheduler can
    create retainer drafts unattended, those drafts must not rot unsent. The
    studio dashboard surfaces a 'Retainer drafts waiting to send' strip listing
    every unsent recurring-plan draft; sending one removes it from the strip."""
    import datetime as dt

    from app.admin import recurring

    admin.post("/admin/studio/clients",
               data={"name": "Waiting Diner", "company": "Waiting Co",
                     "email": "ops@waiting.co", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Waiting retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/recurring/{plan['id']}",
               data={"title": "Waiting retainer", "item_label_0": "Content day",
                     "item_qty_0": "1", "item_price_0": "1000",
                     "anchor_day": "10", "active": "1"}, follow_redirects=False)

    # this plan hasn't generated anything yet → its invoice isn't waiting.
    # (Earlier recurring tests leave their own drafts in the shared DB, so we
    # assert on THIS plan's invoice, not the strip's global presence.)
    assert db.one("SELECT COUNT(*) AS n FROM invoices WHERE recurring_plan_id=?",
                  (plan["id"],))["n"] == 0

    # scheduler sweep makes a DRAFT unattended → it now nags on the dashboard
    recurring.run_due_plans(today=dt.date(2026, 9, 10))
    inv = db.one("SELECT * FROM invoices WHERE recurring_plan_id=? "
                 "ORDER BY id DESC LIMIT 1", (plan["id"],))
    assert inv["status"] == "draft"
    page = admin.get("/admin/studio/activity").text
    assert "Retainer drafts waiting to send" in page
    assert f"/admin/studio/invoices/{inv['id']}" in page

    # Kevin reviews and Sends it → it drops off the waiting strip
    r = admin.post(f"/admin/studio/invoices/{inv['id']}/send", follow_redirects=False)
    assert r.status_code == 303
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/invoices/{inv['id']}" not in page

    # leave no active plan lingering in the shared DB for later modules
    db.run("UPDATE recurring_plans SET deleted_at=datetime('now') WHERE id=?",
           (plan["id"],))


def test_retainer_deliverable_quota(admin):
    """Domain G slice 1: a retainer commits to a monthly deliverable quota
    (labeled targets), and Kevin keeps a MANUAL per-period log of what was
    delivered. The plan page lines the log up against the quota as on-track/met.
    Encodes WHY: the quota is advisory content-tracking only — it never touches
    invoices/billing and is never auto-credited from galleries (Kevin's count, by
    doctrine), and the quota is a plan not a cap, so un-targeted deliveries still
    log (as 'extra') without being rejected."""
    import json

    from app.admin.recurring import _period

    admin.post("/admin/studio/clients",
               data={"name": "Quota Kitchen", "company": "Quota Co",
                     "email": "ops@quota.co", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Brand partner retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    assert plan["quota"] == "[]"  # default — no commitment until set

    # set the quota (labeled targets) alongside the billing line items; quota is
    # parsed independently and stored as JSON, leaving the invoice total alone.
    r = admin.post(f"/admin/studio/recurring/{plan['id']}",
                   data={"title": "Content retainer",
                         "item_label_0": "Content day", "item_qty_0": "1",
                         "item_price_0": "1200", "anchor_day": "5", "active": "1",
                         "quota_label_0": "Hero images", "quota_target_0": "20",
                         "quota_label_1": "Reels", "quota_target_1": "4"},
                   follow_redirects=False)
    assert r.status_code == 303
    plan = db.one("SELECT * FROM recurring_plans WHERE id=?", (plan["id"],))
    quota = json.loads(plan["quota"])
    assert quota == [{"label": "Hero images", "target": 20},
                     {"label": "Reels", "target": 4}]
    assert plan["total_cents"] == 120000  # quota did NOT bleed into billing

    period = _period()
    # nothing logged yet → full target outstanding
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "Hero images" in page and "20 to go" in page

    # log a partial delivery this period → progress reflects it
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries",
                   data={"label": "Hero images", "qty": "5", "period": period,
                         "note": "spring menu batch"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (plan["id"],))["n"] == 1
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "15 to go" in page  # 20 target − 5 delivered

    # a second entry sums with the first; hitting the target reads "met"
    admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries",
               data={"label": "Hero images", "qty": "15", "period": period},
               follow_redirects=False)
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "met" in page

    # an un-targeted label still logs (quota is a plan, not a cap) → shows 'extra'
    admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries",
               data={"label": "Stories", "qty": "3", "period": period},
               follow_redirects=False)
    page = admin.get(f"/admin/studio/recurring/{plan['id']}").text
    assert "Stories" in page and "extra" in page

    # bad inputs are rejected, not silently coerced
    assert admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries",
                      data={"label": "Hero images", "qty": "0", "period": period},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries",
                      data={"label": "Hero images", "qty": "1", "period": "nope"},
                      follow_redirects=False).status_code == 400

    # delete one entry → it leaves the log
    e = db.one("SELECT id FROM retainer_deliveries WHERE plan_id=? AND label='Stories'",
               (plan["id"],))
    r = admin.post(f"/admin/studio/recurring/{plan['id']}/deliveries/{e['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries "
                  "WHERE plan_id=? AND label='Stories'", (plan["id"],))["n"] == 0

    # CASCADE: a HARD plan delete cascades the deliveries (FK ON DELETE CASCADE).
    db.run("DELETE FROM recurring_plans WHERE id=?", (plan["id"],))
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (plan["id"],))["n"] == 0


def test_retainer_behind_quota_strip(admin):
    """Domain G slice 2 — the pace-aware 'behind quota' dashboard strip. A retainer
    surfaces only when its this-period delivery lags the month's run-rate (a label
    is behind when delivered < target × fraction-of-month-elapsed). Encodes WHY two
    date-independent edges hold regardless of the day the test runs: a quota with
    NOTHING delivered is always behind pace (0 < target×elapsed for any day ≥1), so
    it always shows; a FULLY delivered quota is never behind (done == target ≥
    target×elapsed even on the last day), so it stays silent — the strip nags about
    real risk, not about every retainer at the start of the month."""
    from app.admin.recurring import _period

    def mk_plan(client_name, plan_title, quota_label, target):
        admin.post("/admin/studio/clients",
                   data={"name": client_name, "company": client_name + " Co",
                         "email": "", "phone": ""}, follow_redirects=False)
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/clients/{c['id']}/projects",
                   data={"title": "Retainer"}, follow_redirects=False)
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
                   data={"title": plan_title}, follow_redirects=False)
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/recurring/{plan['id']}",
                   data={"title": plan_title, "item_label_0": "Content day",
                         "item_qty_0": "1", "item_price_0": "1000",
                         "anchor_day": "5", "active": "1",
                         "quota_label_0": quota_label, "quota_target_0": str(target)},
                   follow_redirects=False)
        return plan["id"]

    period = _period()
    behind_id = mk_plan("Behind Bistro", "Behind retainer", "Hero images", 20)
    ontrack_id = mk_plan("Ontrack Oyster", "Ontrack retainer", "Reels", 4)
    # fully deliver the on-track plan's quota → never behind pace, any day
    admin.post(f"/admin/studio/recurring/{ontrack_id}/deliveries",
               data={"label": "Reels", "qty": "4", "period": period},
               follow_redirects=False)

    page = admin.get("/admin/studio/activity").text
    assert "Retainers behind quota" in page
    # the un-delivered retainer is on the strip with its worst-label gap
    assert f"/admin/studio/recurring/{behind_id}" in page
    assert "Hero images" in page and "20 to go" in page
    # the fully-delivered retainer is NOT (met its pace)
    assert f"/admin/studio/recurring/{ontrack_id}" not in page

    # delivering enough to clear the run-rate drops it off the strip. On the last
    # day of the month elapsed==1.0, so only a FULL delivery is guaranteed to clear
    # pace on every possible run date — deliver the whole target.
    admin.post(f"/admin/studio/recurring/{behind_id}/deliveries",
               data={"label": "Hero images", "qty": "20", "period": period},
               follow_redirects=False)
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{behind_id}" not in page

    # a PAUSED behind plan never nags (you chose to stop the retainer)
    paused_id = mk_plan("Paused Pub", "Paused retainer", "Stories", 10)
    db.run("UPDATE recurring_plans SET active=0 WHERE id=?", (paused_id,))
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{paused_id}" not in page

    # clean up: hard-delete the plans so no active quota plan lingers for later modules
    for pid in (behind_id, ontrack_id, paused_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_retainer_content_calendar(admin):
    """Domain G slice 3 — a forward-looking content calendar on a retainer plan.
    Dated slots (label + optional title/note) move planned → shot → delivered. The
    plan page shows only THIS period's slots. Encodes WHY the calendar is DECOUPLED
    from the slice-1 delivery log: advancing a slot to 'delivered' is purely a
    planning state and must NOT touch retainer_deliveries (the quota count stays
    Kevin's manual log, by doctrine). Bad date / blank label are rejected, a hard
    plan delete cascades the calendar (FK ON DELETE CASCADE)."""
    from app.admin.recurring import _period

    admin.post("/admin/studio/clients",
               data={"name": "Calendar Cafe", "company": "Cal Co",
                     "email": "", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()  # 'YYYY-MM'
    this_period_date = f"{period}-15"

    # add a slot in the current period → it renders on the calendar
    r = admin.post(f"/admin/studio/recurring/{pid}/calendar",
                   data={"slot_date": this_period_date, "label": "Hero images",
                         "title": "Spring menu hero", "note": "pasta close-up"},
                   follow_redirects=False)
    assert r.status_code == 303
    slot = db.one("SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                  (pid,))
    assert slot["status"] == "planned"  # default
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Spring menu hero" in page and this_period_date in page

    # a slot dated outside this period is NOT shown on the period view
    admin.post(f"/admin/studio/recurring/{pid}/calendar",
               data={"slot_date": "2099-01-10", "label": "Reels"},
               follow_redirects=False)
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "2099-01-10" not in page

    # advance status → reflected, and the delivery log is UNTOUCHED (decoupled)
    r = admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
                   data={"status": "delivered"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT status FROM content_calendar WHERE id=?",
                  (slot["id"],))["status"] == "delivered"
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0  # marking delivered did NOT auto-log a delivery

    # bad inputs rejected, not coerced
    assert admin.post(f"/admin/studio/recurring/{pid}/calendar",
                      data={"slot_date": "nope", "label": "Hero images"},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/recurring/{pid}/calendar",
                      data={"slot_date": this_period_date, "label": "  "},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
                      data={"status": "shipped"},
                      follow_redirects=False).status_code == 400

    # delete a slot → it leaves the calendar
    r = admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM content_calendar WHERE id=?",
                  (slot["id"],))["n"] == 0

    # CASCADE: a hard plan delete cascades the calendar slots
    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))
    assert db.one("SELECT COUNT(*) AS n FROM content_calendar WHERE plan_id=?",
                  (pid,))["n"] == 0


def test_retainer_assisted_credit_prefill(admin):
    """Domain G slice 4 — assisted-credit pre-fill closes the forget-to-log hole
    WITHOUT auto-crediting. Flipping a calendar slot to 'delivered' redirects with
    credit_* query params that seed the delivery-log form (label, qty=1, period from
    the slot date); the human still submits. Encodes WHY two invariants must hold:
    (a) the slot→delivered transition itself writes NO retainer_deliveries row — the
    slice-3 decoupling guarantee survives; (b) the pre-fill carries the right
    label/qty/period but performs no write on its own (only the existing /deliveries
    POST writes). Date-independent: period is derived from _period() and the slot date
    is built from it, so the assertions don't depend on the calendar day."""
    from urllib.parse import parse_qs, urlparse

    from app.admin.recurring import _period

    admin.post("/admin/studio/clients",
               data={"name": "Credit Counter", "company": "Credit Co",
                     "email": "", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()                 # 'YYYY-MM'
    slot_date = f"{period}-09"
    admin.post(f"/admin/studio/recurring/{pid}/calendar",
               data={"slot_date": slot_date, "label": "Hero images"},
               follow_redirects=False)
    slot = db.one("SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                  (pid,))

    # flip planned → delivered: 303 redirect carries the pre-fill query params
    r = admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
                   data={"status": "delivered"}, follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]   # substr(slot_date,1,7)
    # INVARIANT (a): the transition wrote NO delivery row — decoupling preserved
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0

    # INVARIANT (b): rendering the pre-filled page injects the values as form
    # DEFAULTS and still writes nothing on its own
    page = admin.get(f"/admin/studio/recurring/{pid}",
                     params={"credit_label": "Hero images", "credit_qty": "1",
                             "credit_period": period}).text
    assert 'value="Hero images"' in page          # label seeded
    assert f'value="{period}"' in page            # period seeded
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0               # GET is not a write

    # the pre-fill fires ONLY on a transition into delivered, not on a re-save:
    # delivered → delivered carries no credit params
    r2 = admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
                    data={"status": "delivered"}, follow_redirects=False)
    assert "credit_label" not in r2.headers["location"]
    # nor on a non-delivered transition (planned/shot)
    admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
               data={"status": "shot"}, follow_redirects=False)
    r3 = admin.post(f"/admin/studio/recurring/{pid}/calendar/{slot['id']}/status",
                    data={"status": "shot"}, follow_redirects=False)
    assert "credit_label" not in r3.headers["location"]

    # the human submit is the ONLY thing that writes the count
    admin.post(f"/admin/studio/recurring/{pid}/deliveries",
               data={"label": "Hero images", "qty": "1", "period": period},
               follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 1

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_content_due_strip(admin, monkeypatch):
    """Domain G slice 5 — the 'Content due' dashboard strip: calendar slots
    scheduled this period and not yet delivered (the 'what's coming' companion to
    behind-quota's 'what's at risk'). Encodes WHY each edge holds: a planned/shot
    in-period slot appears; a DELIVERED slot drops off (composes with slice-4's
    assisted credit — flipping to delivered clears it here); an OVERDUE slot
    (slot_date < today, not delivered) appears flagged urgent (most actionable, never
    hidden); empty ⇒ the strip is silent. This strip is date-WINDOWED, so today is
    pinned to a fixed date — the assertions can't flake by calendar day (stronger
    than relative dates)."""
    import datetime as _dt
    import types

    from app.admin import studio

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 15)

    monkeypatch.setattr(studio, "dt", types.SimpleNamespace(
        date=_FixedDate, datetime=_dt.datetime,
        timezone=_dt.timezone, timedelta=_dt.timedelta))
    period = "2026-06"

    def mk_plan(client_name, slot_date, status="planned"):
        admin.post("/admin/studio/clients",
                   data={"name": client_name, "company": client_name + " Co",
                         "email": "", "phone": ""}, follow_redirects=False)
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/clients/{c['id']}/projects",
                   data={"title": "Retainer"}, follow_redirects=False)
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
                   data={"title": client_name + " retainer"}, follow_redirects=False)
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/recurring/{plan['id']}/calendar",
                   data={"slot_date": slot_date, "label": "Hero images"},
                   follow_redirects=False)
        slot = db.one("SELECT * FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                      (plan["id"],))
        if status != "planned":
            admin.post(f"/admin/studio/recurring/{plan['id']}/calendar/{slot['id']}/status",
                       data={"status": status}, follow_redirects=False)
        return plan["id"]

    # future in-period (not overdue), overdue (past in-period), delivered (dropped)
    due_id = mk_plan("Due Diner", f"{period}-25")
    overdue_id = mk_plan("Overdue Oven", f"{period}-05")
    delivered_id = mk_plan("Done Deli", f"{period}-20", status="delivered")

    page = admin.get("/admin/studio/activity").text
    assert "Content due" in page
    # planned/shot in-period slots appear, linking to the plan's #calendar anchor
    assert f"/admin/studio/recurring/{due_id}#calendar" in page
    assert f"/admin/studio/recurring/{overdue_id}#calendar" in page
    # the overdue chip is flagged overdue in its when-label (specific to the chip
    # text, not the upcoming-overdue CSS class)
    assert ">overdue</span>" in page
    # a delivered slot drops off the strip (composes with slice-4 assisted credit)
    assert f"/admin/studio/recurring/{delivered_id}#calendar" not in page

    # marking the remaining slots delivered empties the strip → it goes silent
    for pid_ in (due_id, overdue_id):
        s = db.one("SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id LIMIT 1",
                   (pid_,))
        admin.post(f"/admin/studio/recurring/{pid_}/calendar/{s['id']}/status",
                   data={"status": "delivered"}, follow_redirects=False)
    page = admin.get("/admin/studio/activity").text
    assert "Content due" not in page  # silent when empty

    # clean up: hard-delete the plans (cascades their calendar slots)
    for pid_ in (due_id, overdue_id, delivered_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid_,))


def test_content_due_carries_overdue_across_period_rollover(admin, monkeypatch):
    """Overdue-rollover VISIBILITY fix (Domain G, read-only): an undelivered content
    slot from a PRIOR period must NOT vanish when the month rolls over — it stays on
    the Content-due strip as overdue until it's delivered. Encodes WHY: a shoot the
    studio still owes a client doesn't disappear just because the calendar turned;
    the old `substr(slot_date,1,7)=period` filter silently dropped it on the 1st of
    the next month. Date-WINDOWED so today is pinned. The companion guarantee — that
    FUTURE-period slots stay hidden (look-ahead is still period-bounded) — is asserted
    too, so the fix is carryover-only and didn't accidentally widen the window."""
    import datetime as _dt
    import types

    from app.admin import studio

    class _FixedDate(_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 15)

    monkeypatch.setattr(studio, "dt", types.SimpleNamespace(
        date=_FixedDate, datetime=_dt.datetime,
        timezone=_dt.timezone, timedelta=_dt.timedelta))

    def mk_plan(client_name, slot_date, status="planned"):
        admin.post("/admin/studio/clients",
                   data={"name": client_name, "company": client_name + " Co",
                         "email": "", "phone": ""}, follow_redirects=False)
        c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/clients/{c['id']}/projects",
                   data={"title": "Retainer"}, follow_redirects=False)
        proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
                   data={"title": client_name + " retainer"}, follow_redirects=False)
        plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
        admin.post(f"/admin/studio/recurring/{plan['id']}/calendar",
                   data={"slot_date": slot_date, "label": "Hero images"},
                   follow_redirects=False)
        if status != "planned":
            slot = db.one("SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                          (plan["id"],))
            admin.post(f"/admin/studio/recurring/{plan['id']}/calendar/{slot['id']}/status",
                       data={"status": status}, follow_redirects=False)
        return plan["id"]

    # carried over from LAST month (undelivered), this month, and NEXT month
    carry_id = mk_plan("Carryover Cafe", "2026-05-28")      # prior period, still owed
    current_id = mk_plan("Current Counter", "2026-06-25")   # this period
    future_id = mk_plan("Future Fry", "2026-07-10")         # next period (look-ahead)

    page = admin.get("/admin/studio/activity").text
    # the carried-over prior-period slot STAYS visible (this is the fix) and reads overdue
    assert f"/admin/studio/recurring/{carry_id}#calendar" in page
    # current-period slot still shows (unchanged behavior)
    assert f"/admin/studio/recurring/{current_id}#calendar" in page
    # future-period slot stays hidden — carryover didn't widen the look-ahead window
    assert f"/admin/studio/recurring/{future_id}#calendar" not in page

    # delivering the carried-over slot clears it from the strip (status leaves planned/shot)
    s = db.one("SELECT id FROM content_calendar WHERE plan_id=? ORDER BY id LIMIT 1",
               (carry_id,))
    admin.post(f"/admin/studio/recurring/{carry_id}/calendar/{s['id']}/status",
               data={"status": "delivered"}, follow_redirects=False)
    page = admin.get("/admin/studio/activity").text
    assert f"/admin/studio/recurring/{carry_id}#calendar" not in page

    # clean up: hard-delete the plans (cascades their calendar slots)
    for pid_ in (carry_id, current_id, future_id):
        db.run("DELETE FROM recurring_plans WHERE id=?", (pid_,))


def test_retainer_caption_pack(admin):
    """Domain G slice 6a — caption packs (MANUAL, no AI): storage + human workflow
    for caption deliverables, tracked against the quota via the EXISTING delivery log.
    Encodes WHY the decoupling spine extends to captions: creating/editing a caption,
    and advancing it draft→approved, all write NO retainer_deliveries row — the manual
    log stays the count's single source. Approving REUSES slice-4 assisted credit
    (label, qty=1, period) so the human credits in one click; only the /deliveries POST
    moves the count. Also: current-period filtering, CASCADE on hard plan delete, and
    400s on blank text / bad period shape. Date-independent: period from _period()."""
    from urllib.parse import parse_qs, urlparse

    from app.admin.recurring import _period

    admin.post("/admin/studio/clients",
               data={"name": "Caption Kitchen", "company": "Caption Co",
                     "email": "", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]

    period = _period()  # 'YYYY-MM'

    # create a caption → it renders this period; writes NO delivery row (decoupled)
    r = admin.post(f"/admin/studio/recurring/{pid}/captions",
                   data={"label": "Hero images", "body": "Golden hour pasta, fresh basil.",
                         "period": period}, follow_redirects=False)
    assert r.status_code == 303
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                 (pid,))
    assert cap["status"] == "draft"  # default
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Golden hour pasta, fresh basil." in page

    # edit the caption text → persists, still NO delivery row
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cap['id']}",
                   data={"label": "Hero images", "body": "Edited: smoked brisket, slaw."},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT body FROM retainer_captions WHERE id=?",
                  (cap["id"],))["body"] == "Edited: smoked brisket, slaw."
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0

    # a caption in another period is NOT shown on the current-period view
    admin.post(f"/admin/studio/recurring/{pid}/captions",
               data={"label": "Reels", "body": "Next-quarter teaser.",
                     "period": "2099-01"}, follow_redirects=False)
    page = admin.get(f"/admin/studio/recurring/{pid}").text
    assert "Next-quarter teaser." not in page

    # approve: draft→approved redirects with assisted-credit prefill; NO write here
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
                   data={"status": "approved"}, follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0  # approving did NOT auto-log a delivery

    # the prefill fires ONLY on the transition into approved, not on a re-save
    r2 = admin.post(f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
                    data={"status": "approved"}, follow_redirects=False)
    assert "credit_label" not in r2.headers["location"]

    # the human submit of the existing /deliveries route is the ONLY thing that counts
    admin.post(f"/admin/studio/recurring/{pid}/deliveries",
               data={"label": "Hero images", "qty": "1", "period": period},
               follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 1

    # bad inputs rejected, not coerced
    assert admin.post(f"/admin/studio/recurring/{pid}/captions",
                      data={"label": "Hero images", "body": "   ", "period": period},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/recurring/{pid}/captions",
                      data={"label": "Hero images", "body": "ok", "period": "nope"},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/recurring/{pid}/captions/{cap['id']}/status",
                      data={"status": "published"},
                      follow_redirects=False).status_code == 400

    # delete a caption → it leaves the list
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cap['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT COUNT(*) AS n FROM retainer_captions WHERE id=?",
                  (cap["id"],))["n"] == 0

    # CASCADE: a hard plan delete cascades the captions (FK ON DELETE CASCADE)
    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))
    assert db.one("SELECT COUNT(*) AS n FROM retainer_captions WHERE plan_id=?",
                  (pid,))["n"] == 0


def test_caption_ai_draft(admin, monkeypatch):
    """Domain G slice 6b — AI caption drafting via Odysseus (assisted, with
    provenance). The first AI-generated content in Mise, at maximum doctrine stress.
    The Odysseus mesh call is STUBBED — no live Odysseus / real model. Encodes WHY
    each guarantee holds: (a) a draft is a SUGGESTION — it populates body but leaves
    status='draft' and writes ZERO delivery rows (drafting can never both generate and
    credit); (b) provenance is recorded AND the verbatim AI draft is retained distinct
    from a later human edit (the draft→final pair is the dataset — losing it is the one
    thing this slice can't do); (c) a mesh failure leaves body/status untouched and
    writes nothing (no partial drafts); (d) Draft-with-AI never silently overwrites
    human words; (e) the slice-6a credit path is unchanged. Date-independent (period
    from _period())."""
    from urllib.parse import parse_qs, urlparse

    from app import caption_ai
    from app.admin.recurring import _period

    admin.post("/admin/studio/clients",
               data={"name": "AI Diner", "company": "AI Co",
                     "email": "", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]
    period = _period()

    # a caption seeded with HUMAN text
    admin.post(f"/admin/studio/recurring/{pid}/captions",
               data={"label": "Hero images", "body": "my own words", "period": period},
               follow_redirects=False)
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                 (pid,))
    cid = cap["id"]

    AI_TEXT = "Golden hour pasta, basil fresh off the pass. #avleats #fnbphoto"
    calls = []

    def fake_draft(ctx):
        calls.append(ctx)
        return {"caption": AI_TEXT, "model": "magistral:24b"}

    monkeypatch.setattr(caption_ai, "draft_caption", fake_draft)

    # (d) no-clobber: drafting over HUMAN body without replace is refused — the mesh
    # is never even called, and the human's words survive untouched
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    assert len(calls) == 0
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == "my own words" and row["ai_drafted"] == 0

    # (a) with explicit replace: the draft lands in body as a SUGGESTION — status
    # stays draft, provenance recorded, ZERO delivery rows (never generates+credits)
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={"replace": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "caption_error" not in r.headers["location"]
    assert len(calls) == 1
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT
    assert row["status"] == "draft"            # generation NEVER approves
    assert row["ai_drafted"] == 1
    assert row["ai_model"] == "magistral:24b"  # model as reported by Odysseus
    assert row["ai_drafted_at"]                # drafted-at timestamp recorded
    assert row["ai_draft_original"] == AI_TEXT
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0            # drafting never moves the count

    # (b) a human edits the draft → body changes but the ORIGINAL is still recoverable
    admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}",
               data={"label": "Hero images", "body": AI_TEXT + " — tightened by hand"},
               follow_redirects=False)
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT + " — tightened by hand"
    assert row["ai_draft_original"] == AI_TEXT   # the (draft → final) diff survives
    assert row["ai_drafted"] == 1

    # post-edit, body is human-edited again → a re-draft without replace is refused
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={}, follow_redirects=False)
    assert "caption_error" in r.headers["location"]

    # (c) a stubbed mesh FAILURE writes nothing and leaves body/status/original intact
    def fake_fail(ctx):
        raise caption_ai.CaptionDraftError("Odysseus unreachable: timed out")

    monkeypatch.setattr(caption_ai, "draft_caption", fake_fail)
    before = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={"replace": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    after = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["ai_draft_original"] == before["ai_draft_original"]  # never wiped

    # (e) the slice-6a credit path is unchanged: human approve→delivered carries the
    # prefill with 0 writes; only the /deliveries POST moves the count
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/status",
                   data={"status": "approved"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["credit_label"] == ["Hero images"]
    assert q["credit_qty"] == ["1"]
    assert q["credit_period"] == [period]
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0
    admin.post(f"/admin/studio/recurring/{pid}/deliveries",
               data={"label": "Hero images", "qty": "1", "period": period},
               follow_redirects=False)
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 1

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


def test_caption_ai_live_wiring(admin, monkeypatch):
    """Domain G slice 6c — wire draft_caption to the LIVE Odysseus endpoint. Unlike 6b's
    test, this stubs the NETWORK seam (urllib.request.urlopen), not the whole function,
    so the real wiring is exercised: the bearer header, the configured URL, the body Mise
    builds, the JSON round-trip, and the 210s>180s timeout. Encodes WHY each guarantee
    holds: (a) the outbound request carries Authorization: Bearer <token> and hits the
    configured URL with the built body, at the deployed timeout (above the endpoint's
    budget so the ENDPOINT decides failure, not the client); (b) a 200 lands as a
    SUGGESTION — body populated, status still 'draft', 0 delivery rows, and the SERVED
    model string (not a static label) persisted as provenance; (c) an HTTP 502/401/400
    raises CaptionDraftError and writes nothing (no partial drafts); (d) no-clobber
    refuses over human body without replace and the network is NEVER touched; (e) with
    URL/token unset, is_enabled() is False and draft_caption raises with no network call.
    Date-independent (period from _period())."""
    import json as _json
    import urllib.error

    from app import caption_ai, config
    from app.admin.recurring import _period

    URL = "http://mickey:7010/draft/caption"
    TOKEN = "stub-bearer-do-not-log"
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_URL", URL)
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", TOKEN)
    # deployed default timeout is deliberately above the endpoint's ~180s budget
    assert config.ODYSSEUS_TIMEOUT > 180

    # (e) not configured -> is_enabled False and draft_caption raises WITHOUT a call
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", "")
    assert caption_ai.is_enabled() is False
    fired = []
    monkeypatch.setattr(caption_ai.urllib.request, "urlopen",
                        lambda *a, **k: fired.append(1))
    try:
        caption_ai.draft_caption({"label": "x"})
        assert False, "expected CaptionDraftError when not configured"
    except caption_ai.CaptionDraftError:
        pass
    assert fired == []
    monkeypatch.setattr(config, "ODYSSEUS_CAPTION_TOKEN", TOKEN)
    assert caption_ai.is_enabled() is True

    SERVED = "qwen3.5:122b"   # served truth from Odysseus's echo, not a static route label
    AI_TEXT = "Backlit espresso, crema like silk. #avleats #fnbphoto"
    captured: dict = {}

    class _Resp:
        def __init__(self, payload): self._b = _json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b

    def fake_ok(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = _json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Resp({"caption": AI_TEXT, "model": SERVED})

    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_ok)

    # (a) direct call: outbound carries the bearer, hits the configured URL, sends the
    # built body, and uses the deployed (>180s) timeout
    out = caption_ai.draft_caption({"label": "Hero", "client": "Wire Co", "period": "x"})
    assert out == {"caption": AI_TEXT, "model": SERVED}
    assert captured["url"] == URL
    assert captured["auth"] == f"Bearer {TOKEN}"
    assert captured["body"]["label"] == "Hero" and captured["body"]["client"] == "Wire Co"
    assert captured["timeout"] == config.ODYSSEUS_TIMEOUT

    admin.post("/admin/studio/clients",
               data={"name": "Wire Diner", "company": "Wire Co",
                     "email": "", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "Retainer"}, follow_redirects=False)
    proj = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/projects/{proj['id']}/recurring",
               data={"title": "Content retainer"}, follow_redirects=False)
    plan = db.one("SELECT * FROM recurring_plans ORDER BY id DESC LIMIT 1")
    pid = plan["id"]
    period = _period()
    admin.post(f"/admin/studio/recurring/{pid}/captions",
               data={"label": "Hero images", "body": "placeholder", "period": period},
               follow_redirects=False)
    cap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? ORDER BY id DESC LIMIT 1",
                 (pid,))
    cid = cap["id"]

    # (b) through the route, a stubbed 200 lands as a SUGGESTION: body populated, status
    # still draft, provenance = the SERVED model, and ZERO delivery rows written
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={"replace": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "caption_error" not in r.headers["location"]
    row = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert row["body"] == AI_TEXT
    assert row["status"] == "draft"
    assert row["ai_drafted"] == 1
    assert row["ai_model"] == SERVED
    assert row["ai_draft_original"] == AI_TEXT
    assert db.one("SELECT COUNT(*) AS n FROM retainer_deliveries WHERE plan_id=?",
                  (pid,))["n"] == 0

    # (d) no-clobber: a caption with HUMAN body refuses a no-replace draft, and the
    # network seam is NEVER touched
    admin.post(f"/admin/studio/recurring/{pid}/captions",
               data={"label": "Interiors", "body": "chef's own caption", "period": period},
               follow_redirects=False)
    hcap = db.one("SELECT * FROM retainer_captions WHERE plan_id=? AND label='Interiors' "
                  "ORDER BY id DESC LIMIT 1", (pid,))
    tripped = []
    monkeypatch.setattr(caption_ai.urllib.request, "urlopen",
                        lambda *a, **k: tripped.append(1))
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{hcap['id']}/draft",
                   data={}, follow_redirects=False)
    assert "caption_error" in r.headers["location"]
    assert tripped == []
    assert db.one("SELECT body FROM retainer_captions WHERE id=?",
                  (hcap["id"],))["body"] == "chef's own caption"

    # (c) a stubbed HTTP 502 raises CaptionDraftError through the route — nothing written
    before = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))

    def fake_502(req, timeout=None):
        raise urllib.error.HTTPError(URL, 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_502)
    r = admin.post(f"/admin/studio/recurring/{pid}/captions/{cid}/draft",
                   data={"replace": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "caption_error" in r.headers["location"]
    after = db.one("SELECT * FROM retainer_captions WHERE id=?", (cid,))
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["ai_draft_original"] == before["ai_draft_original"]

    # 401 and 400 also surface as a clean CaptionDraftError (no partial draft)
    for code in (401, 400):
        def fake_err(req, timeout=None, _c=code):
            raise urllib.error.HTTPError(URL, _c, "err", {}, None)
        monkeypatch.setattr(caption_ai.urllib.request, "urlopen", fake_err)
        try:
            caption_ai.draft_caption({"label": "x"})
            assert False, f"expected CaptionDraftError on HTTP {code}"
        except caption_ai.CaptionDraftError:
            pass

    db.run("DELETE FROM recurring_plans WHERE id=?", (pid,))


# ── Domain H slice 1: press / published-work tracking ──────────────────────

def test_press_outlet_only_and_audit(admin):
    """(a) A press hit can exist with ALL linkage FKs null — outlet is the only
    required anchor (own-brand / editorial press has no client). Create writes one
    audit row (entity_type='press'), matching the licenses rigor."""
    r = admin.post("/admin/studio/press",
                   data={"outlet": "Garden & Gun"}, follow_redirects=False)
    assert r.status_code == 303
    p = db.one("SELECT * FROM press ORDER BY id DESC LIMIT 1")
    assert p["outlet"] == "Garden & Gun"
    assert p["client_id"] is None and p["project_id"] is None
    assert p["gallery_id"] is None and p["asset_id"] is None
    assert p["publish_date"] is None        # pending until a date is set
    created = db.all_("""SELECT * FROM audit_log WHERE entity_type='press'
                         AND entity_id=? AND action='create'""", (p["id"],))
    assert len(created) == 1
    # list page renders the row (Jinja autoescapes the ampersand)
    assert "Garden &amp; Gun" in admin.get("/admin/studio/press").text


def test_press_publish_date_is_the_gate(admin):
    """(b) publish_date NULL = pending; populated + past = published. The E gate
    (publish_date IS NOT NULL AND publish_date <= today) selects the past row and
    EXCLUDES a future-dated one. Dates are relative to date('now') so the gate
    cannot flake by calendar day."""
    import datetime as _dt
    admin.post("/admin/studio/press", data={"outlet": "Pending Mag"},
               follow_redirects=False)
    pending = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # past-dated = published
    admin.post("/admin/studio/press",
               data={"outlet": "Past Times", "publish_date": "2020-01-15"},
               follow_redirects=False)
    past = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # future-dated = announced but not yet out → must be excluded by the gate
    future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    admin.post("/admin/studio/press",
               data={"outlet": "Future Weekly", "publish_date": future},
               follow_redirects=False)
    fut = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]

    gated = {r["id"] for r in db.all_(
        """SELECT id FROM press WHERE deleted_at IS NULL
           AND publish_date IS NOT NULL
           AND publish_date <= date('now', 'localtime')""")}
    assert past in gated            # published
    assert pending not in gated     # no date = pending
    assert fut not in gated         # future date = not yet out


def test_press_for_license_seam(admin):
    """(c) press_for_license joins published press to a license on linkage +
    channel overlap, returns ONLY gated rows, and writes NOTHING to
    licenses.published (suggestion only — the human owns the flag)."""
    import datetime as _dt
    from app.admin.press import press_for_license

    admin.post("/admin/studio/clients",
               data={"name": "Press Chef", "company": "Seam Bistro"},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    gid = db.run("INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
                 (c["id"], "Seam Gallery", "seamgal12345", "0000"))
    # a license on that gallery granting the 'print' channel, published flag OFF
    admin.post(f"/admin/studio/clients/{c['id']}/licenses",
               data={"title": "Seam license"}, follow_redirects=False)
    lic_id = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    db.run("""UPDATE licenses SET gallery_id=?, channels='["print"]', published=0
              WHERE id=?""", (gid, lic_id))
    lic = db.one("SELECT * FROM licenses WHERE id=?", (lic_id,))

    # published press on that gallery, channel overlaps the license grant
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (gid, "Print Mag", "print", "2021-06-01"))
    hit = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # a future-dated press on the same gallery — must be gated out
    fut = (_dt.date.today() + _dt.timedelta(days=20)).isoformat()
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (gid, "Soon Mag", "print", fut))

    rows = press_for_license(lic)
    ids = {r["id"] for r in rows}
    assert hit in ids                                   # gated + linked → returned
    assert all(r["publish_date"] <= _dt.date.today().isoformat() for r in rows)
    assert {r["id"] for r in rows if r["outlet"] == "Soon Mag"} == set()  # future excluded
    assert next(r for r in rows if r["id"] == hit)["channel_overlap"] is True
    # the seam is READ-ONLY: it never flipped the license's published bit
    assert db.one("SELECT published FROM licenses WHERE id=?", (lic_id,))["published"] == 0


def test_press_set_null_on_linked_delete(admin):
    """(d) ON DELETE SET NULL: deleting a linked client leaves the press row in
    place with its client_id nulled — a press hit outlives the entity it referenced
    (press is reference data)."""
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Doomed Client",))
    db.run("INSERT INTO press (client_id, outlet) VALUES (?,?)", (cid, "Standalone Press"))
    pid = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    assert db.one("SELECT client_id FROM press WHERE id=?", (pid,))["client_id"] == cid
    db.run("DELETE FROM clients WHERE id=?", (cid,))    # FK pragma ON → SET NULL fires
    row = db.one("SELECT * FROM press WHERE id=?", (pid,))
    assert row is not None and row["client_id"] is None
    assert row["outlet"] == "Standalone Press"          # row survived


def test_channels_extraction_no_regression(admin):
    """(e) The CHANNELS extraction is pure: licenses.py and press.py share the one
    list from app.usage_vocab, with identical values + order. licenses behaviour is
    unchanged — a license still persists a valid channel selection."""
    import json as _json
    from app.usage_vocab import CHANNELS as VOCAB
    from app.admin.licenses import CHANNELS as LIC_CHANNELS
    from app.admin.press import CHANNELS as PRESS_CHANNELS

    expected = ["website", "social_organic", "social_paid", "ooh_billboard",
                "print", "pr_editorial", "delivery_apps", "menu", "email", "broadcast"]
    assert VOCAB == expected                # values + order frozen
    assert LIC_CHANNELS is VOCAB and PRESS_CHANNELS is VOCAB   # one source object

    # licenses still validate + store channels exactly as before the move
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Vocab Chef",))
    admin.post(f"/admin/studio/clients/{cid}/licenses",
               data={"title": "Vocab license"}, follow_redirects=False)
    lid = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    admin.post(f"/admin/studio/licenses/{lid}",
               data={"title": "Vocab license", "channels": ["print", "bogus_channel"]},
               follow_redirects=False)
    stored = _json.loads(db.one("SELECT channels FROM licenses WHERE id=?", (lid,))["channels"])
    assert stored == ["print"]              # valid kept, bogus dropped — unchanged behaviour


def test_press_validation_400s(admin):
    """(f) 400s on blank outlet / bad publish_date / channel outside CHANNELS."""
    assert admin.post("/admin/studio/press",
                      data={"outlet": "   "}, follow_redirects=False).status_code == 400
    assert admin.post("/admin/studio/press",
                      data={"outlet": "OK Mag", "publish_date": "not-a-date"},
                      follow_redirects=False).status_code == 400
    assert admin.post("/admin/studio/press",
                      data={"outlet": "OK Mag", "channel": "tiktok_dance"},
                      follow_redirects=False).status_code == 400


def _seam_license_with_gallery(admin, name, company, slug):
    """Build a client + gallery + a license on that gallery (published OFF,
    'print' channel granted) — the linkage the H3 render seam keys off."""
    admin.post("/admin/studio/clients",
               data={"name": name, "company": company}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    gid = db.run("INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
                 (c["id"], f"{name} Gallery", slug, "0000"))
    admin.post(f"/admin/studio/clients/{c['id']}/licenses",
               data={"title": f"{name} license"}, follow_redirects=False)
    lic_id = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    db.run("""UPDATE licenses SET gallery_id=?, channels='["print"]', published=0
              WHERE id=?""", (gid, lic_id))
    return c, gid, lic_id


def test_press_evidence_renders_with_cue(admin):
    """(a) A license with matching published press shows the read-only 'Press
    evidence' section AND the review-published cue near the published control
    (cue only fires while published is OFF — the human hasn't confirmed yet)."""
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Evidence Chef", "Cue Bistro", "h3evidence123")
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date, url)
              VALUES (?,?,?,?,?)""",
           (gid, "Bon Appetit", "print", "2021-03-01", "https://example.com/run"))
    body = admin.get(f"/admin/studio/licenses/{lic_id}").text
    assert "Press evidence" in body
    assert "Bon Appetit" in body
    assert "review the evidence below and confirm published" in body
    assert "https://example.com/run" in body
    assert "granted" in body                       # channel_overlap annotation


def test_press_evidence_silent_when_no_match(admin):
    """(b) A license with no matching press renders silent — no 'Press evidence'
    section, no cue, no error. Matches the silent-when-empty idiom."""
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Quiet Chef", "Silent Bistro", "h3silent1234")
    # press exists but links to a DIFFERENT, unrelated gallery → no overlap
    other = db.run("INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
                   (c["id"], "Other Gallery", "h3other12345", "0000"))
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (other, "Unrelated Weekly", "print", "2021-03-01"))
    r = admin.get(f"/admin/studio/licenses/{lic_id}")
    assert r.status_code == 200
    assert "Press evidence" not in r.text
    assert "review the evidence below" not in r.text


def test_press_evidence_gate_holds_at_render(admin):
    """(c) Future-dated and unlinked press never reach the render — the gate is
    re-pinned at the display layer, not just in the seam unit test. A future-dated
    hit on the SAME gallery and a past-dated hit on an UNRELATED gallery are both
    absent; only the linked, past-dated one shows."""
    import datetime as _dt
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Gate Chef", "Gate Bistro", "h3gate123456")
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (gid, "Shown Past Mag", "print", "2020-02-02"))
    future = (_dt.date.today() + _dt.timedelta(days=25)).isoformat()
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (gid, "Hidden Future Mag", "print", future))
    other = db.run("INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
                   (c["id"], "Unlinked Gallery", "h3unlinked12", "0000"))
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (other, "Hidden Unlinked Mag", "print", "2020-02-02"))
    body = admin.get(f"/admin/studio/licenses/{lic_id}").text
    assert "Shown Past Mag" in body
    assert "Hidden Future Mag" not in body         # future-dated gated out
    assert "Hidden Unlinked Mag" not in body       # unlinked never matches


def test_press_evidence_render_writes_nothing(admin):
    """(d) Viewing the detail page is read-only: rendering the evidence performs
    ZERO writes to licenses.published, and once published IS set the suggestion
    cue stops (evidence still shows, but the 'confirm published' nudge is gone —
    the flip stays the existing human control's job)."""
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Readonly Chef", "NoWrite Bistro", "h3readonly12")
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (gid, "Evidence Times", "print", "2021-01-01"))
    url = f"/admin/studio/licenses/{lic_id}"

    # GET the page several times — published must remain 0 (no auto-flip on view)
    for _ in range(3):
        assert admin.get(url).status_code == 200
    assert db.one("SELECT published FROM licenses WHERE id=?", (lic_id,))["published"] == 0
    # the seam never wrote an audit row either (read-only, no mutation)
    assert db.all_("""SELECT 1 FROM audit_log WHERE entity_type='press'
                      AND action IN ('update','status_change')""") == []

    # human confirms via the EXISTING control → published flips, cue disappears
    db.run("UPDATE licenses SET published=1 WHERE id=?", (lic_id,))
    body = admin.get(url).text
    assert "Press evidence" in body and "Evidence Times" in body   # evidence still shown
    assert "review the evidence below and confirm published" not in body  # cue gone


def test_press_confirm_strip_rolls_up_h3_cue(admin):
    """Domain H, H2 — the studio dashboard 'Press evidence — confirm published'
    strip rolls up H3's per-license cue: an ACTIVE license with matching published
    press but published=0 surfaces (chip links to its detail, where the evidence +
    Published checkbox live). It honors the same conditions as the H3 cue —
    confirmed licenses drop off, no-match licenses stay silent — and is scoped to
    active grants (the locked decision: draft/expired/terminated stay quiet).
    Read-only: rendering the strip never flips published."""
    # (1) active + unpublished + matching published press → ON the strip
    c1, g1, on_id = _seam_license_with_gallery(admin, "H2 On Chef", "On Bistro", "h2on1234567")
    db.run("UPDATE licenses SET status='active' WHERE id=?", (on_id,))
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (g1, "Eater", "print", "2021-05-01"))
    # (2) active + unpublished + NO matching press → off the strip
    c2, g2, none_id = _seam_license_with_gallery(admin, "H2 None Chef", "None Bistro", "h2none12345")
    db.run("UPDATE licenses SET status='active' WHERE id=?", (none_id,))
    # (3) already-confirmed (published=1) + matching press → off the strip (cue gone)
    c3, g3, done_id = _seam_license_with_gallery(admin, "H2 Done Chef", "Done Bistro", "h2done12345")
    db.run("UPDATE licenses SET status='active', published=1 WHERE id=?", (done_id,))
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (g3, "Garden & Gun", "print", "2021-05-01"))
    # (4) draft (status != active) + unpublished + matching press → off (active-only)
    c4, g4, draft_id = _seam_license_with_gallery(admin, "H2 Draft Chef", "Draft Bistro", "h2draft1234")
    db.run("UPDATE licenses SET status='draft' WHERE id=?", (draft_id,))
    db.run("""INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""", (g4, "Local Weekly", "print", "2021-05-01"))

    body = admin.get("/admin/studio/activity").text
    assert "Press evidence — confirm published" in body          # strip rendered
    assert f"/admin/studio/licenses/{on_id}" in body             # the actionable one
    assert f"/admin/studio/licenses/{none_id}" not in body       # no evidence → silent
    assert f"/admin/studio/licenses/{done_id}" not in body       # confirmed → dropped
    assert f"/admin/studio/licenses/{draft_id}" not in body      # not active → quiet

    # read-only: repeated renders never flip the matched license's published bit,
    # and the strip writes no audit row against it.
    for _ in range(3):
        assert admin.get("/admin/studio/activity").status_code == 200
    assert db.one("SELECT published FROM licenses WHERE id=?", (on_id,))["published"] == 0
    assert db.all_("""SELECT 1 FROM audit_log WHERE entity_type='license'
                      AND entity_id=? AND action IN ('update','status_change')""",
                   (on_id,)) == []


# ── Domain H slice 4: public "As seen in" surface ──────────────────────────

def test_press_show_on_site_flag_roundtrips_and_audits(admin):
    """The admin press form's 'Feature on public site' checkbox round-trips to the
    show_on_site column (checked=1, absent=0) and the toggle is captured in the
    press audit trail — public visibility is an auditable human act, default off."""
    # checkbox present → 1
    admin.post("/admin/studio/press",
               data={"outlet": "Bon Appétit", "publish_date": "2021-03-01",
                     "show_on_site": "1"}, follow_redirects=False)
    pid = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid,))["show_on_site"] == 1
    # checkbox absent → 0 (nothing leaks unless explicitly toggled)
    admin.post("/admin/studio/press",
               data={"outlet": "Private Trade Rag", "publish_date": "2021-03-01"},
               follow_redirects=False)
    pid2 = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid2,))["show_on_site"] == 0
    # un-featuring later (checkbox now absent) flips it back to 0 and is audited
    admin.post(f"/admin/studio/press/{pid}",
               data={"outlet": "Bon Appétit", "publish_date": "2021-03-01"},
               follow_redirects=False)
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid,))["show_on_site"] == 0
    rows = db.all_("""SELECT diff_json FROM audit_log WHERE entity_type='press'
                      AND entity_id=? AND action='update'""", (pid,))
    assert any("show_on_site" in (r["diff_json"] or "") for r in rows)


def test_press_public_surface_gates_and_dedups(client):
    """The public /press page + home strip render ONLY press that is featured
    (show_on_site=1) AND published (publish_date populated and not in the future),
    deduped by outlet. The default-off flag plus the publish_date gate keep
    internal / confidential / pending press off the open internet."""
    # featured + published + past → public; two pieces from one outlet → deduped
    db.run("""INSERT INTO press (outlet, title, url, publish_date, show_on_site)
              VALUES (?,?,?,?,1)""",
           ("The Local Spoon", "Older piece", "https://spoon.example/a", "2020-01-01"))
    db.run("""INSERT INTO press (outlet, title, url, publish_date, show_on_site)
              VALUES (?,?,?,?,1)""",
           ("The Local Spoon", "Newest piece", "https://spoon.example/b", "2023-06-01"))
    # featured but NOT yet published (pending) → hidden
    db.run("""INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,1)""", ("Pending Public Mag", None))
    # featured but future-dated → hidden until it's actually out
    db.run("""INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,1)""", ("Future Public Mag", "2099-01-01"))
    # published+past but NOT featured (default 0) → stays internal
    db.run("""INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,0)""", ("Confidential Trade Rag", "2020-01-01"))

    body = client.get("/press").text
    assert "The Local Spoon" in body                     # featured + published
    assert "Pending Public Mag" not in body              # pending → gated
    assert "Future Public Mag" not in body               # future → gated
    assert "Confidential Trade Rag" not in body          # not featured → internal
    # deduped: outlet appears once, and the link points at the NEWEST piece
    assert body.count("The Local Spoon") == 1
    assert "https://spoon.example/b" in body             # newest wins the link
    assert "https://spoon.example/a" not in body         # older piece dropped
    # link-out is hardened against tab-nabbing
    assert 'rel="noopener noreferrer"' in body
    # home strip surfaces the same featured outlet and links to the full page
    home = client.get("/").text
    assert "As seen in" in home
    assert "The Local Spoon" in home
    assert 'href="/press"' in home


def test_press_page_is_indexable(client):
    """/press is a marketing surface meant to be crawled — it must NOT carry the
    noindex header that every non-marketing route gets, and must be in the sitemap."""
    r = client.get("/press")
    assert r.status_code == 200
    assert "x-robots-tag" not in r.headers
    assert "/press" in client.get("/sitemap.xml").text


def test_shot_list_crud_and_audit(admin):
    """Domain F slice 1: Mise-local shot list per project (studio-only). Covers the
    full CRUD contract that proves WHY each piece matters:
      - create writes the row + exactly one audit row (entity_type='shot_list'),
        priority defaulting to 'want' when the form omits it;
      - bad category and bad priority are rejected 400 (app-level vocab gate — the
        reason there's no SQL CHECK, so this test IS the guard);
      - update records a diff audit row and changes only what moved;
      - soft-delete sets deleted_at and the shot vanishes from the project page
        (and from the inline query) without a hard delete;
      - the shot renders on its owning project page.
    Unique strings throughout — the module-scoped DB is shared across tests."""
    # own client + project so the test is self-contained
    admin.post("/admin/studio/clients",
               data={"name": "Shotlist Tester", "company": "Mise Test Kitchen FBQ",
                     "email": "shotfbq@example.com", "phone": ""},
               follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "FBQ shoot production project"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — priority omitted defaults to 'want'
    r = admin.post(f"/admin/studio/projects/{p['id']}/shots",
                   data={"title": "Plated hero FBQ three-quarter",
                         "category": "Hero Dish", "sort_order": "5"},
                   follow_redirects=False)
    assert r.status_code == 303
    s = db.one("SELECT * FROM shot_list ORDER BY id DESC LIMIT 1")
    assert s["title"] == "Plated hero FBQ three-quarter"
    assert s["category"] == "Hero Dish" and s["priority"] == "want"
    assert s["project_id"] == p["id"] and s["deleted_at"] is None
    created = db.all_("""SELECT * FROM audit_log WHERE entity_type='shot_list'
                         AND entity_id=? AND action='create'""", (s["id"],))
    assert len(created) == 1

    # vocab gate — bad category and bad priority both 400 (no row written)
    assert admin.post(f"/admin/studio/projects/{p['id']}/shots",
                      data={"title": "x", "category": "NotARealCategory"},
                      follow_redirects=False).status_code == 400
    assert admin.post(f"/admin/studio/projects/{p['id']}/shots",
                      data={"title": "x", "priority": "urgent"},
                      follow_redirects=False).status_code == 400
    # title required
    assert admin.post(f"/admin/studio/projects/{p['id']}/shots",
                      data={"title": "   "}, follow_redirects=False).status_code == 400

    # update — change priority + note; diff audit row written
    r = admin.post(f"/admin/studio/shots/{s['id']}",
                   data={"title": s["title"], "category": "Hero Dish",
                         "priority": "must", "sort_order": "5",
                         "note": "shoot first FBQ"}, follow_redirects=False)
    assert r.status_code == 303
    s2 = db.one("SELECT * FROM shot_list WHERE id=?", (s["id"],))
    assert s2["priority"] == "must" and s2["note"] == "shoot first FBQ"
    upd = db.all_("""SELECT * FROM audit_log WHERE entity_type='shot_list'
                     AND entity_id=? AND action='update'""", (s["id"],))
    assert len(upd) == 1

    # renders on the project page
    assert "Plated hero FBQ three-quarter" in admin.get(
        f"/admin/studio/projects/{p['id']}").text

    # soft-delete — deleted_at set, vanishes from page and inline query
    r = admin.post(f"/admin/studio/shots/{s['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT deleted_at FROM shot_list WHERE id=?", (s["id"],))["deleted_at"]
    assert "Plated hero FBQ three-quarter" not in admin.get(
        f"/admin/studio/projects/{p['id']}").text
    assert db.one("""SELECT COUNT(*) n FROM shot_list
                     WHERE project_id=? AND deleted_at IS NULL""", (p["id"],))["n"] == 0


def test_shots_read_api(admin):
    """Domain F / B-Direct: the inbound /api/shots read surface Odysseus's
    preshoot_pack calls. Proves the WHY of each rule:
      - disarmed (SHOTS_TOKEN unset) -> 503, NOT 401: the route ships dormant on flow
        and a 503 reads as 'not turned on yet', distinct from a real auth failure;
      - missing/bad bearer -> 401 once armed (secrets.compare_digest gate);
      - good bearer + a session mapped via projects.notion_page_id -> matched True with
        only non-deleted shots, in (sort_order, id) order, title/category/priority only;
      - a soft-deleted shot is excluded (preshoot_pack must not see removed shots);
      - an unmatched session -> matched False empty (caller falls back to Notion, not an error);
      - blank session -> 400.
    config.SHOTS_TOKEN is mutated then restored — it is read live in require_shots_token."""
    from app import config

    sess = "notion-page-fbq-readapi-001"
    admin.post("/admin/studio/clients",
               data={"name": "ReadAPI Tester", "company": "Mise Test Kitchen RAPI",
                     "email": "rapi@example.com", "phone": ""}, follow_redirects=False)
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(f"/admin/studio/clients/{c['id']}/projects",
               data={"title": "ReadAPI shoot project"}, follow_redirects=False)
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    db.run("UPDATE projects SET notion_page_id=? WHERE id=?", (sess, p["id"]))

    # two shots out of insert order + one soft-deleted, to prove ordering + exclusion
    admin.post(f"/admin/studio/projects/{p['id']}/shots",
               data={"title": "RAPI second", "category": "Detail",
                     "priority": "want", "sort_order": "20"}, follow_redirects=False)
    admin.post(f"/admin/studio/projects/{p['id']}/shots",
               data={"title": "RAPI first", "category": "Hero Dish",
                     "priority": "must", "sort_order": "10"}, follow_redirects=False)
    admin.post(f"/admin/studio/projects/{p['id']}/shots",
               data={"title": "RAPI gone", "priority": "if-time",
                     "sort_order": "5"}, follow_redirects=False)
    gone = db.one("SELECT id FROM shot_list WHERE title='RAPI gone'")
    admin.post(f"/admin/studio/shots/{gone['id']}/delete", follow_redirects=False)

    url = f"/api/shots?session={sess}"
    saved = config.SHOTS_TOKEN
    try:
        # disarmed -> 503 even with a bearer present
        config.SHOTS_TOKEN = ""
        assert admin.get(url, headers={"Authorization": "Bearer anything"}
                         ).status_code == 503

        # armed
        config.SHOTS_TOKEN = "rapi-secret-token"
        bearer = {"Authorization": "Bearer rapi-secret-token"}

        assert admin.get(url).status_code == 401              # no header
        assert admin.get(url, headers={"Authorization": "Bearer wrong"}
                         ).status_code == 401                 # wrong token

        r = admin.get(url, headers=bearer)
        assert r.status_code == 200
        body = r.json()
        assert body["matched"] is True and body["project_id"] == p["id"]
        titles = [s["title"] for s in body["shots"]]
        assert titles == ["RAPI first", "RAPI second"]        # sort_order order, gone excluded
        assert body["shots"][0] == {"title": "RAPI first", "category": "Hero Dish",
                                    "priority": "must"}

        # unmatched session -> matched False, not an error
        miss = admin.get("/api/shots?session=no-such-page", headers=bearer)
        assert miss.status_code == 200
        assert miss.json() == {"matched": False, "session": "no-such-page", "shots": []}

        # blank session -> 400
        assert admin.get("/api/shots?session=", headers=bearer).status_code == 400
    finally:
        config.SHOTS_TOKEN = saved


def test_inbox_reply_sends_logs_and_marks_emailed(admin):
    """Inbox reply: manual Gmail send → emails_log row → inquiry marked emailed.

    The send is the whole point (a real reply, not a mailto bounce), so we assert
    the mailer was called with the inquiry's address and that the audit row +
    unread-clearing flag both land. mailer.send is patched so no SMTP happens."""
    from unittest import mock
    from app import mailer

    iid = db.run("INSERT INTO inquiries (name, email, business, message, kind) "
                 "VALUES (?,?,?,?,?)",
                 ("Ana Diaz", "ana@bistro.test", "Bistro Verde",
                  "Need a full menu shoot in July.", "contact"))

    with mock.patch.object(mailer, "configured", return_value=True), \
         mock.patch.object(mailer, "send") as send:
        r = admin.post(f"/admin/inbox/{iid}/reply",
                       data={"tab": "all", "subject": "Re: your inquiry",
                             "message": "Hi Ana — happy to help, sending a quote."},
                       follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/inbox?tab=all&sel={iid}"
    send.assert_called_once()
    to, subject, body = send.call_args.args[:3]
    assert to == "ana@bistro.test"
    assert "happy to help" in body
    assert db.one("SELECT emailed FROM inquiries WHERE id=?", (iid,))["emailed"] == 1
    logged = db.one("SELECT doc_kind, doc_id, to_email FROM emails_log "
                    "WHERE to_email='ana@bistro.test'")
    assert logged is not None
    assert logged["doc_kind"] == "other" and logged["doc_id"] == iid


def test_inbox_reply_blocked_without_email(admin):
    """An inquiry with no email address can't be replied to — 400, no send."""
    from unittest import mock
    from app import mailer

    iid = db.run("INSERT INTO inquiries (name, email, business, message, kind) "
                 "VALUES (?,?,?,?,?)", ("No Email", "", "Ghost Cafe", "hi", "contact"))
    # email is NOT NULL in schema; an empty string is the realistic "missing" case
    with mock.patch.object(mailer, "configured", return_value=True), \
         mock.patch.object(mailer, "send") as send:
        r = admin.post(f"/admin/inbox/{iid}/reply",
                       data={"subject": "Re", "message": "x"},
                       follow_redirects=False)
    assert r.status_code == 400
    send.assert_not_called()


def test_expense_create_and_delete(admin):
    """Expenses are real CRUD over operator-entered data: the row persists with
    cents parsed from a dollar string, and deductible math is honest."""
    r = admin.post("/admin/financials/expenses",
                   data={"spent_on": "2026-06-15", "vendor": "B&H Photo",
                         "category": "Equipment", "amount": "1,240.00",
                         "deductible_pct": "100", "notes": "85mm lens"},
                   follow_redirects=False)
    assert r.status_code == 303
    row = db.one("SELECT * FROM expenses WHERE vendor='B&H Photo'")
    assert row is not None and row["amount_cents"] == 124000
    assert row["category"] == "Equipment" and row["deductible_pct"] == 100

    page = admin.get("/admin/financials/expenses")
    assert page.status_code == 200 and "Expense log" in page.text

    r = admin.post(f"/admin/financials/expenses/{row['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM expenses WHERE id=?", (row["id"],)) is None


def test_expense_rejects_bad_amount(admin):
    r = admin.post("/admin/financials/expenses",
                   data={"spent_on": "2026-06-15", "vendor": "Junk",
                         "amount": "not-a-number"}, follow_redirects=False)
    assert r.status_code == 400


def test_receipt_upload_links_and_serves(admin):
    """A receipt scan uploads to disk, links to an expense, serves back its bytes,
    and flags the expense as having a receipt — no auto-matching, the link is explicit."""
    eid = db.run("INSERT INTO expenses (spent_on, vendor, category, amount_cents) "
                 "VALUES (?,?,?,?)", ("2026-06-12", "Adobe", "Software", 5999))
    png = _logo_png()
    r = admin.post("/admin/financials/receipts",
                   files={"file": ("adobe.png", io.BytesIO(png), "image/png")},
                   data={"expense_id": str(eid)}, follow_redirects=False)
    assert r.status_code == 303
    rc = db.one("SELECT * FROM receipts WHERE expense_id=?", (eid,))
    assert rc is not None and rc["filename"] == "adobe.png"

    served = admin.get(f"/admin/financials/receipts/{rc['id']}/file")
    assert served.status_code == 200 and served.content == png

    # the linked expense now shows a receipt pill
    page = admin.get("/admin/financials/expenses")
    assert "Receipt" in page.text

    # deleting the expense leaves the receipt (unlinked), not destroyed
    admin.post(f"/admin/financials/expenses/{eid}/delete", follow_redirects=False)
    still = db.one("SELECT expense_id FROM receipts WHERE id=?", (rc["id"],))
    assert still is not None and still["expense_id"] is None


def test_receipt_rejects_non_document(admin):
    r = admin.post("/admin/financials/receipts",
                   files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
                   follow_redirects=False)
    assert r.status_code == 400


def test_mileage_create_deduction_and_delete(admin):
    """Mileage is real CRUD; the deduction is miles × the IRS rate frozen per trip."""
    from app import config
    r = admin.post("/admin/financials/mileage",
                   data={"drove_on": "2026-06-17", "from_place": "Studio",
                         "to_place": "Cúrate", "purpose": "Summer menu shoot",
                         "miles": "8.4"}, follow_redirects=False)
    assert r.status_code == 303
    trip = db.one("SELECT * FROM mileage WHERE to_place='Cúrate'")
    assert trip is not None and abs(trip["miles"] - 8.4) < 1e-6
    assert trip["rate_cents"] == config.MILEAGE_RATE_CENTS

    page = admin.get("/admin/financials/mileage")
    # 8.4 mi × 70¢ = $5.88
    assert page.status_code == 200 and "$5.88" in page.text

    r = admin.post(f"/admin/financials/mileage/{trip['id']}/delete",
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM mileage WHERE id=?", (trip["id"],)) is None


def test_dashboard_nudge_dismiss_clears_for_today(admin):
    """A 'Needs you today' nudge can be checked off; the dismissal is keyed to the
    underlying item and only suppresses it for the current local day (the worklist
    is 'needs you TODAY', so the item returns tomorrow if the condition still holds)."""
    iid = db.run(
        "INSERT INTO inquiries (name, business, email, message, created_at) "
        "VALUES (?,?,?,?, datetime('now','-5 days'))",
        ("Nudge Test Co", "Nudge Bistro", "nudge@example.com", "test msg"))
    key = f"inq_reply:{iid}"
    try:
        # the stale inquiry surfaces as a checkable nudge
        assert key in admin.get("/admin/home").text

        # an unknown nudge prefix is rejected (validated input, R18)
        bad = admin.post("/admin/home/nudge/dismiss",
                         data={"key": "bogus:1"}, follow_redirects=False)
        assert bad.status_code == 400

        # checking it off records the dismissal and drops it from today's worklist
        ok = admin.post("/admin/home/nudge/dismiss",
                        data={"key": key}, follow_redirects=False)
        assert ok.status_code == 303
        assert db.one("SELECT 1 FROM dismissed_nudges WHERE nudge_key=?", (key,))
        assert key not in admin.get("/admin/home").text
    finally:
        db.run("DELETE FROM dismissed_nudges WHERE nudge_key=?", (key,))
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def _quo_sig(secret_b64: str, raw: bytes, ts: str = "1700000000") -> str:
    """Build a valid openphone-signature header for `raw` (mirrors sms.verify_webhook)."""
    import base64, hashlib, hmac
    key = base64.b64decode(secret_b64)
    sig = base64.b64encode(hmac.new(key, ts.encode() + b"." + raw,
                                    hashlib.sha256).digest()).decode()
    return f"hmac;1;{ts};{sig}"


def test_quo_webhook_inert_without_secret(client, monkeypatch):
    """Ships inert: with no signing secret the inbound route refuses (503), writing
    nothing — the same posture as the Stripe webhook."""
    from app import config
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "")
    r = client.post("/webhooks/quo", content=b"{}")
    assert r.status_code == 503


def test_quo_webhook_rejects_bad_signature(client, monkeypatch):
    """Signature is the gate. A wrong/absent HMAC fails closed (400)."""
    import base64
    from app import config
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", base64.b64encode(b"k").decode())
    r = client.post("/webhooks/quo", content=b'{"type":"message.received"}',
                    headers={"openphone-signature": "hmac;1;1700000000;deadbeef"})
    assert r.status_code == 400


def test_quo_inbound_creates_sms_inquiry_and_is_idempotent(client, monkeypatch):
    """A text from an unknown number auto-creates a kind='sms' inquiry and records the
    message; a retried webhook (same provider id) is a no-op."""
    import base64, json
    from app import config
    secret = base64.b64encode(b"signing-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15557654321"
    body = {"type": "message.received",
            "data": {"object": {"id": "QUO_MSG_1", "direction": "incoming",
                                "from": phone, "to": "+15550001111",
                                "body": "Hi, do you shoot restaurants?"}}}
    raw = json.dumps(body).encode()
    sig = _quo_sig(secret, raw)
    try:
        r = client.post("/webhooks/quo", content=raw,
                        headers={"openphone-signature": sig})
        assert r.status_code == 200 and r.json()["ok"]
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='sms'", (phone,))
        assert inq is not None and inq["message"].startswith("Hi, do you shoot")
        msgs = db.all_("SELECT * FROM messages WHERE inquiry_id=?", (inq["id"],))
        assert len(msgs) == 1 and msgs[0]["direction"] == "in" and msgs[0]["channel"] == "sms"

        # retry with the same provider id → idempotent, no new inquiry/message
        r2 = client.post("/webhooks/quo", content=raw,
                         headers={"openphone-signature": sig})
        assert r2.status_code == 200 and r2.json().get("duplicate")
        assert db.one("SELECT COUNT(*) n FROM messages WHERE inquiry_id=?", (inq["id"],))["n"] == 1
        assert db.one("SELECT COUNT(*) n FROM inquiries WHERE phone=?", (phone,))["n"] == 1
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_MSG_1'")
        db.run("DELETE FROM inquiries WHERE phone=?", (phone,))


def test_inbox_sms_reply_logs_outbound(admin, monkeypatch):
    """Replying by text sends via the Quo adapter and logs an outbound bubble to the
    thread. SMS is gated on sms.configured() — inert until keys exist."""
    from app import sms
    phone = "+15553334444"
    iid = db.run("INSERT INTO inquiries (name, email, message, kind, phone) "
                 "VALUES (?,?,?,?,?)",
                 ("Texted Lead", "", "(no text)", "sms", phone))
    sent = {}
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send",
                        lambda to, body: sent.update(to=to, body=body) or "QUO_OUT_1")
    try:
        r = admin.post(f"/admin/inbox/{iid}/reply",
                       data={"channel": "sms", "message": "Yes! Let's talk."},
                       follow_redirects=False)
        assert r.status_code == 303
        assert sent == {"to": phone, "body": "Yes! Let's talk."}
        out = db.one("SELECT * FROM messages WHERE inquiry_id=? AND direction='out'", (iid,))
        assert out is not None and out["channel"] == "sms" and out["provider_msg_id"] == "QUO_OUT_1"

        # the conversation now shows both the inbound seed and the outbound reply
        page = admin.get(f"/admin/inbox?sel={iid}")
        assert "Yes! Let&#39;s talk." in page.text or "Yes! Let's talk." in page.text
    finally:
        db.run("DELETE FROM messages WHERE inquiry_id=?", (iid,))
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_inbound_call_creates_call_inquiry_and_is_idempotent(client, monkeypatch):
    """A completed inbound call from an unknown number auto-creates a kind='call'
    inquiry and records a channel='call' bubble; a Quo retry (same call id) is a no-op."""
    import base64, json
    from app import config
    secret = base64.b64encode(b"call-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15558889999"
    body = {"type": "call.completed",
            "data": {"object": {"id": "QUO_CALL_1", "direction": "incoming",
                                "from": phone, "to": "+15550001111",
                                "status": "completed", "duration": 134}}}
    raw = json.dumps(body).encode()
    sig = _quo_sig(secret, raw)
    try:
        r = client.post("/webhooks/quo", content=raw,
                        headers={"openphone-signature": sig})
        assert r.status_code == 200 and r.json()["ok"]
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='call'", (phone,))
        assert inq is not None
        msg = db.one("SELECT * FROM messages WHERE inquiry_id=? AND channel='call'", (inq["id"],))
        assert msg is not None and msg["direction"] == "in"
        assert "Incoming call" in msg["body"] and "2m14s" in msg["body"]

        # retry with the same call id → idempotent, no second row
        r2 = client.post("/webhooks/quo", content=raw,
                         headers={"openphone-signature": sig})
        assert r2.status_code == 200 and r2.json().get("duplicate")
        assert db.one("SELECT COUNT(*) n FROM messages WHERE inquiry_id=?", (inq["id"],))["n"] == 1
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_CALL_1'")
        db.run("DELETE FROM inquiries WHERE phone=?", (phone,))


def test_inquiry_dismiss_returns_to_inbox_when_asked(admin):
    """Triage actions invoked from the Inbox honor a safe return_to so Kevin stays
    in the Inbox instead of being bounced to Studio. An unsafe/off-site return_to
    falls back to the default Studio destination."""
    iid = db.run("INSERT INTO inquiries (name, email, message, kind) VALUES (?,?,?,?)",
                 ("Triage Lead", "t@x.com", "hi", "contact"))
    try:
        back = f"/admin/inbox?tab=all&sel={iid}"
        r = admin.post(f"/admin/studio/inquiries/{iid}/dismiss",
                       data={"return_to": back}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == back

        # open-redirect guard: a non-/admin/ target is ignored
        r2 = admin.post(f"/admin/studio/inquiries/{iid}/undismiss",
                        data={"return_to": "https://evil.example.com"},
                        follow_redirects=False)
        assert r2.status_code == 303 and r2.headers["location"] == "/admin/studio"
    finally:
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_inbound_reopens_dismissed_thread(client, monkeypatch):
    """A fresh inbound text on a thread the user had dismissed clears dismissed_at
    so it resurfaces in the active inbox instead of vanishing into the archive."""
    import base64, json
    from app import config
    secret = base64.b64encode(b"reopen-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15552223333"
    iid = db.run("INSERT INTO inquiries (name, email, message, kind, phone, dismissed_at) "
                 "VALUES (?,?,?,?,?, datetime('now'))",
                 ("Texted Lead", "", "old text", "sms", phone))
    body = {"type": "message.received",
            "data": {"object": {"id": "QUO_REOPEN_1", "direction": "incoming",
                                "from": phone, "body": "Actually, are you free Friday?"}}}
    raw = json.dumps(body).encode()
    try:
        assert db.one("SELECT dismissed_at FROM inquiries WHERE id=?", (iid,))["dismissed_at"]
        r = client.post("/webhooks/quo", content=raw,
                        headers={"openphone-signature": _quo_sig(secret, raw)})
        assert r.status_code == 200
        # same thread (no fork), now un-dismissed
        assert db.one("SELECT COUNT(*) n FROM inquiries WHERE phone=?", (phone,))["n"] == 1
        assert db.one("SELECT dismissed_at FROM inquiries WHERE id=?", (iid,))["dismissed_at"] is None
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_REOPEN_1'")
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_missed_call_and_transcript_enrichment(client, monkeypatch):
    """A missed call reads as 'Missed call'; a later transcript event appends to the
    same call row rather than creating a new one, matched by call id."""
    import base64, json
    from app import config
    secret = base64.b64encode(b"call-key-2").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15557778888"
    call = {"type": "call.completed",
            "data": {"object": {"id": "QUO_CALL_2", "direction": "incoming",
                                "from": phone, "to": "+15550001111",
                                "status": "missed"}}}
    raw = json.dumps(call).encode()
    try:
        r = client.post("/webhooks/quo", content=raw,
                        headers={"openphone-signature": _quo_sig(secret, raw)})
        assert r.status_code == 200
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='call'", (phone,))
        msg = db.one("SELECT * FROM messages WHERE inquiry_id=? AND channel='call'", (inq["id"],))
        assert msg["body"] == "Missed call"

        # transcript event for the same call appends, not a new row
        tr = {"type": "call.transcript.completed",
              "data": {"object": {"callId": "QUO_CALL_2",
                                  "dialogue": [{"content": "Hi, leaving a voicemail"},
                                               {"content": "call me back please"}]}}}
        traw = json.dumps(tr).encode()
        r2 = client.post("/webhooks/quo", content=traw,
                         headers={"openphone-signature": _quo_sig(secret, traw)})
        assert r2.status_code == 200 and r2.json().get("enriched")
        assert db.one("SELECT COUNT(*) n FROM messages WHERE inquiry_id=?", (inq["id"],))["n"] == 1
        updated = db.one("SELECT body FROM messages WHERE id=?", (msg["id"],))
        assert "Missed call" in updated["body"] and "Transcript:" in updated["body"]
        assert "voicemail" in updated["body"]
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_CALL_2'")
        db.run("DELETE FROM inquiries WHERE phone=?", (phone,))


def test_db_ident_allows_listed_identifier():
    assert db.ident("reminded_24h", {"reminded_24h", "reminded_48h"}) == "reminded_24h"


def test_db_ident_rejects_unlisted_identifier():
    # The gate must fail loud so a stray identifier can't become injection.
    import pytest
    with pytest.raises(ValueError):
        db.ident("bookings; DROP TABLE bookings", {"reminded_24h", "reminded_48h"})
