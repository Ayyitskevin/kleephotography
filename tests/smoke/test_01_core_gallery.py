"""Smoke domain slice — see tests/smoke/conftest.py for fixtures."""

import io
import os
import re
import tempfile
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, platekit
from app.main import app
from tests.smoke._helpers import (
    _checkout_event,
    _cleanup_money_chain,
    _close,
    _jpeg_bytes,
    _logo_png,
    _mp4_bytes,
    _post_signed,
    _quo_sig,
    _ready_photo_gallery,
    _ready_video,
    _seam_license_with_gallery,
    _seed_money_chain,
    _spark_rect_count,
    _stripe_sig,
)

pytestmark = pytest.mark.smoke


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
    r = admin.post(
        "/admin/galleries",
        data={"title": "Test Bistro", "client_name": "Chef"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    assert g["title"] == "Test Bistro" and len(g["slug"]) >= 12

    # upload a photo
    r = admin.post(
        f"/admin/galleries/{g['id']}/upload",
        files=[("files", ("dish.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    assert r.status_code == 200 and r.json()["accepted"] == 1

    # wait for derivative job

    for _ in range(50):
        a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
        if a["status"] == "ready":
            break
        time.sleep(0.2)
    assert a["status"] == "ready" and a["width"] == 800

    # publish with PIN
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Test Bistro", "pin": "1234", "published": "true"},
    )

    # set cover from the asset grid; nonexistent asset 404s
    assert admin.post(f"/admin/galleries/{g['id']}/assets/999999/cover").status_code == 404
    r = admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/cover", follow_redirects=False)
    assert r.status_code == 303
    assert (
        db.one("SELECT cover_asset_id FROM galleries WHERE id=?", (g["id"],))["cover_asset_id"]
        == a["id"]
    )
    # saving settings must NOT wipe the cover (field no longer in the form)
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Test Bistro", "pin": "1234", "published": "true"},
    )
    assert (
        db.one("SELECT cover_asset_id FROM galleries WHERE id=?", (g["id"],))["cover_asset_id"]
        == a["id"]
    )

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
        lightbox_tag = re.search(r'<div id="lightbox"[^>]*>', r.text, re.S).group(0)
        assert 'aria-label="Media viewer"' in lightbox_tag
        assert 'class="lb-play" aria-label="Slideshow" aria-pressed="false"' in r.text
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
        r = pub.get(f"/media/{g['slug']}/original/{a['id']}", headers={"Range": "bytes=0-99"})
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
        r = pub.post(
            f"/g/{g['slug']}/email",
            data={"email": "chef@bistro.com", "asset_id": str(a["id"])},
            follow_redirects=False,
        )
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
        # favorites ZIP: the export rail offers "takes only" with a live count,
        # and the bundle holds exactly the faved original
        page = pub.get(f"/g/{g['slug']}").text
        assert f"/g/{g['slug']}/download/favorites" in page and "1 take circled" in page
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
    assert (
        db.one("SELECT cover_asset_id FROM galleries WHERE id=?", (g["id"],))["cover_asset_id"]
        is None
    )

    # activity recorded
    rows = db.all_("SELECT * FROM downloads WHERE gallery_id=?", (g["id"],))
    assert len(rows) == 3  # single + zip + favorites zip
    v = db.one("SELECT * FROM visitors WHERE gallery_id=?", (g["id"],))
    assert v["email"] == "chef@bistro.com"


def test_client_proofing_completion_state():
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("ProofClosure01", "Summer menu selects", "2468"),
    )
    sid = db.run(
        "INSERT INTO sections (gallery_id, name, position, proof_target) VALUES (?,?,?,?)",
        (gid, "Hero dishes", 0, 2),
    )
    aids = [
        db.run(
            "INSERT INTO assets (gallery_id, section_id, kind, filename, stored, status) "
            "VALUES (?,?,?,?,?,?)",
            (gid, sid, "photo", f"hero-{i}.jpg", f"proofclosure{i}.jpg", "ready"),
        )
        for i in range(2)
    ]
    originals = config.MEDIA_DIR / str(gid) / "original"
    originals.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        Image.new("RGB", (8, 8), (180, 90, 40)).save(originals / f"proofclosure{i}.jpg")

    with TestClient(app, base_url="https://testserver") as pub:
        assert pub.post("/g/ProofClosure01/pin", data={"pin": "2468"}).status_code == 200

        # Incomplete state is explicit, semantic, and links to the existing contact flow.
        page = pub.get("/g/ProofClosure01").text
        assert 'id="proof-status"' in page
        assert 'role="status" aria-live="polite" aria-atomic="true"' in page
        assert "Selections in progress" in page and "Choose 2 more photos" in page
        assert "prefill=gallery_question" in page and "Message Kevin with a question" in page

        first = pub.post(f"/g/ProofClosure01/fav/{aids[0]}")
        assert first.status_code == 200
        assert 'id="proof-status"' in first.text and 'hx-swap-oob="outerHTML"' in first.text
        assert "Selections in progress" in first.text and "Choose 1 more photo" in first.text

        # The final selection flips the live status without adding approval semantics.
        final = pub.post(f"/g/ProofClosure01/fav/{aids[1]}")
        assert final.status_code == 200
        assert "Selections complete" in final.text
        assert "final approval and delivery happen separately" in final.text
        assert "Message Kevin about next steps" in final.text

        # Reload derives the same completed state from current targets and favorites.
        reloaded = pub.get("/g/ProofClosure01").text
        assert "Selections complete" in reloaded and "2 of 2 selected" in reloaded

        # Existing unfavorite behavior reopens the derived state.
        reopened = pub.post(f"/g/ProofClosure01/fav/{aids[0]}")
        assert reopened.status_code == 200
        assert "Selections in progress" in reopened.text and "Choose 1 more photo" in reopened.text


def test_video_pipeline_full_flow(admin):
    """Locks the previously-untested video path end-to-end: a .mp4 upload routes to
    kind='video' + a transcode job; ffmpeg produces the web mp4 + poster + thumb and
    the asset goes ready with probed dims/duration; the gallery renders a video tile
    and media serves the mp4 with HTTP Range (iOS scrubbing) plus the poster frame."""
    from pathlib import Path

    from app import config

    r = admin.post(
        "/admin/galleries",
        data={"title": "Test Kitchen Reel", "client_name": "Chef"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")

    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        # upload routes a .mp4 to kind='video' (image extensions would be kind='photo')
        r = admin.post(
            f"/admin/galleries/{g['id']}/upload",
            files=[("files", ("reel.mp4", _mp4_bytes(), "video/mp4"))],
        )
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
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Test Kitchen Reel", "pin": "1234", "published": "true"},
    )

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
        r = pub.get(f"/media/{g['slug']}/web/{a['id']}", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        # grid thumb serves as a jpeg
        r = pub.get(f"/media/{g['slug']}/thumb/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        # poster frame serves as a jpeg; the poster route is video-only
        r = pub.get(f"/media/{g['slug']}/poster/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert pub.get(f"/media/{g['slug']}/poster/999999").status_code == 404

        # duration badge renders from the probed duration, and the tile offers
        # the web-ready MP4 action alongside the original download
        assert 'class="dur-badge"' in page
        assert "icon-btn-mp4" in page
        # the lightbox reaches the web MP4 too (tile data attr + viewer chip)
        assert "data-dl-web=" in page and 'class="lb-dl-mp4"' in page

        # web-MP4 download: the email gate carries the web flag through, then
        # the transcoded H.264 serves as an attachment (video-only route)
        r = pub.get(f"/g/{g['slug']}/download", params={"asset_id": a["id"], "web": 1})
        assert 'name="web" value="1"' in r.text
        r = pub.post(
            f"/g/{g['slug']}/email",
            # same address the photo-flow test captures — test_captured_emails
            # asserts exactly one unique captured email across the suite
            data={"email": "chef@bistro.com", "asset_id": a["id"], "web": 1},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith(f"/download/web/{a['id']}")
        r = pub.get(f"/g/{g['slug']}/download/web/{a['id']}")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert "_web.mp4" in r.headers.get("content-disposition", "")
        assert pub.get(f"/g/{g['slug']}/download/web/999999").status_code == 404


def test_video_comments_flow(admin):
    """C3: timecoded review comments on a video deliverable. A PIN-gated client
    leaves a note anchored to a playhead second; replies thread under it and
    inherit the parent's timecode; the admin authors + threads too; admin hide
    soft-deletes the comment AND its replies and writes one audit row. The gate
    rejects non-visitors and non-video assets."""
    g, vid, photo = _ready_video(admin, title="Reel Review A")

    # the client gallery page ships the lightbox comment wiring
    with TestClient(app) as pub:
        assert (
            pub.post(
                f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False
            ).status_code
            == 303
        )
        page = pub.get(f"/g/{g['slug']}").text
        assert f'data-slug="{g["slug"]}"' in page
        assert 'class="lb-comments"' in page

        # client posts a top-level note anchored at 12.5s → persists with that timecode
        r = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Tighten this cut", "timecode": 12.5},
        )
        assert r.status_code == 200
        thread = r.json()
        assert len(thread) == 1
        top = thread[0]
        assert top["timecode"] == 12.5 and top["author_role"] == "client"
        assert top["body"] == "Tighten this cut" and top["parent_id"] is None

        # a reply inherits the parent's timecode (ignores any posted timecode)
        r = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "agreed", "parent_id": top["id"], "timecode": 99},
        )
        assert r.status_code == 200
        reply = next(c for c in r.json() if c["parent_id"] == top["id"])
        assert reply["timecode"] == 12.5  # inherited, not 99

        # GET returns the visible thread (both rows), ordered for display
        got = pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()
        assert {c["id"] for c in got} == {top["id"], reply["id"]}

        # the ADMIN gallery page renders the same visible thread through its comment
        # macro — it now builds it from the shared video_comment_thread helper
        # (dicts, not sqlite3.Rows), so this pins that render path
        apage = admin.get(f"/admin/galleries/{g['id']}").text
        assert "Tighten this cut" in apage and "agreed" in apage

        # gate: empty body 400, bogus reply target 400, photo/asset are not video → 404
        assert (
            pub.post(f"/g/{g['slug']}/comments/{vid['id']}", data={"body": "   "}).status_code
            == 400
        )
        assert (
            pub.post(
                f"/g/{g['slug']}/comments/{vid['id']}", data={"body": "x", "parent_id": 999999}
            ).status_code
            == 400
        )
        assert pub.get(f"/g/{g['slug']}/comments/{photo['id']}").status_code == 404
        assert (
            pub.post(f"/g/{g['slug']}/comments/{photo['id']}", data={"body": "x"}).status_code
            == 404
        )

    # gate: a visitor cookie is required (no PIN → 403 on read and write)
    with TestClient(app) as anon:
        assert anon.get(f"/g/{g['slug']}/comments/{vid['id']}").status_code == 403
        assert (
            anon.post(f"/g/{g['slug']}/comments/{vid['id']}", data={"body": "x"}).status_code == 403
        )

    # admin authors a studio note + a reply; both visible in the shared thread
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/comments/{vid['id']}",
            data={"body": "Color is warm here", "timecode": 5},
            follow_redirects=False,
        ).status_code
        == 303
    )
    studio = db.one(
        "SELECT * FROM video_comments WHERE asset_id=? AND author_role='admin'", (vid["id"],)
    )
    assert studio and studio["timecode"] == 5.0
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/comments/{vid['id']}",
            data={"body": "reply under client note", "parent_id": top["id"]},
            follow_redirects=False,
        ).status_code
        == 303
    )

    # admin hide cascades to descendants + writes exactly one audit row
    n_before = db.one(
        "SELECT COUNT(*) AS n FROM video_comments WHERE asset_id=? AND deleted_at IS NULL",
        (vid["id"],),
    )["n"]
    audit_before = db.one("SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment'")[
        "n"
    ]
    assert (
        admin.post(f"/admin/comments/{top['id']}/hide", follow_redirects=False).status_code == 303
    )
    # top + its client reply + the admin reply under it all hidden together
    visible = db.all_(
        "SELECT id FROM video_comments WHERE asset_id=? AND deleted_at IS NULL", (vid["id"],)
    )
    vis_ids = {r["id"] for r in visible}
    assert top["id"] not in vis_ids and reply["id"] not in vis_ids
    assert studio["id"] in vis_ids  # a sibling thread is untouched
    assert len(vis_ids) == n_before - 3
    audit_after = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' AND action='hide'"
    )["n"]
    assert audit_after == audit_before + 1
    # hidden comments drop from the client-facing thread too
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        got = pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()
        assert top["id"] not in {c["id"] for c in got}

    # bogus comment id → 404
    assert admin.post("/admin/comments/999999/hide", follow_redirects=False).status_code == 404


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
        root = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Re-grade the intro", "timecode": 3},
        ).json()[0]
        # a new comment is born 'open' and the status field surfaces client-side
        assert root["status"] == "open"
        pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "esp. the first shot", "parent_id": root["id"]},
        )
        sib = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Audio dips at the end", "timecode": 20},
        ).json()
        sibling = next(c for c in sib if c["parent_id"] is None and c["id"] != root["id"])

    reply = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))
    # DB default really is 'open'
    assert db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"] == "open"

    # resolve the root → cascades to the reply; sibling thread untouched; +1 audit
    a0 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE entity_type='video_comment' AND action='resolved'"
    )["n"]
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 303
    )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"]
        == "resolved"
    )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (reply["id"],))["status"]
        == "resolved"
    )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (sibling["id"],))["status"] == "open"
    )
    a1 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE entity_type='video_comment' AND action='resolved'"
    )["n"]
    assert a1 == a0 + 1

    # client thread reflects resolved status (render extends; server-proven)
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        got = {c["id"]: c["status"] for c in pub.get(f"/g/{g['slug']}/comments/{vid['id']}").json()}
        assert got[root["id"]] == "resolved" and got[reply["id"]] == "resolved"
        assert got[sibling["id"]] == "open"

    # illegal transitions rejected: re-resolve → 409, reopen an open one → 409
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 409
    )
    assert (
        admin.post(f"/admin/comments/{sibling['id']}/reopen", follow_redirects=False).status_code
        == 409
    )
    # a reply (non-root) is not independently transitionable → 400
    assert (
        admin.post(f"/admin/comments/{reply['id']}/resolve", follow_redirects=False).status_code
        == 400
    )

    # reopen the root → cascades back to open; +1 audit (action='open')
    o0 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' AND action='open'"
    )["n"]
    assert (
        admin.post(f"/admin/comments/{root['id']}/reopen", follow_redirects=False).status_code
        == 303
    )
    assert db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"] == "open"
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (reply["id"],))["status"] == "open"
    )
    o1 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' AND action='open'"
    )["n"]
    assert o1 == o0 + 1

    # hide × status orthogonal: a hidden comment is out of the workflow → 404
    assert (
        admin.post(f"/admin/comments/{sibling['id']}/hide", follow_redirects=False).status_code
        == 303
    )
    assert (
        admin.post(f"/admin/comments/{sibling['id']}/resolve", follow_redirects=False).status_code
        == 404
    )

    # bogus comment id → 404 on both transitions
    assert admin.post("/admin/comments/999999/resolve", follow_redirects=False).status_code == 404
    assert admin.post("/admin/comments/999999/reopen", follow_redirects=False).status_code == 404


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
        root = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Re-grade the intro", "timecode": 3},
        ).json()[0]
        pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "first shot esp.", "parent_id": root["id"]},
        )
        sib = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Audio dips at the end", "timecode": 20},
        ).json()
        sibling = next(c for c in sib if c["parent_id"] is None and c["id"] != root["id"])
    child = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))

    # admin resolves the root thread (cascades 'resolved' to root + child)
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 303
    )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"]
        == "resolved"
    )

    # ── a client reply on the RESOLVED thread reopens the whole thread ───────
    sys0 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system'"
    )["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        # reply to the CHILD (depth 2) — the upward walk must still reach the root
        thread = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "still too warm", "parent_id": child["id"], "timecode": 99},
        ).json()
    new_reply = db.one("SELECT * FROM video_comments WHERE body='still too warm'")
    # C3 not regressed: a reply still inherits its parent's timecode (child @3, not 99)
    assert new_reply["timecode"] == child["timecode"]
    # whole thread back to open — root, the original child, and the new reply
    assert db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"] == "open"
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (child["id"],))["status"] == "open"
    )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (new_reply["id"],))["status"]
        == "open"
    )
    # the sibling thread never changed
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (sibling["id"],))["status"] == "open"
    )
    # the open-count signal: the client thread shows the root open again
    assert {c["id"]: c["status"] for c in thread}[root["id"]] == "open"
    # exactly one system-attributed audit row, naming the triggering reply
    sys_rows = db.all_(
        "SELECT * FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system' ORDER BY id"
    )
    assert len(sys_rows) == sys0 + 1
    last = sys_rows[-1]
    assert last["entity_id"] == root["id"]
    diff = json.loads(last["diff_json"])
    assert diff["from"] == "resolved" and diff["to"] == "open"
    assert diff["cause_reply_id"] == new_reply["id"]

    # ── a reply on an already-OPEN thread is a status no-op (no row written) ──
    sys_a = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system'"
    )["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "one more on audio", "parent_id": sibling["id"]},
        )
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (sibling["id"],))["status"] == "open"
    )
    sys_b = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system'"
    )["n"]
    assert sys_b == sys_a  # no spurious reopen audit row

    # ── admin transition guards still intact after the helper extraction ─────
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 303
    )  # open → resolved
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 409
    )  # already resolved
    assert (
        admin.post(f"/admin/comments/{child['id']}/reopen", follow_redirects=False).status_code
        == 400
    )  # non-root target


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
    monkeypatch.setattr(
        reopen_notify, "notify_reopen", lambda payload: pushed.append(payload) or True
    )

    # client builds a thread (root + reply), then admin resolves it
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        root = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "Re-grade the intro", "timecode": 3},
        ).json()[0]
        pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "first shot esp.", "parent_id": root["id"]},
        )
    child = db.one("SELECT * FROM video_comments WHERE parent_id=?", (root["id"],))
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 303
    )
    assert not pushed  # building + resolving never pushes — only a client reopen does

    # ── a client reply on the RESOLVED thread pushes exactly one notify ──────
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        r = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "notify-reopen warm", "parent_id": child["id"]},
        )
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
        pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "one more", "parent_id": new_reply["id"]},
        )
    assert len(pushed) == 1  # the thread was already open — no reopen, no push

    # ── a push that BLOWS UP must not break the comment or the reopen ────────
    assert (
        admin.post(f"/admin/comments/{root['id']}/resolve", follow_redirects=False).status_code
        == 303
    )  # resolve again

    def boom(payload):
        raise RuntimeError("odysseus exploded")

    monkeypatch.setattr(reopen_notify, "notify_reopen", boom)
    sys0 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system'"
    )["n"]
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        r = pub.post(
            f"/g/{g['slug']}/comments/{vid['id']}",
            data={"body": "still warm again", "parent_id": child["id"]},
        )
        assert r.status_code == 200  # client comment unaffected by the push failure
    assert (
        db.one("SELECT status FROM video_comments WHERE id=?", (root["id"],))["status"] == "open"
    )  # reopen still committed
    sys1 = db.one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entity_type='video_comment' "
        "AND action='open' AND actor='system'"
    )["n"]
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
    # Self-contained: seed a published gallery rather than grabbing the newest
    # row from an earlier test (that coupling failed under -k subsets).
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("pin-lock-01", "Pin Lockout Gallery", "1234"),
    )
    g = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    try:
        with TestClient(app) as pub:
            for _ in range(5):
                pub.post(f"/g/{g['slug']}/pin", data={"pin": "9999"})
            r = pub.post(f"/g/{g['slug']}/pin", data={"pin": "1234"})
            assert r.status_code == 429
    finally:
        db.run("DELETE FROM pin_attempts", ())
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_expired_gallery(admin):
    import datetime as dt

    # Self-contained: create the gallery this test needs rather than grabbing
    # (and mutating) the newest one from an earlier test — that coupling made
    # the test fail under -k subsets when no prior gallery existed.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("expired-card-01", "Expiry Card", "1234"),
    )
    g = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    try:
        db.run("UPDATE galleries SET expires_at='2000-01-01' WHERE id=?", (gid,))
        with TestClient(app) as pub:
            assert pub.get(f"/g/{g['slug']}").status_code == 410

        # the grid card derives an Expiring status from a real expiry so Kevin sees
        # lockouts before clients do. Anchor on the grid card (last href occurrence —
        # the orphan strip may mention it earlier) and read to the card's </a>.
        def card_of(gid_):
            page = admin.get("/admin/galleries").text
            start = page.rindex(f"/admin/galleries/{gid_}")
            return page[start : page.index("</a>", start)]

        card = card_of(gid)
        assert ">Expiring<" in card and "expired" in card  # past-due → dated "expired"

        near = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        db.run("UPDATE galleries SET expires_at=? WHERE id=?", (near, gid))
        card = card_of(gid)
        assert ">Expiring<" in card and "3 days" in card  # within the 7-day window

        far = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        db.run("UPDATE galleries SET expires_at=? WHERE id=?", (far, gid))
        # far-future expiry isn't flagged Expiring on the card grid — only soon/expired
        # are; the full date lives on the gallery detail page (checked below)
        assert ">Expiring<" not in card_of(gid)

        # delivery email prefill carries the expiry note (form renders when published)
        assert f"Available until {far}" in admin.get(f"/admin/galleries/{gid}").text
    finally:
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_expired_gallery_blocks_fav_and_poster(admin):
    # Every gated gallery surface 410s once expired; the fav toggle and the video
    # poster route used to skip that check, letting a visitor with a live cookie
    # keep changing proofing picks / pulling posters after the window closed.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, expires_at) "
        "VALUES (?,?,?,1,'2000-01-01')",
        ("expired-surfaces", "Expired Surfaces", "1234"),
    )
    aid = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (gid, "video", "v.mp4", "vfile.mp4", "ready"),
    )
    try:
        with TestClient(app) as pub:
            assert pub.post(f"/g/expired-surfaces/fav/{aid}").status_code == 410
            assert pub.get(f"/media/expired-surfaces/poster/{aid}").status_code == 410
    finally:
        db.run("DELETE FROM assets WHERE id=?", (aid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
