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


def test_specialty_pages(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    from app import config as cfg

    with TestClient(app) as pub:
        # all three spokes render, are indexable, and sit in the sitemap
        for path in ("/real-estate", "/portraits", "/food-beverage"):
            r = pub.get(path)
            assert r.status_code == 200, path
            assert "x-robots-tag" not in r.headers, path
            assert 'content="index, follow"' in r.text
            assert '"@type": "FAQPage"' in r.text  # FAQ JSON-LD rides on every spoke
        sm = pub.get("/sitemap.xml")
        for path in ("/real-estate", "/portraits", "/food-beverage"):
            assert f"<loc>{cfg.BASE_URL}{path}</loc>" in sm.text

        # nothing carries an re/ tag yet → the RE spoke falls back to its
        # specialty empty state (copy-led, not the generic include)
        r = pub.get("/real-estate")
        assert "empty-state" in r.text
        assert "Real estate work is being curated" in r.text
        # no re-shoot event type exists yet → CTA falls back to /book
        assert 'href="/book"' in r.text and 'href="/book/re-shoot"' not in r.text

        # the hub renders one feature title card per specialty, each
        # linking to its spoke (sp-door stays the stable hook class)
        r = pub.get("/")
        assert r.text.count("sp-door") == 3
        for slug in ("real-estate", "portraits", "food-beverage"):
            assert f'href="/{slug}"' in r.text

    # plant two synthetic starred photos: one RE-prefixed, one legacy-tagged
    # (unprefixed = F&B by convention)
    re_id = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio, "
        "portfolio_tag) VALUES (?,?,?,?,?,?,?)",
        (g["id"], "photo", "re.jpg", "cafefeed01cafefeed.jpg", "ready", 1, "re/exteriors"),
    )
    fb_id = db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status, portfolio, "
        "portfolio_tag) VALUES (?,?,?,?,?,?,?)",
        (g["id"], "photo", "fb.jpg", "cafefeed02cafefeed.jpg", "ready", 1, "dishes"),
    )
    try:
        with TestClient(app) as pub:
            r = pub.get("/real-estate")
            assert f'data-web="/site/img/{re_id}"' in r.text
            assert f"/site/img/{fb_id}" not in r.text
            # prefix-derived craft phrase lands in the alt text
            assert "real estate photography by" in r.text
            # the spoke og:image is its own specialty's lead asset
            assert f'property="og:image" content="{cfg.BASE_URL}/site/img/{re_id}"' in r.text
            # prefix is stripped from the visible chip/caption label
            assert "re/exteriors" not in r.text and "Exteriors" in r.text
            # with work present, the portfolio link opens pre-filtered
            assert 'href="/portfolio#sp:re"' in r.text

            r = pub.get("/food-beverage")
            assert f'data-web="/site/img/{fb_id}"' in r.text
            assert f'data-web="/site/img/{re_id}"' not in r.text

            # (grid attributes, not bare substrings — the sitewide JSON-LD
            # image may legitimately reference any starred asset on any page)
            r = pub.get("/portraits")
            assert f'data-web="/site/img/{re_id}"' not in r.text
            assert f'data-web="/site/img/{fb_id}"' not in r.text

            # the RE title card now badges its single starred frame
            r = pub.get("/")
            assert "1 still" in r.text

        # once the conventional event type exists, the spoke CTA deep-links it
        admin.post(
            "/admin/scheduling/event",
            data={"name": "Real Estate Shoot", "slug": "re-shoot", "duration_min": 120},
            follow_redirects=False,
        )
        with TestClient(app) as pub:
            assert 'href="/book/re-shoot"' in pub.get("/real-estate").text
    finally:
        db.run("DELETE FROM assets WHERE id IN (?,?)", (re_id, fb_id))
        db.run("DELETE FROM event_types WHERE slug='re-shoot'")


@pytest.mark.parametrize(
    ("slug", "tag", "heading", "label", "service"),
    [
        (
            "cta-real-estate",
            "re/exteriors",
            "Like the look? Let&#39;s shoot your listing.",
            "Request a listing quote",
            "Real%20Estate",
        ),
        (
            "cta-portrait",
            "pl/headshots",
            "Like the look? Let&#39;s plan your session.",
            "Request a portrait quote",
            "Portraits",
        ),
    ],
)
def test_case_study_cta_variants(slug, tag, heading, label, service):
    gid = db.run(
        """INSERT INTO galleries
           (slug, title, pin, cs_published, cs_tagline, cs_brief)
           VALUES (?,?,?,?,?,?)""",
        (slug, f"CTA study {slug}", "1234", 1, "A focused campaign.", "The brief."),
    )
    aid = db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, portfolio, portfolio_tag)
           VALUES (?,?,?,?,?,?,?)""",
        (gid, "photo", f"{slug}.jpg", f"{slug}.jpg", "ready", 1, tag),
    )
    try:
        with TestClient(app) as pub:
            response = pub.get(f"/work/{slug}")

        assert response.status_code == 200
        assert heading in response.text
        assert label in response.text
        assert f'href="/contact?service={service}"' in response.text
    finally:
        db.run("DELETE FROM assets WHERE id=?", (aid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_video_renditions_flow(admin):

    g, vid, photo = _ready_video(admin, title="Rendition Reel", pin="4321")
    with TestClient(app) as pub:
        # photos can't get renditions
        r = admin.post(
            f"/admin/galleries/{g['id']}/assets/{photo['id']}/renditions",
            follow_redirects=False,
        )
        assert r.status_code == 404

        # queue social cuts for the video; both preset rows appear
        r = admin.post(
            f"/admin/galleries/{g['id']}/assets/{vid['id']}/renditions",
            follow_redirects=False,
        )
        assert r.status_code == 303
        rows = db.all_("SELECT * FROM asset_renditions WHERE asset_id=?", (vid["id"],))
        assert {x["preset"] for x in rows} == {"9x16", "1x1"}

        # the job renders both from the original (real ffmpeg, no mocks)
        for _ in range(200):
            rows = db.all_("SELECT * FROM asset_renditions WHERE asset_id=?", (vid["id"],))
            if rows and all(x["status"] == "ready" for x in rows):
                break
            time.sleep(0.2)
        assert all(x["status"] == "ready" for x in rows)
        by = {x["preset"]: x for x in rows}
        assert (by["9x16"]["width"], by["9x16"]["height"]) == (1080, 1920)
        assert (by["1x1"]["width"], by["1x1"]["height"]) == (1080, 1080)
        assert all(x["bytes"] > 0 for x in rows)

        # re-running the build is idempotent — still exactly two rows
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{vid['id']}/renditions",
            follow_redirects=False,
        )
        n = db.one("SELECT COUNT(*) AS n FROM asset_renditions WHERE asset_id=?", (vid["id"],))
        assert n["n"] == 2

        # client side: tile offers the cuts once ready; download is email-gated
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "4321"}, follow_redirects=False)
        page = pub.get(f"/g/{g['slug']}").text
        rid = by["9x16"]["id"]
        assert f"/g/{g['slug']}/download/rendition/{rid}" in page
        assert ">9:16<" in page
        r = pub.get(f"/g/{g['slug']}/download/rendition/{rid}", follow_redirects=False)
        assert r.status_code == 303  # email gate first
        pub.post(
            f"/g/{g['slug']}/email",
            data={"email": "chef@bistro.com", "asset_id": vid["id"]},
            follow_redirects=False,
        )
        r = pub.get(f"/g/{g['slug']}/download/rendition/{rid}")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert "_9x16.mp4" in r.headers.get("content-disposition", "")
        assert pub.get(f"/g/{g['slug']}/download/rendition/999999").status_code == 404


def test_bulk_star_and_tag(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    ids = []
    for i in range(3):
        aid = db.run(
            "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
            (g["id"], "photo", f"bulk{i}.jpg", f"beefcafe0{i}beefcafe.jpg", "ready"),
        )
        ids.append(aid)
    try:
        # star + tag two of the three in one sweep
        r = admin.post(
            f"/admin/galleries/{g['id']}/assets/bulk-portfolio",
            data={"asset_ids": [str(ids[0]), str(ids[1])], "portfolio_tag": "re/exteriors"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        for aid in ids[:2]:
            row = db.one("SELECT portfolio, portfolio_tag FROM assets WHERE id=?", (aid,))
            assert row["portfolio"] == 1 and row["portfolio_tag"] == "re/exteriors"
        assert db.one("SELECT portfolio FROM assets WHERE id=?", (ids[2],))["portfolio"] == 0

        # starring without a tag keeps existing tags untouched
        admin.post(
            f"/admin/galleries/{g['id']}/assets/bulk-portfolio",
            data={"asset_ids": [str(ids[0])]},
            follow_redirects=False,
        )
        row = db.one("SELECT portfolio, portfolio_tag FROM assets WHERE id=?", (ids[0],))
        assert row["portfolio"] == 1 and row["portfolio_tag"] == "re/exteriors"

        # unstar sweep
        admin.post(
            f"/admin/galleries/{g['id']}/assets/bulk-portfolio",
            data={"asset_ids": [str(i) for i in ids], "mode": "unstar"},
            follow_redirects=False,
        )
        for aid in ids:
            assert db.one("SELECT portfolio FROM assets WHERE id=?", (aid,))["portfolio"] == 0

        # the bulk toolbar ships both portfolio actions
        page = admin.get(f"/admin/galleries/{g['id']}").text
        assert "bulk-portfolio" in page and "Star checked" in page and "Unstar" in page
    finally:
        db.run("DELETE FROM assets WHERE id IN (?,?,?)", (ids[0], ids[1], ids[2]))


def test_portfolio_video_tiles(admin):
    g, vid, photo = _ready_video(admin, title="Archive Motion", pin="9911")
    admin.post(f"/admin/galleries/{g['id']}/assets/{vid['id']}/portfolio", follow_redirects=False)
    admin.post(f"/admin/galleries/{g['id']}/assets/{photo['id']}/portfolio", follow_redirects=False)
    try:
        with TestClient(app) as pub:
            r = pub.get("/portfolio")
            # the starred video joins the masonry as a lightbox video tile
            assert f'data-web="/site/vid/{vid["id"]}"' in r.text
            assert 'data-kind="video"' in r.text
            assert f'data-poster="/site/poster/{vid["id"]}"' in r.text
            assert 'class="play-badge"' in r.text and 'class="dur-badge"' in r.text
            # video thumbnails serve through the thumb variant; web stays
            # photo-only (a video's web rendition is the mp4 behind /site/vid)
            assert pub.get(f"/site/img/{vid['id']}?variant=thumb").status_code == 200
            assert pub.get(f"/site/img/{vid['id']}?variant=web").status_code == 404
            assert pub.get(f"/site/vid/{vid['id']}").status_code == 200
            assert pub.get(f"/site/poster/{vid['id']}").status_code == 200

            # /reels ships VideoObject JSON-LD + the sound toggle for the reel
            r = pub.get("/reels")
            assert '"@type": "VideoObject"' in r.text
            assert f"/site/vid/{vid['id']}" in r.text
            assert "data-sound-toggle" in r.text
            assert "data-reel-video" in r.text
            assert "reel; use the sound button below for audio" in r.text
    finally:
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{vid['id']}/portfolio", follow_redirects=False
        )
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{photo['id']}/portfolio", follow_redirects=False
        )

    with TestClient(app) as pub:
        assert pub.get(f"/site/img/{vid['id']}?variant=thumb").status_code == 404
        assert pub.get(f"/site/vid/{vid['id']}").status_code == 404
        assert pub.get(f"/site/poster/{vid['id']}").status_code == 404


def test_portal_motion_band(admin):
    g, vid, photo = _ready_video(admin, title="Motion Portal", pin="7788")
    # client + portal, gallery linked to the client
    admin.post(
        "/admin/studio/clients",
        data={"name": "Motion Client", "email": "motion@example.com"},
        follow_redirects=False,
    )
    cid = db.one("SELECT id FROM clients ORDER BY id DESC LIMIT 1")["id"]
    db.run("UPDATE galleries SET client_id=? WHERE id=?", (cid, g["id"]))
    admin.post(f"/admin/studio/clients/{cid}/portal", follow_redirects=False)
    admin.post(
        f"/admin/studio/clients/{cid}/portal/publish",
        data={"published": "true"},
        follow_redirects=False,
    )
    prow = db.one("SELECT slug, pin FROM portals ORDER BY id DESC LIMIT 1")
    try:
        with TestClient(app) as pub:
            r = pub.post(
                f"/portal/{prow['slug']}/pin", data={"pin": prow["pin"]}, follow_redirects=False
            )
            assert r.status_code == 303
            page = pub.get(f"/portal/{prow['slug']}").text
            # delivered motion lists with duration + a route into the gallery
            assert "Reels &amp; films" in page
            assert f"/portal/{prow['slug']}/thumb/{vid['id']}" in page
            assert f"/g/{g['slug']}" in page
            assert 'class="dur-badge"' in page
            # the portal thumb route serves the video's thumbnail
            assert pub.get(f"/portal/{prow['slug']}/thumb/{vid['id']}").status_code == 200
    finally:
        db.run("DELETE FROM portals WHERE slug=?", (prow["slug"],))
        db.run("UPDATE galleries SET client_id=NULL WHERE id=?", (g["id"],))


def test_screening_room_rollout_flag(client, monkeypatch):
    """Foundation wiring: body.sr rides the marketing pages when the rollout
    flag is on (default), disappears when it's off, and the token layer is
    served. Old themes must render either way — the tokens are body.sr-scoped."""

    # default: flag ON — the site body carries the sr scope
    page = client.get("/").text
    assert 'class="site-body sr"' in page
    # tokens ship as their own sheet, linked from base.html
    css = client.get("/static/screening-room-tokens.css")
    assert css.status_code == 200
    assert "body.sr" in css.text
    assert "--sr-house" in css.text
    assert "/static/screening-room-tokens.css" in page
    # Plex Mono self-hosted, declared alongside the existing families
    fonts = client.get("/static/fonts.css").text
    assert "IBM Plex Mono" in fonts
    assert client.get("/static/fonts/ibm-plex-mono-500-latin.woff2").status_code == 200

    # kill switch: flag OFF — sr scope gone, page still renders
    monkeypatch.setattr(config, "SCREENING_ROOM", False)
    off = client.get("/")
    assert off.status_code == 200
    assert 'class="site-body"' in off.text
    assert " sr" not in off.text.split("<body")[1].split(">")[0]

    # the admin gate is the crew-pass ticket when the flag is on, and falls
    # back to the cream login card when it's off (auth logic untouched)
    monkeypatch.setattr(config, "SCREENING_ROOM", True)
    login = client.get("/admin/login").text
    assert "sr-ticket" in login and 'action="/admin/login"' in login
    monkeypatch.setattr(config, "SCREENING_ROOM", False)
    assert 'class="cream-theme"' in client.get("/admin/login").text


def test_aerial_pass_booking_addon(monkeypatch, admin):
    """The Aerial Pass add-on rides re- bookings via notes (zero-schema) and is
    doubly gated: the checkbox only acts when the slug is re- AND aerials_live.
    The /real-estate band + spec line ride the same flag."""
    import datetime as dt

    from app import scheduling as S

    eid = db.run(
        """INSERT INTO event_types
        (slug, name, duration_min, min_notice_hours, booking_window_days,
         max_per_day, creates_notion_session, location, active)
        VALUES (?,?,?,?,?,?,?,?,1)""",
        ("re-aerial-test", "RE Aerial Test", 60, 1, 60, 0, 1, "On-site"),
    )
    for wd in range(5):
        db.run(
            "INSERT INTO availability_rules (event_type_id, weekday, start_min, "
            "end_min) VALUES (?,?,?,?)",
            (eid, wd, 540, 1020),
        )
    et = S.event_by_slug("re-aerial-test")
    day = dt.date.today() + dt.timedelta(days=3)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    slots = S.slots_for_day(et, day)
    assert len(slots) >= 2

    def book(start, flag, aerial="1"):
        monkeypatch.setattr(config, "AERIALS_LIVE", flag)
        with TestClient(app) as pub:
            r = pub.post(
                "/book/re-aerial-test",
                data={
                    "name": "Agent Ada",
                    "email": f"ada+{flag}@brokerage.com",
                    "start": start,
                    "tz": "America/New_York",
                    "notes": "Twilight if possible.",
                    "aerial_pass": aerial,
                },
                follow_redirects=False,
            )
            assert r.status_code == 303, r.text
        return db.one("SELECT * FROM bookings ORDER BY id DESC LIMIT 1")

    try:
        # flag ON: intake shows the add-on line and the note tag lands
        monkeypatch.setattr(config, "AERIALS_LIVE", True)
        with TestClient(app) as pub:
            page = pub.get(
                f"/book/re-aerial-test?year={day.year}&month={day.month}"
                f"&day={day.isoformat()}&start={slots[0]['utc']}&tz=America/New_York"
            ).text
            assert 'name="aerial_pass"' in page
            assert "+$150" in page  # the rate from specialties.AERIAL_PASS_CENTS
        b = book(slots[0]["utc"], True)
        assert "AERIAL PASS requested (+$150 add-on)" in b["notes"]
        assert "Twilight if possible." in b["notes"]

        # flag OFF: checkbox is gone and a forged POST field is ignored
        from app import ratelimit, security

        ratelimit._hits.clear()
        db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (security.INQUIRY_BUCKET_BOOK,))
        monkeypatch.setattr(config, "AERIALS_LIVE", False)
        with TestClient(app) as pub:
            page = pub.get(
                f"/book/re-aerial-test?year={day.year}&month={day.month}"
                f"&day={day.isoformat()}&start={slots[1]['utc']}&tz=America/New_York"
            ).text
            assert 'name="aerial_pass"' not in page
        b = book(slots[1]["utc"], False)
        assert "AERIAL PASS" not in (b["notes"] or "")
    finally:
        db.run("DELETE FROM bookings WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM availability_rules WHERE event_type_id=?", (eid,))
        db.run("DELETE FROM event_types WHERE id=?", (eid,))


def test_screening_room_behavior_hooks(admin, monkeypatch):
    """The Screening Room behaviors ship as delegated, CSP-safe hooks: chapter
    seeking + bench culling live in behaviors.js (no inline handlers), the
    bench rail carries the premiere check, the deck renders ON DECK, and the
    ledger gets its month reel."""

    js = admin.get("/static/behaviors.js").text
    assert "data-seek" in js and "data-cull" in js
    assert "data-deck-swipe" in js  # mobile one-hand deck (3j)

    admin.post("/admin/galleries", data={"title": "Hook Check"}, follow_redirects=False)
    g = db.one("SELECT * FROM galleries WHERE title='Hook Check' ORDER BY id DESC LIMIT 1")
    try:
        page = admin.get(f"/admin/galleries/{g['id']}").text
        assert "Premiere check" in page
        assert "curtain down (draft)" in page
        assert "client not linked" in page

        deck = admin.get("/admin/home").text
        assert "On deck" in deck
        assert "/admin/palette.json" in deck  # ⌘K command-runner bindings load lazily
        assert admin.get("/admin/palette.json").json()["clients"] is not None
        # phone mode ships server-side as hidden hooks; JS unhides them ≤860px
        assert "data-deck-swipe" in deck
        assert "data-deck-nav" in deck and "data-deck-count" in deck
        assert "Swipe &larr; done" in deck or "Swipe ← done" in deck
        # the swipe behavior keys off the sr-admin body class, which the kill
        # switch removes — pin the precondition the JS gate relies on
        monkeypatch.setattr(config, "SCREENING_ROOM", False)
        assert "sr-admin" not in admin.get("/admin/home").text
        monkeypatch.setattr(config, "SCREENING_ROOM", True)

        fin = admin.get("/admin/financials").text
        assert "sr-monthreel" in fin
    finally:
        admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)


def test_premiere_plays_once_per_browser(admin, monkeypatch):
    """The premiere title card is a ceremony that plays on the first admitted
    visit; after that a seen-cookie (set only after PIN admission, scoped to
    the gallery path) compresses it to a welcome-back strip. Display-only:
    the kill switch keeps the full card on every visit, and nothing
    server-side depends on the cookie."""

    admin.post("/admin/galleries", data={"title": "Premiere Once"}, follow_redirects=False)
    g = db.one("SELECT * FROM galleries WHERE title='Premiere Once' ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Premiere Once", "pin": "4321", "published": "true"},
    )
    try:
        with TestClient(app) as pub:
            # the PIN gate itself never marks the premiere as seen
            gate = pub.get(f"/g/{g['slug']}")
            assert gate.status_code == 200 and "PIN" in gate.text
            assert f"sr_seen_g{g['id']}" not in ";".join(gate.headers.get_list("set-cookie"))
            pub.post(f"/g/{g['slug']}/pin", data={"pin": "4321"}, follow_redirects=False)

            # first admitted visit: the full ceremony, and the seen-cookie lands
            # with the load-bearing attributes (path-scoped, HttpOnly)
            first = pub.get(f"/g/{g['slug']}")
            assert "a private premiere" in first.text
            assert "Welcome back" not in first.text
            seen = [
                c
                for c in first.headers.get_list("set-cookie")
                if c.startswith(f"sr_seen_g{g['id']}=")
            ]
            assert len(seen) == 1
            assert f"Path=/g/{g['slug']}" in seen[0] and "HttpOnly" in seen[0]

            # second visit: compact welcome-back strip, straight to the frames
            again = pub.get(f"/g/{g['slug']}")
            assert "Welcome back" in again.text
            assert "the screening room is open" in again.text
            assert "a private premiere" not in again.text

            # kill switch: the cream fallback keeps the full card on every visit
            monkeypatch.setattr(config, "SCREENING_ROOM", False)
            cream = pub.get(f"/g/{g['slug']}")
            assert "a private premiere" in cream.text
            assert "Welcome back" not in cream.text

        # flag OFF must not burn the ceremony: a browser that visited while
        # the kill switch was down still gets its first premiere (and only
        # then the compact strip) once the flag comes back up
        with TestClient(app) as pub2:
            pub2.post(f"/g/{g['slug']}/pin", data={"pin": "4321"}, follow_redirects=False)
            off = pub2.get(f"/g/{g['slug']}")
            assert f"sr_seen_g{g['id']}" not in ";".join(off.headers.get_list("set-cookie"))
            monkeypatch.setattr(config, "SCREENING_ROOM", True)
            assert "a private premiere" in pub2.get(f"/g/{g['slug']}").text
            assert "Welcome back" in pub2.get(f"/g/{g['slug']}").text
    finally:
        # published galleries refuse deletion on purpose — unpublish first so
        # the cleanup actually runs instead of silently 400ing and leaking a
        # live gallery into the shared session DB
        admin.post(
            f"/admin/galleries/{g['id']}/settings",
            data={"title": "Premiere Once", "pin": "4321"},
        )
        admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)
        assert db.one("SELECT 1 FROM galleries WHERE id=?", (g["id"],)) is None


def test_focused_project_delivery_check(admin, monkeypatch):
    """The focused-project delivery workbench (Screening Room 3h): admin-gated,
    read-only over the linked gallery, and it only polls while an encode can
    still FINISH — a permanently failed asset must not keep the fragment
    polling forever. The whole focused row rides the kill switch."""

    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Workbench Co",))
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Workbench Project"))
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("WorkbenchSlug1", "Workbench", "5678"),
    )
    url = f"/admin/studio/projects/{pid}/delivery-check"
    try:
        # gated like every other studio route
        with TestClient(app) as anon:
            assert anon.get(url, follow_redirects=False).status_code in (303, 401, 403)

        # no gallery linked yet → the hint, and no polling
        frag = admin.get(url).text
        assert "No gallery linked yet" in frag
        assert "hx-get" not in frag

        db.run("UPDATE projects SET gallery_id=? WHERE id=?", (gid, pid))

        # linked, everything settled → checklist renders, still no polling
        frag = admin.get(url).text
        assert "PIN + expiry review" in frag
        assert "hx-get" not in frag

        # a failed encode surfaces as a warning but must NOT poll forever
        aid = db.run(
            "INSERT INTO assets (gallery_id, kind, filename, stored, bytes, status) "
            "VALUES (?,?,?,?,?,?)",
            (gid, "photo", "x.jpg", "x.jpg", 1, "failed"),
        )
        frag = admin.get(url).text
        assert "1 file failed" in frag
        assert "hx-get" not in frag

        # a live encode DOES poll
        db.run("UPDATE assets SET status='pending' WHERE id=?", (aid,))
        frag = admin.get(url).text
        assert "hx-get" in frag and "every 8s" in frag

        # stock chip: derived from the newest non-cancelled booking's event
        # slug prefix — a cancelled re- booking must not survive as the chip
        eid = db.run(
            """INSERT INTO event_types
            (slug, name, duration_min, min_notice_hours, booking_window_days,
             max_per_day, creates_notion_session, location, active)
            VALUES (?,?,?,?,?,?,?,?,1)""",
            ("re-chip-test", "RE Chip Test", 60, 1, 60, 0, 0, "On-site"),
        )
        bid = db.run(
            """INSERT INTO bookings (token, event_type_id, project_id, name, email,
                                     start_utc, end_utc, status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                "chiptok1",
                eid,
                pid,
                "Chip",
                "chip@x.com",
                "2026-08-01 15:00:00",
                "2026-08-01 16:00:00",
                "confirmed",
            ),
        )
        monkeypatch.setattr(config, "SCREENING_ROOM", True)
        page = admin.get(f"/admin/studio/projects/{pid}").text
        assert "250D" in page  # re- slug → Feature 01 stock
        db.run("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
        page = admin.get(f"/admin/studio/projects/{pid}").text
        assert "250D" not in page

        # the focused row honors the kill switch on the full project page
        page = admin.get(f"/admin/studio/projects/{pid}")
        assert page.status_code == 200 and "The money on this one" in page.text
        monkeypatch.setattr(config, "SCREENING_ROOM", False)
        page = admin.get(f"/admin/studio/projects/{pid}")
        assert page.status_code == 200 and "The money on this one" not in page.text
    finally:
        db.run("DELETE FROM bookings WHERE project_id=?", (pid,))
        db.run("DELETE FROM event_types WHERE slug='re-chip-test'")
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
        db.run("DELETE FROM projects WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))
