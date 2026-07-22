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


def test_portal_lifecycle(admin):
    import datetime as dt

    from app import jobs, presets

    crop_slugs = [ps["slug"] for ps in presets.active()]
    g, a = _ready_photo_gallery(admin, title="Portal Bistro")
    c_id = db.run(
        "INSERT INTO clients (name, company, email) VALUES (?,?,?)",
        ("Chef Portal", "Portal Bistro", "portal@example.test"),
    )
    c = db.one("SELECT * FROM clients WHERE id=?", (c_id,))

    # link gallery to the studio client, with captions
    r = admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={
            "title": g["title"],
            "pin": "1234",
            "published": "true",
            "client_id": str(c["id"]),
            "captions": "Golden hour plating shot #foodie",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT client_id FROM galleries WHERE id=?", (g["id"],))["client_id"] == c["id"]

    # usage rights on the client
    admin.post(
        f"/admin/studio/clients/{c['id']}",
        data={
            "name": c["name"],
            "company": c["company"] or "",
            "usage_rights": "Social + web, 12 months.",
        },
        follow_redirects=False,
    )

    # brand asset upload — allowlist enforced
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/brand",
        files=[
            ("files", ("logo.png", _jpeg_bytes(64, 64), "image/png")),
            ("files", ("evil.exe", b"MZ", "application/octet-stream")),
        ],
        follow_redirects=False,
    )
    assert r.status_code == 303
    brand = db.all_("SELECT * FROM brand_assets WHERE client_id=?", (c["id"],))
    assert len(brand) == 1 and brand[0]["filename"] == "logo.png"
    assert admin.get(f"/admin/studio/clients/{c['id']}/brand/{brand[0]['id']}").status_code == 200

    visitor_id = db.run(
        "INSERT INTO visitors (gallery_id, token, email) VALUES (?,?,?)",
        (g["id"], f"portal-fixture-{g['id']}", "chef@bistro.com"),
    )
    db.run(
        "INSERT OR IGNORE INTO favorites (visitor_id, asset_id) VALUES (?,?)",
        (visitor_id, a["id"]),
    )

    # create portal (idempotent: second create rejected), backfills crop jobs
    r = admin.post(f"/admin/studio/clients/{c['id']}/portal", follow_redirects=False)
    assert r.status_code == 303
    assert (
        admin.post(f"/admin/studio/clients/{c['id']}/portal", follow_redirects=False).status_code
        == 400
    )
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
    with TestClient(app):  # fresh lifespan drains queued social-crop jobs
        for _ in range(50):
            if all((jobs.crops_dir(g["id"]) / f"{stem}_{n}.jpg").is_file() for n in crop_slugs):
                break
            time.sleep(0.2)
    assert (jobs.crops_dir(g["id"]) / f"{stem}_1x1.jpg").is_file()

    with TestClient(app) as pub:
        # unpublished portal 404s
        assert pub.get(f"/portal/{portal['slug']}").status_code == 404
        admin.post(
            f"/admin/studio/clients/{c['id']}/portal/publish",
            data={"published": "true"},
            follow_redirects=False,
        )

        # PIN gate: page, wrong PIN, lockout uses a namespaced bucket well clear
        # of gallery ids AND the inquiry-throttle sentinels (-2/-3/-4)
        from app.public.portal import _pin_bucket

        r = pub.get(f"/portal/{portal['slug']}")
        assert r.status_code == 200 and "PIN" in r.text
        assert pub.post(f"/portal/{portal['slug']}/pin", data={"pin": "0000"}).status_code == 401
        bucket = db.one("SELECT gallery_id FROM pin_attempts")["gallery_id"]
        assert bucket == _pin_bucket(portal["id"])
        assert bucket < -1_000_000  # distinct large-negative band, no sentinel collision
        for _ in range(4):
            pub.post(f"/portal/{portal['slug']}/pin", data={"pin": "0000"})
        assert (
            pub.post(f"/portal/{portal['slug']}/pin", data={"pin": portal["pin"]}).status_code
            == 429
        )
        db.run("DELETE FROM pin_attempts", ())

        # no cookie → media gated
        assert pub.get(f"/portal/{portal['slug']}/thumb/{a['id']}").status_code == 403
        assert pub.get(f"/portal/{portal['slug']}/crops.zip").status_code == 403

        # right PIN → portal renders all four sections
        r = pub.post(
            f"/portal/{portal['slug']}/pin", data={"pin": portal["pin"]}, follow_redirects=False
        )
        assert r.status_code == 303
        r = pub.get(f"/portal/{portal['slug']}")
        assert r.status_code == 200
        assert f"/g/{g['slug']}" in r.text  # gallery link
        assert "Golden hour plating shot" in r.text  # captions
        assert "Social + web, 12 months." in r.text  # usage rights
        assert "logo.png" in r.text  # brand asset
        assert f"/portal/{portal['slug']}/thumb/{a['id']}" in r.text  # crop tile
        # section-count pills tell the client at a glance what's in each section
        n_gal = db.one(
            "SELECT COUNT(*) AS n FROM galleries WHERE client_id=? AND published=1", (c["id"],)
        )["n"]
        n_brand = db.one("SELECT COUNT(*) AS n FROM brand_assets WHERE client_id=?", (c["id"],))[
            "n"
        ]
        assert f'class="section-count">{n_gal}<' in r.text  # Galleries (N)
        assert f'class="section-count">{n_brand}<' in r.text  # Brand assets (N)
        # caption-meta line surfaces the "tap to expand" hint when any gallery has captions
        assert "include caption drafts" in r.text
        # the favorites-summary line aggregates ALL faves across the client's
        # published galleries — one heart, one count, both numbers right.
        # Re-fav to ensure a known state (prior tests may have shuffled).
        existing = db.one(
            """SELECT f.asset_id FROM favorites f
                             JOIN assets ax ON ax.id=f.asset_id
                             JOIN galleries gx ON gx.id=ax.gallery_id
                             WHERE gx.client_id=? AND gx.published=1
                               AND ax.kind='photo' AND ax.status='ready'""",
            (c["id"],),
        )
        if not existing:
            v = db.one("SELECT id FROM visitors WHERE gallery_id=? LIMIT 1", (g["id"],))
            if v:
                db.run(
                    "INSERT OR IGNORE INTO favorites (visitor_id, asset_id) VALUES (?,?)",
                    (v["id"], a["id"]),
                )
        n_faves = db.one(
            """SELECT COUNT(DISTINCT f.asset_id) AS n FROM favorites f
               JOIN assets ax ON ax.id=f.asset_id
               JOIN galleries gx ON gx.id=ax.gallery_id
               WHERE gx.client_id=? AND gx.published=1
                 AND ax.kind='photo' AND ax.status='ready'""",
            (c["id"],),
        )["n"]
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
        db.run("UPDATE galleries SET created_at=datetime('now', '-1 hour') WHERE id=?", (g["id"],))
        db.run(
            "UPDATE portals SET last_visit=datetime('now', '-30 minutes') WHERE id=?",
            (portal["id"],),
        )
        new_gid = db.run(
            "INSERT INTO galleries (slug, title, pin, client_id, published) VALUES (?,?,?,?,1)",
            ("PortalNewPill01", "Fresh delivery", "1234", c["id"]),
        )
        page = pub.get(f"/portal/{portal['slug']}").text
        # the fresh gallery carries a NEW pill; the original (created before
        # the rewound last_visit) does not. Anchor each row on its unique slug
        # since titles can appear elsewhere (e.g. in crop tile figcaptions).
        fresh_start = page.index("/g/PortalNewPill01")
        fresh_row = page[fresh_start : page.index("</li>", fresh_start)]
        assert 'class="new-pill"' in fresh_row
        orig_start = page.index(f"/g/{g['slug']}")
        orig_row = page[orig_start : page.index("</li>", orig_start)]
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

        # an expired gallery must not be a live link in the portal — /g/{slug}
        # 410s, so the portal renders it unlinked with a "get in touch" note.
        db.run("UPDATE galleries SET expires_at='2000-01-01' WHERE id=?", (g["id"],))
        page = pub.get(f"/portal/{portal['slug']}").text
        row_start = page.index(g["title"])
        row = page[row_start : page.index("</li>", row_start)]
        assert f'href="/g/{g["slug"]}"' not in row  # no live link
        assert "expired 2000-01-01" in row and "get in touch" in row
        db.run("UPDATE galleries SET expires_at=NULL WHERE id=?", (g["id"],))
        # neutralize this check's extra portal view so the visit-count assertion
        # below (== 5) still holds
        db.run("UPDATE portals SET visits=visits-1 WHERE id=?", (portal["id"],))

        # crop + thumb + brand downloads
        assert pub.get(f"/portal/{portal['slug']}/thumb/{a['id']}").status_code == 200
        for ratio in crop_slugs:
            r = pub.get(f"/portal/{portal['slug']}/crop/{a['id']}/{ratio}")
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        # untrusted slug token: unknown ratio 404s
        assert pub.get(f"/portal/{portal['slug']}/crop/{a['id']}/16x9").status_code == 404
        # an inactive preset's slug also 404s — validation reads presets.active(),
        # so deactivating a preset immediately stops its token from resolving
        db.run("UPDATE crop_presets SET active=0 WHERE slug='9x16'")
        assert pub.get(f"/portal/{portal['slug']}/crop/{a['id']}/9x16").status_code == 404
        db.run("UPDATE crop_presets SET active=1 WHERE slug='9x16'")
        assert pub.get(f"/portal/{portal['slug']}/brand/{brand[0]['id']}").status_code == 200

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
    audit = page.text[audit_start : page.text.index("</p>", audit_start)]
    assert "<strong>1</strong> published gallery" in audit
    assert "<strong>&hearts; 1</strong> favorite" in audit
    assert "KB brand" in audit
    # per-gallery row shows asset + fav counts
    assert "<th>Assets</th>" in page.text and "<th>Favorites</th>" in page.text

    # brand delete removes row + file
    from app import config as cfg

    stored = cfg.BRAND_DIR / str(c["id"]) / brand[0]["stored"]
    assert stored.is_file()
    r = admin.post(
        f"/admin/studio/clients/{c['id']}/brand/{brand[0]['id']}/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert not db.one("SELECT 1 AS x FROM brand_assets WHERE id=?", (brand[0]["id"],))
    assert not stored.exists()


def test_contact_prefill():
    with TestClient(app) as pub:
        # no prefill → form renders blank (no canned message)
        r = pub.get("/contact")
        assert r.status_code == 200 and 'name="business"' in r.text
        assert "<span data-scope-label>Project scope</span>" in r.text
        assert "additional formats" not in r.text
        assert "Listing / brokerage" in r.text

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
        assert (
            "question about the &#34;Spring Menu&#34;" in r.text
            or 'question about the "Spring Menu"' in r.text
        )
        # missing gallery name → falls through without prefilled message
        r = pub.get("/contact?prefill=gallery_question")
        assert "question about" not in r.text

        # services-page deep link → project type + canned tier message
        r = pub.get("/contact?service=Real+Estate&tier=Signature")
        assert r.status_code == 200
        assert 'value="Real Estate" selected' in r.text or (
            'value="Real Estate"' in r.text and "selected" in r.text
        )
        assert "Signature tier for Real Estate" in r.text
        assert "<span data-scope-label>Listing / property scope</span>" in r.text
        assert "3,200 sq ft · 4 bed 3 bath" in r.text

        r = pub.get("/contact?service=Portraits")
        assert "<span data-scope-label>Subject / team scope</span>" in r.text


def test_pipeline_dashboard(admin):
    # The pipeline summary strip lives on the Activity view (the board itself is
    # strict-1:1 kanban). The per-stage overdue chip is the overdue indicator.
    page = admin.get("/admin/studio/activity")
    assert page.status_code == 200
    assert "Retainer Paid <strong>1</strong>" in page.text  # Dana's project sits at retainer paid
    assert "nothing outstanding" in page.text  # her invoice is fully paid
    # No overdue *indicator* when nothing is overdue: the per-stage chip only
    # renders for a stage that actually has past-due invoices.
    assert "pipeline-overdue" not in page.text

    # a sent invoice past its due date flags the stage chip and the summary
    p = db.one("SELECT id, status FROM projects ORDER BY id LIMIT 1")
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents,
                    status, due_date) VALUES (?,?,?,?,?,?)""",
        (p["id"], "overdue-test-slug", "Late", 50000, "sent", "2000-01-01"),
    )
    page = admin.get("/admin/studio/activity").text
    assert "1 overdue" in page
    # per-stage chip: the project's current stage shows "(1 overdue)" inline
    # so Kevin can see which bucket of the pipeline is stuck.
    assert 'class="warn pipeline-overdue">(1 overdue)' in page
    # paid invoices stop counting even with a past due date
    db.run("UPDATE invoices SET status='paid' WHERE id=?", (iid,))
    assert "pipeline-overdue" not in admin.get("/admin/studio/activity").text
    db.run("DELETE FROM invoices WHERE id=?", (iid,))


def test_marketing_site(admin, monkeypatch):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    a = db.one("SELECT * FROM assets WHERE gallery_id=?", (g["id"],))
    monkeypatch.setattr(config, "DEMO_GALLERY_SLUG", g["slug"])
    monkeypatch.setattr(config, "DEMO_GALLERY_PIN", g["pin"])

    with TestClient(app) as pub:
        # marketing pages render and are indexable; everything else stays noindex
        marketing_paths = (
            "/",
            "/real-estate",
            "/portraits",
            "/food-beverage",
            "/portfolio",
            "/about",
            "/contact",
            "/book",
            "/work",
            "/services",
            "/reels",
            "/press",
        )

        mobile_destinations = (
            "/real-estate",
            "/portraits",
            "/food-beverage",
            "/portfolio",
            "/services",
            "/work",
            "/about",
            "/book",
            "/contact",
        )
        for path in marketing_paths:
            r = pub.get(path)
            assert r.status_code == 200
            assert "x-robots-tag" not in r.headers, path
            assert 'content="index, follow"' in r.text
            assert "<title>" in r.text and '<meta name="description"' in r.text
            assert f'<link rel="canonical" href="{config.BASE_URL}{path}">' in r.text
            # marketing shell skips admin/client JS (~70KB) — HTMX stays on cream
            assert "htmx.min.js" not in r.text, path
            assert "behaviors.js" not in r.text, path
            assert "details_persist.js" not in r.text, path
            menu_start = '<details class="nav-menu" data-mobile-menu>'
            assert menu_start in r.text, path
            after_header = r.text.split("</header>", 1)[1]
            assert after_header.lstrip().startswith(menu_start), path
            menu = r.text.split(menu_start, 1)[1].split("</details>", 1)[0]
            assert menu.lstrip().startswith(
                '<summary class="nav-menu-btn" aria-label="Site menu">'
            ), path
            assert menu.count("<summary") == 1, path
            assert '<nav class="nav-mobile" aria-label="Mobile site">' in menu, path
            assert "aria-expanded=" not in menu and "data-menu-btn" not in menu, path
            mobile_nav = menu.split('aria-label="Mobile site">', 1)[1].split("</nav>", 1)[0]
            mobile_hrefs = tuple(re.findall(r'<a\b[^>]*\bhref="([^"]+)"', mobile_nav))
            assert mobile_hrefs == mobile_destinations, path
        # Lean marketing JS retains the few booking/spoke handlers public pages need.
        js = pub.get("/static/site.js").text
        assert "select[data-autosubmit]" in js
        assert "form[data-confirm]" in js
        assert 'closest("[data-seek]")' in js
        assert 'typeof window.plausible !== "function"' in js
        assert "video[data-reel-video]" in js and "IntersectionObserver" in js
        menu_js = js.split("// --- mobile menu ---", 1)[1].split("// --- scroll reveal ---", 1)[0]
        for contract in (
            'mobileMenu.querySelector("summary")',
            'mobileMenu.addEventListener("focusin"',
            'mobileMenu.addEventListener("toggle"',
            'document.documentElement.classList.add("nav-menu-enhanced")',
            'document.documentElement.classList.add("nav-menu-open")',
            'document.documentElement.classList.remove("nav-menu-open")',
            "setBackgroundInert(true)",
            "setBackgroundInert(false)",
            'element.setAttribute("inert", "")',
            'element.removeAttribute("inert")',
            "requestAnimationFrame",
            "firstLink.focus()",
            'e.key === "Tab"',
            "e.shiftKey",
            "document.activeElement",
            'e.key === "Escape"',
            "menuButton.focus()",
            'closest("a[href]")',
            'window.addEventListener("resize"',
            'window.getComputedStyle(menuButton).display === "none"',
            "lastMenuFocus",
            "focusWasInMenu",
            "matched = Array.prototype.some.call",
            "if (!matched && focusWasInMenu && nav)",
            "restoreDesktopFocus(active)",
            "if (restoreFocus) restoreDesktopFocus(active)",
            'window.addEventListener("pageshow"',
            "syncMenuState(false)",
        ):
            assert contract in menu_js
        assert "aria-expanded" not in menu_js
        assert 'classList.toggle("open"' not in menu_js
        assert "document.body.style.overflow" not in menu_js
        assert "summaryHadFocus" not in menu_js

        focus_capture = menu_js.index("var active = document.activeElement;")
        style_read = menu_js.index('window.getComputedStyle(menuButton).display === "none"')
        assert focus_capture < style_read
        enhancement = menu_js.index('classList.add("nav-menu-enhanced")')
        for listener in (
            'mobileMenu.addEventListener("toggle"',
            'document.addEventListener("keydown"',
            'window.addEventListener("resize"',
            'window.addEventListener("pageshow"',
        ):
            assert menu_js.index(listener) < enhancement

        def css_block(source, marker):
            start = source.index(marker)
            brace = source.index("{", start)
            depth = 0
            for index in range(brace, len(source)):
                if source[index] == "{":
                    depth += 1
                elif source[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[start : index + 1]
            raise AssertionError(f"Unclosed CSS block: {marker}")

        legacy_css = pub.get("/static/mise.css").text
        screening_css = pub.get("/static/screening.css").text
        assert ".nav-menu[open] > .nav-mobile { display: flex; }" in legacy_css
        assert ".sr .nav-menu[open] > .nav-mobile { display: flex; }" in screening_css
        assert ".nav-menu-btn::-webkit-details-marker { display: none; }" in legacy_css
        assert ".nav-mobile.open" not in legacy_css
        assert ".nav-mobile.open" not in screening_css

        legacy_fallback = css_block(legacy_css, ".nav-mobile {")
        legacy_enhanced = css_block(legacy_css, "html.nav-menu-enhanced .nav-mobile {")
        screening_fallback = css_block(screening_css, ".sr .nav-mobile {")
        screening_enhanced = css_block(screening_css, "html.nav-menu-enhanced .sr .nav-mobile {")
        for fallback in (legacy_fallback, screening_fallback):
            assert "position: relative" in fallback
            assert "min-height: 100dvh" in fallback
            assert "overflow-y: auto" not in fallback
        for enhanced in (legacy_enhanced, screening_enhanced):
            assert "position: fixed" in enhanced
            assert "height: 100dvh" in enhanced
            assert "overflow-y: auto" in enhanced
        assert ".nav-mobile > a:first-child { margin-top: auto; }" in legacy_css
        assert ".nav-mobile > a:last-child { margin-bottom: auto; }" in legacy_css
        assert "z-index: 80" in css_block(legacy_css, ".lightbox {")
        scroll_lock = css_block(legacy_css, "html.nav-menu-open,")
        assert "overflow: hidden" in scroll_lock
        assert "overscroll-behavior: none" in scroll_lock

        legacy_mobile_css = css_block(legacy_css, "@media (max-width: 860px)")
        screening_mobile_css = css_block(screening_css, "@media (max-width: 1080px)")
        assert ".site-nav::after" in legacy_mobile_css
        assert ".sr .site-nav::after" in screening_mobile_css
        assert ".nav-menu { display: block; }" in legacy_mobile_css
        assert ".sr .nav-menu { display: block; }" in screening_mobile_css
        home = pub.get("/").text
        assert 'href="/book">Book a shoot' in home
        assert "Ready for your close-up?" in home
        book = pub.get("/book").text
        assert "Instant confirmation" in book
        assert "Calendar invite in your inbox" in book
        assert "x-robots-tag" in pub.get(f"/g/{g['slug']}").headers
        # client gallery still gets the HTMX shell via base_cream → base.html
        assert "htmx.min.js" in pub.get(f"/g/{g['slug']}").text
        r = pub.get("/robots.txt")
        assert r.status_code == 200 and "Disallow: /g/" in r.text
        assert "x-robots-tag" not in r.headers
        assert "Sitemap: " in r.text and "/sitemap.xml" in r.text

        # sitemap lists exactly the indexable pages, crawlable itself
        r = pub.get("/sitemap.xml")
        assert r.status_code == 200 and "xml" in r.headers["content-type"]
        assert "x-robots-tag" not in r.headers
        from app import config as cfg

        for path in marketing_paths:
            assert f"<loc>{cfg.BASE_URL}{path}</loc>" in r.text
        assert "<lastmod>" in r.text
        assert "/g/" not in r.text and "/admin" not in r.text

        # OG card present, but no og:image while nothing is starred
        r = pub.get("/")
        assert 'property="og:title"' in r.text and "og:image" not in r.text
        assert 'content="summary"' in r.text

        # portfolio gating: unflagged asset is not served publicly
        assert pub.get(f"/site/img/{a['id']}").status_code == 404
        admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/portfolio", follow_redirects=False)
        assert db.one("SELECT portfolio FROM assets WHERE id=?", (a["id"],))["portfolio"] == 1
        assert pub.get(f"/site/img/{a['id']}").status_code == 200
        # tiles carry data-web for the lightbox, and the overlay ships with the page
        r = pub.get("/portfolio")
        assert f'data-web="/site/img/{a["id"]}?variant=web"' in r.text
        assert 'id="lightbox"' in r.text and "lightbox.js" in r.text
        # slideshow ▶ ships on the marketing lightbox too (fav/dl stay gallery-only)
        lightbox_tag = re.search(r'<div id="lightbox"[^>]*>', r.text, re.S).group(0)
        assert 'aria-label="Media viewer"' in lightbox_tag
        assert 'class="lb-play" aria-label="Slideshow" aria-pressed="false"' in r.text
        assert 'class="lb-fav"' not in r.text
        r = pub.get("/")
        assert f'data-web="/site/img/{a["id"]}"' in r.text
        assert 'id="lightbox"' in r.text
        assert 'data-analytics-event="Demo Gallery Click"' in r.text
        # starred photo becomes the OG share image
        assert f'property="og:image" content="{cfg.BASE_URL}/site/img/{a["id"]}"' in r.text
        assert 'content="summary_large_image"' in r.text
        # toggle off hides it again
        admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/portfolio", follow_redirects=False)
        assert pub.get(f"/site/img/{a['id']}").status_code == 404


def test_inquiry_form(monkeypatch):
    from app import config, jobs, mailer

    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    # Owner notify is a durable job; keep pool off so we drain inline.
    monkeypatch.setattr(jobs, "start", lambda: None)
    monkeypatch.setattr(jobs, "_pool", None)
    sent = []
    monkeypatch.setattr(
        mailer,
        "send",
        lambda to, subject, body, reply_to="": sent.append((to, subject, body, reply_to)),
    )

    def _drain_owner_email():
        row = db.one(
            "SELECT id, status FROM jobs WHERE kind='inquiry_owner_email' ORDER BY id DESC LIMIT 1"
        )
        assert row is not None
        if row["status"] == "done":
            return
        if row["status"] != "queued":
            db.run(
                "UPDATE jobs SET status='queued', attempts=0, error=NULL WHERE id=?",
                (row["id"],),
            )
        jobs._execute(row["id"])

    with TestClient(app) as pub:
        # honeypot filled → pretend success, store nothing, send nothing
        r = pub.post(
            "/contact",
            data={
                "name": "Bot",
                "email": "b@spam.com",
                "message": "buy now",
                "website": "spam.com",
            },
        )
        assert r.status_code == 200 and "Thanks" in r.text
        assert db.one("SELECT COUNT(*) AS n FROM inquiries")["n"] == 0 and not sent

        # bad email rejected — and every typed value is echoed back so the
        # visitor never loses their quote request to a typo (dotless domain
        # passes the browser's type=email check but fails the server's)
        r = pub.post(
            "/contact",
            data={
                "name": "Sam Owner",
                "email": "sam@localhost",
                "phone": "(828) 555-0199",
                "business": "Taqueria Luz",
                "message": "Need a menu shoot in July.",
                "service": "Food & Beverage",
                "dish_count": "12 dishes",
                "usage": "Not sure",
                "budget": "Under $1,000",
            },
        )
        assert r.status_code == 400
        assert 'value="Sam Owner"' in r.text and 'value="sam@localhost"' in r.text
        assert 'value="(828) 555-0199"' in r.text
        assert 'value="Taqueria Luz"' in r.text and "Need a menu shoot in July." in r.text
        assert 'value="12 dishes"' in r.text
        assert "<span data-scope-label>Dishes / setups</span>" in r.text
        # selects re-select the chosen option (specialty options since the revamp)
        assert "selected>Food &amp; Beverage — photo / video</option>" in r.text
        assert '<option value="Not sure" selected' in r.text
        assert '<option value="Under $1,000" selected' in r.text
        # nothing was stored for the rejected submission
        assert db.one("SELECT COUNT(*) AS n FROM inquiries")["n"] == 0

        # real inquiry: stored immediately; visitor ack is sync; owner notify is a job
        r = pub.post(
            "/contact",
            data={
                "name": "Sam Owner",
                "email": "sam@taqueria.com",
                "phone": "828-555-0102",
                "business": "Taqueria Luz",
                "message": "Need a menu shoot in July.",
                "service": "Real Estate",
                "dish_count": "3,200 sq ft · 4 bed 3 bath",
            },
        )
        assert r.status_code == 200 and "Thanks" in r.text
        assert 'data-analytics-view="Contact Success"' in r.text
        assert 'href="/book">Need a time now?' in r.text
        q = db.one("SELECT * FROM inquiries ORDER BY id DESC LIMIT 1")
        assert q["name"] == "Sam Owner" and q["emailed"] == 0
        assert q["phone"] == "828-555-0102"
        assert "Listing / property scope: 3,200 sq ft · 4 bed 3 bath" in q["message"]
        assert "Dishes / setups" not in q["message"]
        # Visitor acknowledgement is best-effort on the request path.
        ack_to, ack_subject, ack_body, ack_reply_to = sent[0]
        assert ack_to == "sam@taqueria.com" and ack_reply_to == ""
        assert "what happens next" in ack_subject.lower()
        assert f"{config.BASE_URL}/services" in ack_body
        assert f"{config.BASE_URL}/book" in ack_body
        _drain_owner_email()
        q = db.one("SELECT * FROM inquiries WHERE id=?", (q["id"],))
        assert q["emailed"] == 1 and q["owner_email_delivered_at"]
        to, subject, body, reply_to = sent[1]
        assert to == "kevin@example.com" and reply_to == "sam@taqueria.com"
        assert "Taqueria Luz" in body and "menu shoot" in body and "828-555-0102" in body
        assert "Listing / property scope" in body and "Dishes / setups" not in body

        # Visitor acknowledgement is best-effort and does not change the
        # successful response or the admin notification's emailed flag.
        delivered = []

        def fail_ack(to, subject, body, reply_to=""):
            delivered.append((to, subject, body, reply_to))
            if to == "pat@cafe.com":
                raise OSError("visitor delivery failed")

        monkeypatch.setattr(mailer, "send", fail_ack)
        r = pub.post(
            "/contact",
            data={"name": "Pat", "email": "pat@cafe.com", "message": "Brand partner info?"},
        )
        assert r.status_code == 200 and "Thanks" in r.text
        q = db.one("SELECT * FROM inquiries ORDER BY id DESC LIMIT 1")
        assert q["name"] == "Pat" and q["emailed"] == 0 and q["phone"] == ""
        # Request path only attempted visitor ack (failed); owner notify is the job.
        assert [call[0] for call in delivered] == ["pat@cafe.com"]
        _drain_owner_email()
        q = db.one("SELECT * FROM inquiries WHERE id=?", (q["id"],))
        assert q["emailed"] == 1
        assert [call[0] for call in delivered] == ["pat@cafe.com", "kevin@example.com"]

        # Total SMTP failure: row kept with emailed=0, visitor still thanked.
        def boom(*a, **kw):
            raise OSError("smtp down")

        monkeypatch.setattr(mailer, "send", boom)
        r = pub.post(
            "/contact",
            data={"name": "Lee", "email": "lee@cafe.com", "message": "Portrait session?"},
        )
        assert r.status_code == 200 and "Thanks" in r.text
        q = db.one("SELECT * FROM inquiries ORDER BY id DESC LIMIT 1")
        assert q["name"] == "Lee" and q["emailed"] == 0
        _drain_owner_email()
        q = db.one("SELECT * FROM inquiries WHERE id=?", (q["id"],))
        assert q["emailed"] == 0
        assert q["owner_email_failure_category"] == "smtp_error"


def test_inquiries_admin_view(admin):
    iid = db.run(
        "INSERT INTO inquiries (name, email, business, message, emailed) VALUES (?,?,?,?,0)",
        ("Robin Che", "robin@bistro.com", "Bistro Vert", "Spring menu?"),
    )

    # the inquiry surfaces in the unified inbox; selecting it shows the business
    # as the thread name, the contact name beneath, and the real convert action.
    r = admin.get(f"/admin/inbox?sel={iid}")
    assert r.status_code == 200
    assert "Bistro Vert" in r.text and "Robin Che" in r.text and "Spring menu?" in r.text
    assert f'action="/admin/studio/inquiries/{iid}/client"' in r.text
    assert "Create client &amp; project" in r.text

    # one click creates a client carrying the inquiry context
    r = admin.post(f"/admin/studio/inquiries/{iid}/client", follow_redirects=False)
    assert r.status_code == 303
    c = db.one("SELECT * FROM clients WHERE email='robin@bistro.com'")
    assert c["name"] == "Robin Che" and c["company"] == "Bistro Vert"
    assert "Spring menu?" in c["notes"]
    assert r.headers["location"] == f"/admin/studio/clients/{c['id']}"
    # the inquiry now carries a converted_at timestamp + client backref
    conv = db.one(
        "SELECT converted_at, converted_client_id, converted_project_id FROM inquiries WHERE id=?",
        (iid,),
    )
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
    r = admin.post(f"/admin/studio/inquiries/{iid}/unconvert", follow_redirects=False)
    assert r.status_code == 303
    conv = db.one(
        "SELECT converted_at, converted_client_id, converted_project_id FROM inquiries WHERE id=?",
        (iid,),
    )
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


def test_inbox_deep_link_selects_matching_thread_outside_list_window(admin):
    """An explicit thread link must never substitute the first visible inquiry.

    Dashboard/activity links carry an inquiry id, but the sidebar is bounded to
    100 rows. Seed 100 newer rows so the requested active inquiry falls outside
    that window; its conversation and action ownership must still be selected.
    """
    marker = "deep-link-window-test"
    with db.tx() as con:
        target_id = con.execute(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?, ?, ?, 'contact', '2000-01-01 00:00:00')""",
            ("Requested Old Lead", f"target@{marker}.test", "Requested thread body"),
        ).lastrowid
        con.executemany(
            """INSERT INTO inquiries (name, email, message, kind, created_at)
               VALUES (?, ?, 'decoy body', 'contact', '2099-01-01 00:00:00')""",
            [(f"Newer Decoy {i}", f"decoy-{i}@{marker}.test") for i in range(100)],
        )

    try:
        page = admin.get(f"/admin/inbox?tab=all&sel={target_id}")
        assert page.status_code == 200
        assert "Requested Old Lead" in page.text
        assert "Requested thread body" in page.text
        assert f'action="/admin/studio/inquiries/{target_id}/quote"' in page.text
        assert f'href="/admin/inbox?tab=all&sel={target_id}" class="ib-row is-active"' in page.text
        assert page.text.count('class="ib-row ') == 100

        # Selection is still tab-scoped: after archive, the stale all-tab URL
        # advances to a visible row instead of pinning an out-of-filter record.
        db.run("UPDATE inquiries SET dismissed_at=datetime('now') WHERE id=?", (target_id,))
        filtered = admin.get(f"/admin/inbox?tab=all&sel={target_id}")
        assert "Requested thread body" not in filtered.text
        assert f'action="/admin/studio/inquiries/{target_id}/quote"' not in filtered.text

        archived = admin.get(f"/admin/inbox?tab=archived&sel={target_id}")
        assert "Requested thread body" in archived.text
        assert (
            f'href="/admin/inbox?tab=archived&sel={target_id}" '
            'class="ib-row is-active"' in archived.text
        )
    finally:
        db.run("DELETE FROM inquiries WHERE email LIKE ?", (f"%@{marker}.test",))


def test_booking_flow(monkeypatch, admin):
    # The public booking surface is the scheduler: GET /book lists event types,
    # GET /book/{slug} renders a slot picker, POST /book/{slug} claims a slot.
    # A confirmed real-shoot booking find-or-creates a Studio client + project and
    # emails both sides. (The old free-text inquiry form at bare /book is gone.)
    import datetime as dt

    from app import config, mailer
    from app import scheduling as S

    monkeypatch.setattr(config, "GMAIL_USER", "kevin@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda to, subject, body, reply_to="", ics=None: sent.append((to, subject, body, reply_to)),
    )

    # Seed a real-shoot event type (creates_notion_session=1 → spawns a project)
    # with Mon–Fri 9:00–17:00 availability and 1h notice so a near slot is open.
    eid = db.run(
        """INSERT INTO event_types
        (slug, name, duration_min, min_notice_hours, booking_window_days,
         max_per_day, creates_notion_session, location, active)
        VALUES (?,?,?,?,?,?,?,?,1)""",
        ("fb-shoot", "Food Shoot", 60, 1, 60, 0, 1, "On-site"),
    )
    for wd in range(5):
        db.run(
            "INSERT INTO availability_rules (event_type_id, weekday, start_min, "
            "end_min) VALUES (?,?,?,?)",
            (eid, wd, 540, 1020),
        )

    et = S.event_by_slug("fb-shoot")
    day = dt.date.today() + dt.timedelta(days=3)  # ≥3d out so notice never clips
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    slots = S.slots_for_day(et, day)
    assert slots, "seed produced no open slots"
    start = slots[0]["utc"]

    def n_bookings(status=None):
        if status:
            return db.one(
                "SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=? AND status=?",
                (eid, status),
            )["n"]
        return db.one("SELECT COUNT(*) AS n FROM bookings WHERE event_type_id=?", (eid,))["n"]

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
        r = pub.post(
            "/book/fb-shoot",
            data={"name": "Mara", "email": "not-email", "start": start, "tz": "America/New_York"},
        )
        assert r.status_code == 400
        assert n_bookings() == 0

        # honeypot pretends success silently (303 → /book), nothing stored
        r = pub.post(
            "/book/fb-shoot",
            data={
                "name": "Bot",
                "email": "b@spam.com",
                "start": start,
                "tz": "America/New_York",
                "website": "spam.com",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/book"
        assert n_bookings() == 0

        # happy path: 303 → /booking/{token}; booking confirmed + F&B intake stored
        r = pub.post(
            "/book/fb-shoot",
            data={
                "name": "Mara Che",
                "email": "Booking-Flow@Test.cafe",
                "phone": "555-0100",
                "start": start,
                "tz": "America/New_York",
                "notes": "Spring menu launch.",
                "venue_address": "12 Vine St",
                "dish_count": "40",
                "parking_notes": "Loading dock out back",
                "style_refs": "bright, airy",
                "onsite_contact": "Lou 555-0199",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"].startswith("/booking/")
        token = r.headers["location"].rsplit("/", 1)[-1]
        b = db.one("SELECT * FROM bookings WHERE token=?", (token,))
        assert b and b["status"] == "confirmed" and b["start_utc"] == start
        assert b["email"] == "booking-flow@test.cafe"  # normalized to lowercase
        assert b["venue_address"] == "12 Vine St" and b["dish_count"] == "40"
        # confirmation emails fired to both client and Kevin
        assert any(to == "booking-flow@test.cafe" for to, *_ in sent)
        assert any(to == "kevin@example.com" for to, *_ in sent)
        manage = pub.get(f"/booking/{token}")
        assert 'data-analytics-view="Booking Completion"' in manage.text
        assert "data-analytics-once" in manage.text

        # double-book the same instant → 409 (slot taken), no second booking
        submitted = {
            "name": "Dup Visitor",
            "email": "Dup@cafe.com",
            "phone": "555-0188",
            "notes": "Keep the blue napkins.",
            "start": start,
            "tz": "America/New_York",
            "venue_address": "88 Race Lane",
            "dish_count": "12 plated dishes",
            "parking_notes": "Use the west loading bay",
            "style_refs": "moody evening references",
            "onsite_contact": "Dee 555-0142",
        }
        r = pub.post(
            "/book/fb-shoot",
            data=submitted,
        )
        assert r.status_code == 409
        assert n_bookings("confirmed") == 1
        assert f'value="{start}"' in r.text
        assert day.isoformat() in r.text
        assert slots[0]["label"] in r.text
        for value in (
            submitted["name"],
            submitted["email"],
            submitted["phone"],
            submitted["notes"],
            submitted["venue_address"],
            submitted["dish_count"],
            submitted["parking_notes"],
            submitted["style_refs"],
            submitted["onsite_contact"],
        ):
            assert value in r.text

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
    gid_draft = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("unlinked-draft-1", "Loose draft", "1234"),
    )
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
    r = admin.post(
        f"/admin/galleries/{gid_draft}/link-client",
        data={"client_id": str(cid)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid_draft,))["client_id"] == cid
    assert n_warned() == baseline
    page = admin.get("/admin/home").text
    assert f"/admin/galleries/{gid_draft}/link-client" not in page
    # link-client refuses bogus client_id; the gallery's client_id isn't touched
    r = admin.post(
        f"/admin/galleries/{gid_draft}/link-client",
        data={"client_id": "999999"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid_draft,))["client_id"] == cid

    # ship #53's force-delete of a client unlinks galleries → count returns
    admin.post(f"/admin/studio/clients/{cid}/delete", data={"force": "1"}, follow_redirects=False)
    assert db.one("SELECT client_id FROM galleries WHERE id=?", (gid_draft,))["client_id"] is None
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
    db.run(
        "INSERT INTO brand_assets (client_id, filename, stored, bytes) VALUES (?,?,?,?)",
        (cid2, "logo.png", "logo.png", 1),
    )
    r = admin.post(f"/admin/studio/clients/{cid2}/delete", follow_redirects=False)
    assert r.status_code == 400
    assert "brand asset" in r.json()["detail"]
    assert db.one("SELECT id FROM clients WHERE id=?", (cid2,)) is not None
    r = admin.post(
        f"/admin/studio/clients/{cid2}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (cid2,)) is None
    assert not bdir.exists()  # disk cleanup

    # client with linked gallery: refused; force unlinks the gallery (no
    # ON DELETE clause on galleries.client_id → manual UPDATE).
    cid3 = db.run("INSERT INTO clients (name) VALUES (?)", ("Linked Co",))
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, client_id) VALUES (?,?,?,?)",
        ("client-del-glink", "Linked", "1234", cid3),
    )
    r = admin.post(f"/admin/studio/clients/{cid3}/delete", follow_redirects=False)
    assert r.status_code == 400 and "linked galler" in r.json()["detail"]
    r = admin.post(
        f"/admin/studio/clients/{cid3}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert db.one("SELECT id FROM clients WHERE id=?", (cid3,)) is None
    # gallery survives, unlinked
    surv = db.one("SELECT client_id FROM galleries WHERE id=?", (gid,))
    assert surv is not None and surv["client_id"] is None
    db.run("DELETE FROM galleries WHERE id=?", (gid,))  # tidy up

    # client with portal visits: blocked + listed
    cid4 = db.run("INSERT INTO clients (name) VALUES (?)", ("Visited Co",))
    db.run(
        "INSERT INTO portals (client_id, slug, pin, visits) VALUES (?,?,?,5)",
        (cid4, "client-del-portal", "1234"),
    )
    r = admin.post(f"/admin/studio/clients/{cid4}/delete", follow_redirects=False)
    assert r.status_code == 400 and "portal with 5 visits" in r.json()["detail"]
    # client detail page shows the same blockers + a button that carries force
    page = admin.get(f"/admin/studio/clients/{cid4}").text
    assert "portal with 5 visits" in page
    assert 'name="force" value="1"' in page
    # cleanup
    admin.post(f"/admin/studio/clients/{cid4}/delete", data={"force": "1"}, follow_redirects=False)

    # client with favorites in a linked gallery
    cid5 = db.run("INSERT INTO clients (name) VALUES (?)", ("Faved Co",))
    gid5 = db.run(
        "INSERT INTO galleries (slug, title, pin, client_id, published) VALUES (?,?,?,?,1)",
        ("client-del-favs", "Faved", "1234", cid5),
    )
    aid5 = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (gid5, "photo", "f.jpg", "favfile.jpg", "ready"),
    )
    vid5 = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid5, "vtok-cldel"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid5, aid5))
    r = admin.post(f"/admin/studio/clients/{cid5}/delete", follow_redirects=False)
    assert r.status_code == 400 and "favorite" in r.json()["detail"]
    db.run("DELETE FROM galleries WHERE id=?", (gid5,))
    db.run("DELETE FROM clients WHERE id=?", (cid5,))

    # 404 on unknown id
    assert (
        admin.post("/admin/studio/clients/99999/delete", follow_redirects=False).status_code == 404
    )


def test_workspace_expired_gallery_unlinked(admin):
    # The project workspace links the delivered gallery; an expired gallery
    # 410s at /g/{slug}, so the workspace must render it unlinked with a
    # "get in touch" note rather than sending the client to a dead end.
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("WS Client",))
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, client_id, published) VALUES (?,?,?,?,1)",
        ("ws-gallery-01", "Final Delivery", "1234", cid),
    )
    pid = db.run(
        """INSERT INTO projects
           (client_id, title, gallery_id, workspace_slug, workspace_pin, workspace_published)
           VALUES (?,?,?,?,?,1)""",
        (cid, "WS Project", gid, "ws-proj-01", "2468"),
    )
    try:
        with TestClient(app) as pub:
            pub.post("/w/ws-proj-01/pin", data={"pin": "2468"}, follow_redirects=False)
            # live gallery → real link
            page = pub.get("/w/ws-proj-01").text
            assert 'href="/g/ws-gallery-01"' in page
            # expire it → card is unlinked with the re-open note
            db.run("UPDATE galleries SET expires_at='2000-01-01' WHERE id=?", (gid,))
            page = pub.get("/w/ws-proj-01").text
            assert 'href="/g/ws-gallery-01"' not in page
            assert "Expired 2000-01-01" in page and "get in touch" in page
    finally:
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))


def test_reels_never_expose_non_portfolio_videos(admin):
    # A ready client video that is NOT portfolio-starred must never surface on the
    # public /reels or home motion band: the /site/vid + /site/poster routes gate
    # on portfolio=1, so rendering it would produce black players whose src+poster
    # both 404 and leak the private asset id. _portfolio_reels() returns [] here.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("reel-priv", "Private Reels", "1234"),
    )
    aid = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio) "
        "VALUES (?,?,?,?,?,0)",
        (gid, "video", "client.mp4", "clientfile.mp4", "ready"),
    )
    try:
        with TestClient(app) as pub:
            reels = pub.get("/reels")
            assert reels.status_code == 200
            assert f"/site/vid/{aid}" not in reels.text
            assert f"/site/poster/{aid}" not in reels.text
            # /reels shows its empty state rather than a broken player
            assert "motion-feature" not in reels.text
            home = pub.get("/")
            assert f"/site/vid/{aid}" not in home.text
    finally:
        db.run("DELETE FROM assets WHERE id=?", (aid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_zip_wait_reports_failed_build(admin):
    # The wait page polls /download/zip/status; a zip_build that exhausted its
    # retries must be reported as failed so the page stops spinning forever and
    # offers a retry instead.
    import json as _json

    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("zip-fail-01", "Zip Fail", "1234"),
    )
    g = db.one("SELECT id, content_rev FROM galleries WHERE id=?", (gid,))
    try:
        with TestClient(app) as pub:
            # nothing built yet, no failed job → still waiting
            s = pub.get("/g/zip-fail-01/download/zip/status").json()
            assert s["ready"] is False and s["failed"] is False
            # a build that hit MAX_ATTEMPTS is marked failed → status surfaces it
            db.run(
                "INSERT INTO jobs (kind, payload, status) VALUES ('zip_build', ?, 'failed')",
                (_json.dumps({"gallery_id": g["id"], "rev": g["content_rev"]}),),
            )
            s = pub.get("/g/zip-fail-01/download/zip/status").json()
            assert s["ready"] is False and s["failed"] is True
    finally:
        db.run("DELETE FROM jobs WHERE json_extract(payload,'$.gallery_id')=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_drop_gallery_favorites_no_redirect_loop(admin):
    # A drop (transfer) gallery skips the email gate. download_favorites and
    # download_section used to check `not email` unconditionally, so on a drop
    # they 303'd to /download?fav=1, which 303'd back to /download/favorites —
    # an infinite loop. With the gate fixed they fall through to the normal 404.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, type, require_pin) "
        "VALUES (?,?,?,1,'drop',0)",
        ("drop-dl-01", "Drop DL", "1234"),
    )
    try:
        with TestClient(app) as pub:
            # first view of a link-only drop mints a visitor cookie
            assert pub.get("/g/drop-dl-01").status_code == 200
            # no loop: favorites falls through to 404 (no favorites), not a 303
            r = pub.get("/g/drop-dl-01/download/favorites", follow_redirects=False)
            assert r.status_code == 404
            # no loop: a missing section 404s rather than bouncing to the gate
            r = pub.get("/g/drop-dl-01/download/section/999999", follow_redirects=False)
            assert r.status_code == 404
    finally:
        db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_booking_manage_shows_client_timezone(admin):
    # The confirmation page must show the time in the zone the client booked in
    # (bookings.tz), not the studio's. 17:00 UTC is 10:00 AM in LA but 1:00 PM
    # in the studio's Eastern zone — showing the latter is a missed-appointment
    # trap since the picker sold the slot in the client's zone.
    eid = db.run(
        "INSERT INTO event_types (slug, name, duration_min, active) VALUES (?,?,?,1)",
        ("tz-shoot", "TZ Shoot", 60),
    )
    bid = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, start_utc, end_utc, status, tz)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "TzManage01",
            eid,
            "Pat",
            "pat@x.com",
            "2026-08-15 17:00:00",
            "2026-08-15 18:00:00",
            "confirmed",
            "America/Los_Angeles",
        ),
    )
    try:
        with TestClient(app) as pub:
            page = pub.get("/booking/TzManage01").text
            assert "10:00 AM" in page  # client's LA zone
            assert "1:00 PM" not in page  # NOT the studio's Eastern zone
            assert "America/Los_Angeles" in page  # zone labelled
    finally:
        db.run("DELETE FROM bookings WHERE id=?", (bid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


@pytest.mark.parametrize(
    ("hours_out", "kind", "flag", "subject_phrase"),
    [
        (36, "48h", "reminded_48h", "about two days"),
        (12, "24h", "reminded_24h", "tomorrow"),
    ],
)
def test_booking_reminder_sweeps_once(monkeypatch, hours_out, kind, flag, subject_phrase):
    import datetime as dt

    from app import booking_reminders, mailer

    now = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.UTC)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    eid = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        (f"reminder-{kind}", f"Reminder {kind}", 60),
    )
    start = now + dt.timedelta(hours=hours_out)
    bid = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, start_utc, end_utc, tz)
           VALUES (?,?,?,?,?,?,?)""",
        (
            f"ReminderToken{kind}",
            eid,
            "Remy",
            "remy@example.com",
            start.strftime("%Y-%m-%d %H:%M:%S"),
            (start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "America/New_York",
        ),
    )
    sent = []
    monkeypatch.setattr(booking_reminders.dt, "datetime", FrozenDateTime)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda *args, **kwargs: sent.append((args, kwargs)))
    try:
        booking_reminders.sweep()
        booking_reminders.sweep()

        row = db.one(
            "SELECT reminded_48h, reminded_24h FROM bookings WHERE id=?",
            (bid,),
        )
        assert row[flag] == 1
        assert row["reminded_24h" if flag == "reminded_48h" else "reminded_48h"] == 0
        assert len(sent) == 1
        assert subject_phrase in sent[0][0][1]
    finally:
        db.run("DELETE FROM bookings WHERE id=?", (bid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_client_booking_cancel_route(monkeypatch):
    from app import booking_notify

    eid = db.run(
        "INSERT INTO event_types (slug, name, duration_min) VALUES (?,?,?)",
        ("client-cancel", "Client Cancel", 60),
    )
    bid = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, start_utc, end_utc)
           VALUES (?,?,?,?,?,?)""",
        (
            "ClientCancelToken",
            eid,
            "Casey",
            "casey@example.com",
            "2026-09-10 14:00:00",
            "2026-09-10 15:00:00",
        ),
    )
    notified = []
    monkeypatch.setattr(booking_notify, "cancelled", lambda booking_id: notified.append(booking_id))
    try:
        with TestClient(app) as pub:
            response = pub.post(
                "/booking/ClientCancelToken/cancel",
                data={"reason": "Plans changed"},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/booking/ClientCancelToken"
        booking = db.one("SELECT status, cancel_reason FROM bookings WHERE id=?", (bid,))
        assert booking["status"] == "cancelled"
        assert booking["cancel_reason"] == "Plans changed"
        assert notified == [bid]
    finally:
        db.run("DELETE FROM bookings WHERE id=?", (bid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_client_booking_reschedule_leaves_one_confirmed(monkeypatch):
    import datetime as dt

    from app import booking_notify
    from app import scheduling as S
    from app.public import scheduling as public_scheduling

    day = dt.date(2026, 9, 14)
    eid = db.run(
        """INSERT INTO event_types
           (slug, name, duration_min, slot_step_min, min_notice_hours,
            booking_window_days, max_per_day)
           VALUES (?,?,?,?,?,?,?)""",
        ("client-reschedule", "Client Reschedule", 60, 60, 0, 365, 1),
    )
    db.run(
        """INSERT INTO availability_rules
           (event_type_id, weekday, start_min, end_min) VALUES (?,?,?,?)""",
        (eid, day.weekday(), 540, 780),
    )
    old_id = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, start_utc, end_utc, tz)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "OldRescheduleToken",
            eid,
            "Riley",
            "riley@example.com",
            "2026-09-14 13:00:00",
            "2026-09-14 14:00:00",
            "America/New_York",
        ),
    )
    confirmed = []
    monkeypatch.setattr(S, "now_utc", lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
    monkeypatch.setattr(public_scheduling, "_today_local", lambda: dt.date(2026, 9, 1))
    monkeypatch.setattr(
        S.gcal,
        "free_busy",
        lambda start, end: S.gcal.FreeBusyQuery(intervals=[], unavailable=False),
    )
    monkeypatch.setattr(booking_notify, "confirm", lambda booking_id: confirmed.append(booking_id))
    try:
        with TestClient(app) as pub:
            picker = pub.get(
                "/booking/OldRescheduleToken/reschedule?day=2026-09-14",
            )
            assert picker.status_code == 200
            assert "start=2026-09-14%2015%3A00%3A00" in picker.text

            rejected = pub.post(
                "/booking/OldRescheduleToken/reschedule",
                data={"start": "2026-09-14 14:30:00", "tz": "America/New_York"},
                follow_redirects=False,
            )
            assert rejected.status_code == 409
            assert "start=2026-09-14%2015%3A00%3A00" in rejected.text
            assert (
                db.one("SELECT status FROM bookings WHERE id=?", (old_id,))["status"] == "confirmed"
            )
            assert confirmed == []

            response = pub.post(
                "/booking/OldRescheduleToken/reschedule",
                data={"start": "2026-09-14 15:00:00", "tz": "America/New_York"},
                follow_redirects=False,
            )

        assert response.status_code == 303
        new_token = response.headers["location"].rsplit("/", 1)[-1]
        rows = db.all_(
            "SELECT id, token, status, reschedule_of FROM bookings WHERE event_type_id=?",
            (eid,),
        )
        assert sum(row["status"] == "confirmed" for row in rows) == 1
        assert db.one("SELECT status FROM bookings WHERE id=?", (old_id,))["status"] == "cancelled"
        new = next(row for row in rows if row["token"] == new_token)
        assert new["status"] == "confirmed" and new["reschedule_of"] == old_id
        assert confirmed == [new["id"]]

        compensated_token = "CompensatedRescheduleToken"
        db.run(
            """INSERT INTO bookings
                 (token, event_type_id, name, email, start_utc, end_utc, status,
                  reschedule_of, cancel_reason)
               VALUES (?,?,?,?,?,?,'cancelled',?,?)""",
            (
                compensated_token,
                eid,
                "Riley",
                "riley@example.com",
                "2026-09-14 17:00:00",
                "2026-09-14 18:00:00",
                old_id,
                "Reschedule failed — replacement rolled back",
            ),
        )

        with TestClient(app) as pub:
            old_manage = pub.get("/booking/OldRescheduleToken", follow_redirects=False)
            old_invite = pub.get("/booking/OldRescheduleToken/invite.ics", follow_redirects=False)
            new_manage = pub.get(f"/booking/{new_token}")
            invite = pub.get(f"/booking/{new_token}/invite.ics")
            compensated_invite = pub.get(
                f"/booking/{compensated_token}/invite.ics", follow_redirects=False
            )
        assert old_manage.status_code == 200
        assert "Booking cancelled" in old_manage.text
        assert "Check your latest confirmation" in old_manage.text
        assert "Book another time" not in old_manage.text
        assert new_token not in old_manage.text
        assert old_invite.status_code == 410
        assert new_token not in old_invite.text
        assert "calendar.google.com" in new_manage.text
        assert "remove or edit that old entry" in new_manage.text
        assert compensated_invite.status_code == 410
        assert new_token not in compensated_invite.text
        assert invite.status_code == 200
        assert f"UID:mise-booking-{old_id}@kleephotography.com" in invite.text
        assert "SEQUENCE:1" in invite.text
        db.run("DELETE FROM bookings WHERE token=?", (compensated_token,))

        # A malformed legacy lineage with two confirmed branches must not
        # publish conflicting REQUEST invites under the shared calendar UID.
        db.run("UPDATE bookings SET status='confirmed' WHERE id=?", (old_id,))
        try:
            with TestClient(app) as pub:
                split_old_invite = pub.get(
                    "/booking/OldRescheduleToken/invite.ics", follow_redirects=False
                )
                split_new_invite = pub.get(
                    f"/booking/{new_token}/invite.ics", follow_redirects=False
                )
            assert split_old_invite.status_code == 410
            assert split_new_invite.status_code == 410
        finally:
            db.run("UPDATE bookings SET status='cancelled' WHERE id=?", (old_id,))

        # A database failure while releasing the current booking must roll the
        # replacement INSERT back with it. The route may fail, but it must never
        # leave two independently confirmed bookings or fire side effects.
        con = db.connect()
        try:
            con.executescript(
                f"""CREATE TRIGGER test_route_reschedule_release_abort
                    BEFORE UPDATE OF status ON bookings
                    WHEN OLD.id={new["id"]} AND NEW.status='cancelled'
                    BEGIN
                      SELECT RAISE(ABORT, 'forced route reschedule failure');
                    END;"""
            )
            con.commit()
        finally:
            con.close()
        try:
            with TestClient(app, raise_server_exceptions=False) as pub:
                response = pub.post(
                    f"/booking/{new_token}/reschedule",
                    data={"start": "2026-09-14 16:00:00", "tz": "America/New_York"},
                    follow_redirects=False,
                )
        finally:
            db.run("DROP TRIGGER test_route_reschedule_release_abort")

        assert response.status_code == 500
        rows = db.all_(
            "SELECT id, token, status, reschedule_of FROM bookings WHERE event_type_id=?",
            (eid,),
        )
        assert len(rows) == 2
        assert sum(row["status"] == "confirmed" for row in rows) == 1
        assert next(row for row in rows if row["token"] == new_token)["status"] == "confirmed"
        assert max(row["id"] for row in rows) == new["id"]
        assert confirmed == [new["id"]]

        assert S.cancel(new_token, "Plans changed") is True
        with TestClient(app) as pub:
            old_after_leaf_cancel = pub.get("/booking/OldRescheduleToken/invite.ics")
            leaf_cancel = pub.get(f"/booking/{new_token}/invite.ics")
        assert old_after_leaf_cancel.status_code == 410
        assert leaf_cancel.status_code == 200
        assert "METHOD:CANCEL" in leaf_cancel.text
        assert "STATUS:CANCELLED" in leaf_cancel.text
        assert "SEQUENCE:2" in leaf_cancel.text
    finally:
        db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM availability_rules WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_booking_ics_download():
    eid = db.run(
        """INSERT INTO event_types (slug, name, duration_min, location)
           VALUES (?,?,?,?)""",
        ("ics-download", "ICS Portrait", 60, "Studio 4"),
    )
    bid = db.run(
        """INSERT INTO bookings
           (token, event_type_id, name, email, notes, start_utc, end_utc)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "IcsDownloadToken",
            eid,
            "Avery",
            "avery@example.com",
            "Bring two looks.",
            "2026-10-05 14:00:00",
            "2026-10-05 15:00:00",
        ),
    )
    try:
        with TestClient(app) as pub:
            response = pub.get("/booking/IcsDownloadToken/invite.ics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert response.headers["content-disposition"] == 'attachment; filename="invite.ics"'
        assert f"UID:mise-booking-{bid}@kleephotography.com" in response.text
        assert "DTSTART:20261005T140000Z" in response.text
        assert "DTEND:20261005T150000Z" in response.text
        assert "SUMMARY:ICS Portrait" in response.text
        assert "LOCATION:Studio 4" in response.text
        assert "STATUS:CONFIRMED" in response.text
    finally:
        db.run("DELETE FROM bookings WHERE id=?", (bid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))
