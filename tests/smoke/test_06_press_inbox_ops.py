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

# ── Domain H slice 1: press / published-work tracking ──────────────────────


def test_press_outlet_only_and_audit(admin):
    """(a) A press hit can exist with ALL linkage FKs null — outlet is the only
    required anchor (own-brand / editorial press has no client). Create writes one
    audit row (entity_type='press'), matching the licenses rigor."""
    r = admin.post("/admin/studio/press", data={"outlet": "Garden & Gun"}, follow_redirects=False)
    assert r.status_code == 303
    p = db.one("SELECT * FROM press ORDER BY id DESC LIMIT 1")
    assert p["outlet"] == "Garden & Gun"
    assert p["client_id"] is None and p["project_id"] is None
    assert p["gallery_id"] is None and p["asset_id"] is None
    assert p["publish_date"] is None  # pending until a date is set
    created = db.all_(
        """SELECT * FROM audit_log WHERE entity_type='press'
                         AND entity_id=? AND action='create'""",
        (p["id"],),
    )
    assert len(created) == 1
    # list page renders the row (Jinja autoescapes the ampersand)
    assert "Garden &amp; Gun" in admin.get("/admin/studio/press").text


def test_press_publish_date_is_the_gate(admin):
    """(b) publish_date NULL = pending; populated + past = published. The E gate
    (publish_date IS NOT NULL AND publish_date <= today) selects the past row and
    EXCLUDES a future-dated one. Dates are relative to date('now') so the gate
    cannot flake by calendar day."""
    import datetime as _dt

    admin.post("/admin/studio/press", data={"outlet": "Pending Mag"}, follow_redirects=False)
    pending = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # past-dated = published
    admin.post(
        "/admin/studio/press",
        data={"outlet": "Past Times", "publish_date": "2020-01-15"},
        follow_redirects=False,
    )
    past = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # future-dated = announced but not yet out → must be excluded by the gate
    future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    admin.post(
        "/admin/studio/press",
        data={"outlet": "Future Weekly", "publish_date": future},
        follow_redirects=False,
    )
    fut = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]

    gated = {
        r["id"]
        for r in db.all_(
            """SELECT id FROM press WHERE deleted_at IS NULL
           AND publish_date IS NOT NULL
           AND publish_date <= date('now', 'localtime')"""
        )
    }
    assert past in gated  # published
    assert pending not in gated  # no date = pending
    assert fut not in gated  # future date = not yet out


def test_press_for_license_seam(admin):
    """(c) press_for_license joins published press to a license on linkage +
    channel overlap, returns ONLY gated rows, and writes NOTHING to
    licenses.published (suggestion only — the human owns the flag)."""
    import datetime as _dt

    from app.admin.press import press_for_license

    admin.post(
        "/admin/studio/clients",
        data={"name": "Press Che", "company": "Seam Bistro"},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    gid = db.run(
        "INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
        (c["id"], "Seam Gallery", "seamgal12345", "0000"),
    )
    # a license on that gallery granting the 'print' channel, published flag OFF
    admin.post(
        f"/admin/studio/clients/{c['id']}/licenses",
        data={"title": "Seam license"},
        follow_redirects=False,
    )
    lic_id = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    db.run(
        """UPDATE licenses SET gallery_id=?, channels='["print"]', published=0
              WHERE id=?""",
        (gid, lic_id),
    )
    lic = db.one("SELECT * FROM licenses WHERE id=?", (lic_id,))

    # published press on that gallery, channel overlaps the license grant
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (gid, "Print Mag", "print", "2021-06-01"),
    )
    hit = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    # a future-dated press on the same gallery — must be gated out
    fut = (_dt.date.today() + _dt.timedelta(days=20)).isoformat()
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (gid, "Soon Mag", "print", fut),
    )

    rows = press_for_license(lic)
    ids = {r["id"] for r in rows}
    assert hit in ids  # gated + linked → returned
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
    db.run("DELETE FROM clients WHERE id=?", (cid,))  # FK pragma ON → SET NULL fires
    row = db.one("SELECT * FROM press WHERE id=?", (pid,))
    assert row is not None and row["client_id"] is None
    assert row["outlet"] == "Standalone Press"  # row survived


def test_channels_extraction_no_regression(admin):
    """(e) The CHANNELS extraction is pure: licenses.py and press.py share the one
    list from app.usage_vocab, with identical values + order. licenses behaviour is
    unchanged — a license still persists a valid channel selection."""
    import json as _json

    from app.admin.licenses import CHANNELS as LIC_CHANNELS
    from app.admin.press import CHANNELS as PRESS_CHANNELS
    from app.usage_vocab import CHANNELS as VOCAB

    expected = [
        "website",
        "social_organic",
        "social_paid",
        "ooh_billboard",
        "print",
        "pr_editorial",
        "delivery_apps",
        "menu",
        "email",
        "broadcast",
    ]
    assert VOCAB == expected  # values + order frozen
    assert LIC_CHANNELS is VOCAB and PRESS_CHANNELS is VOCAB  # one source object

    # licenses still validate + store channels exactly as before the move
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Vocab Chef",))
    admin.post(
        f"/admin/studio/clients/{cid}/licenses",
        data={"title": "Vocab license"},
        follow_redirects=False,
    )
    lid = db.one("SELECT id FROM licenses ORDER BY id DESC LIMIT 1")["id"]
    admin.post(
        f"/admin/studio/licenses/{lid}",
        data={"title": "Vocab license", "channels": ["print", "bogus_channel"]},
        follow_redirects=False,
    )
    stored = _json.loads(db.one("SELECT channels FROM licenses WHERE id=?", (lid,))["channels"])
    assert stored == ["print"]  # valid kept, bogus dropped — unchanged behaviour


def test_press_validation_400s(admin):
    """(f) 400s on blank outlet / bad publish_date / channel outside CHANNELS."""
    assert (
        admin.post(
            "/admin/studio/press", data={"outlet": "   "}, follow_redirects=False
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/admin/studio/press",
            data={"outlet": "OK Mag", "publish_date": "not-a-date"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            "/admin/studio/press",
            data={"outlet": "OK Mag", "channel": "tiktok_dance"},
            follow_redirects=False,
        ).status_code
        == 400
    )


def test_press_evidence_renders_with_cue(admin):
    """(a) A license with matching published press shows the read-only 'Press
    evidence' section AND the review-published cue near the published control
    (cue only fires while published is OFF — the human hasn't confirmed yet)."""
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Evidence Che", "Cue Bistro", "h3evidence123"
    )
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date, url)
              VALUES (?,?,?,?,?)""",
        (gid, "Bon Appetit", "print", "2021-03-01", "https://example.com/run"),
    )
    body = admin.get(f"/admin/studio/licenses/{lic_id}").text
    assert "Press evidence" in body
    assert "Bon Appetit" in body
    assert "review the evidence below and confirm published" in body
    assert "https://example.com/run" in body
    assert "granted" in body  # channel_overlap annotation


def test_press_evidence_silent_when_no_match(admin):
    """(b) A license with no matching press renders silent — no 'Press evidence'
    section, no cue, no error. Matches the silent-when-empty idiom."""
    c, gid, lic_id = _seam_license_with_gallery(admin, "Quiet Che", "Silent Bistro", "h3silent1234")
    # press exists but links to a DIFFERENT, unrelated gallery → no overlap
    other = db.run(
        "INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
        (c["id"], "Other Gallery", "h3other12345", "0000"),
    )
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (other, "Unrelated Weekly", "print", "2021-03-01"),
    )
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

    c, gid, lic_id = _seam_license_with_gallery(admin, "Gate Che", "Gate Bistro", "h3gate123456")
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (gid, "Shown Past Mag", "print", "2020-02-02"),
    )
    future = (_dt.date.today() + _dt.timedelta(days=25)).isoformat()
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (gid, "Hidden Future Mag", "print", future),
    )
    other = db.run(
        "INSERT INTO galleries (client_id, title, slug, pin) VALUES (?,?,?,?)",
        (c["id"], "Unlinked Gallery", "h3unlinked12", "0000"),
    )
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (other, "Hidden Unlinked Mag", "print", "2020-02-02"),
    )
    body = admin.get(f"/admin/studio/licenses/{lic_id}").text
    assert "Shown Past Mag" in body
    assert "Hidden Future Mag" not in body  # future-dated gated out
    assert "Hidden Unlinked Mag" not in body  # unlinked never matches


def test_press_evidence_render_writes_nothing(admin):
    """(d) Viewing the detail page is read-only: rendering the evidence performs
    ZERO writes to licenses.published, and once published IS set the suggestion
    cue stops (evidence still shows, but the 'confirm published' nudge is gone —
    the flip stays the existing human control's job)."""
    c, gid, lic_id = _seam_license_with_gallery(
        admin, "Readonly Che", "NoWrite Bistro", "h3readonly12"
    )
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (gid, "Evidence Times", "print", "2021-01-01"),
    )
    url = f"/admin/studio/licenses/{lic_id}"

    # GET the page several times — published must remain 0 (no auto-flip on view)
    for _ in range(3):
        assert admin.get(url).status_code == 200
    assert db.one("SELECT published FROM licenses WHERE id=?", (lic_id,))["published"] == 0
    # the seam never wrote an audit row either (read-only, no mutation)
    assert (
        db.all_("""SELECT 1 FROM audit_log WHERE entity_type='press'
                      AND action IN ('update','status_change')""")
        == []
    )

    # human confirms via the EXISTING control → published flips, cue disappears
    db.run("UPDATE licenses SET published=1 WHERE id=?", (lic_id,))
    body = admin.get(url).text
    assert "Press evidence" in body and "Evidence Times" in body  # evidence still shown
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
    c1, g1, on_id = _seam_license_with_gallery(admin, "H2 On Che", "On Bistro", "h2on1234567")
    db.run("UPDATE licenses SET status='active' WHERE id=?", (on_id,))
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (g1, "Eater", "print", "2021-05-01"),
    )
    # (2) active + unpublished + NO matching press → off the strip
    c2, g2, none_id = _seam_license_with_gallery(admin, "H2 None Che", "None Bistro", "h2none12345")
    db.run("UPDATE licenses SET status='active' WHERE id=?", (none_id,))
    # (3) already-confirmed (published=1) + matching press → off the strip (cue gone)
    c3, g3, done_id = _seam_license_with_gallery(admin, "H2 Done Che", "Done Bistro", "h2done12345")
    db.run("UPDATE licenses SET status='active', published=1 WHERE id=?", (done_id,))
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (g3, "Garden & Gun", "print", "2021-05-01"),
    )
    # (4) draft (status != active) + unpublished + matching press → off (active-only)
    c4, g4, draft_id = _seam_license_with_gallery(
        admin, "H2 Draft Che", "Draft Bistro", "h2draft1234"
    )
    db.run("UPDATE licenses SET status='draft' WHERE id=?", (draft_id,))
    db.run(
        """INSERT INTO press (gallery_id, outlet, channel, publish_date)
              VALUES (?,?,?,?)""",
        (g4, "Local Weekly", "print", "2021-05-01"),
    )

    body = admin.get("/admin/studio/activity").text
    assert "Press evidence — confirm published" in body  # strip rendered
    assert f"/admin/studio/licenses/{on_id}" in body  # the actionable one
    assert f"/admin/studio/licenses/{none_id}" not in body  # no evidence → silent
    assert f"/admin/studio/licenses/{done_id}" not in body  # confirmed → dropped
    assert f"/admin/studio/licenses/{draft_id}" not in body  # not active → quiet

    # read-only: repeated renders never flip the matched license's published bit,
    # and the strip writes no audit row against it.
    for _ in range(3):
        assert admin.get("/admin/studio/activity").status_code == 200
    assert db.one("SELECT published FROM licenses WHERE id=?", (on_id,))["published"] == 0
    assert (
        db.all_(
            """SELECT 1 FROM audit_log WHERE entity_type='license'
                      AND entity_id=? AND action IN ('update','status_change')""",
            (on_id,),
        )
        == []
    )


# ── Domain H slice 4: public "As seen in" surface ──────────────────────────


def test_press_show_on_site_flag_roundtrips_and_audits(admin):
    """The admin press form's 'Feature on public site' checkbox round-trips to the
    show_on_site column (checked=1, absent=0) and the toggle is captured in the
    press audit trail — public visibility is an auditable human act, default off."""
    # checkbox present → 1
    admin.post(
        "/admin/studio/press",
        data={"outlet": "Bon Appétit", "publish_date": "2021-03-01", "show_on_site": "1"},
        follow_redirects=False,
    )
    pid = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid,))["show_on_site"] == 1
    # checkbox absent → 0 (nothing leaks unless explicitly toggled)
    admin.post(
        "/admin/studio/press",
        data={"outlet": "Private Trade Rag", "publish_date": "2021-03-01"},
        follow_redirects=False,
    )
    pid2 = db.one("SELECT id FROM press ORDER BY id DESC LIMIT 1")["id"]
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid2,))["show_on_site"] == 0
    # un-featuring later (checkbox now absent) flips it back to 0 and is audited
    admin.post(
        f"/admin/studio/press/{pid}",
        data={"outlet": "Bon Appétit", "publish_date": "2021-03-01"},
        follow_redirects=False,
    )
    assert db.one("SELECT show_on_site FROM press WHERE id=?", (pid,))["show_on_site"] == 0
    rows = db.all_(
        """SELECT diff_json FROM audit_log WHERE entity_type='press'
                      AND entity_id=? AND action='update'""",
        (pid,),
    )
    assert any("show_on_site" in (r["diff_json"] or "") for r in rows)


def test_press_public_surface_gates_and_dedups(client):
    """The public /press page + home strip render ONLY press that is featured
    (show_on_site=1) AND published (publish_date populated and not in the future),
    deduped by outlet. The default-off flag plus the publish_date gate keep
    internal / confidential / pending press off the open internet."""
    # featured + published + past → public; two pieces from one outlet → deduped
    db.run(
        """INSERT INTO press (outlet, title, url, publish_date, show_on_site)
              VALUES (?,?,?,?,1)""",
        ("The Local Spoon", "Older piece", "https://spoon.example/a", "2020-01-01"),
    )
    db.run(
        """INSERT INTO press (outlet, title, url, publish_date, show_on_site)
              VALUES (?,?,?,?,1)""",
        ("The Local Spoon", "Newest piece", "https://spoon.example/b", "2023-06-01"),
    )
    # featured but NOT yet published (pending) → hidden
    db.run(
        """INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,1)""",
        ("Pending Public Mag", None),
    )
    # featured but future-dated → hidden until it's actually out
    db.run(
        """INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,1)""",
        ("Future Public Mag", "2099-01-01"),
    )
    # published+past but NOT featured (default 0) → stays internal
    db.run(
        """INSERT INTO press (outlet, publish_date, show_on_site)
              VALUES (?,?,0)""",
        ("Confidential Trade Rag", "2020-01-01"),
    )

    body = client.get("/press").text
    assert "The Local Spoon" in body  # featured + published
    assert "Pending Public Mag" not in body  # pending → gated
    assert "Future Public Mag" not in body  # future → gated
    assert "Confidential Trade Rag" not in body  # not featured → internal
    # deduped: outlet appears once, and the link points at the NEWEST piece
    assert body.count("The Local Spoon") == 1
    assert "https://spoon.example/b" in body  # newest wins the link
    assert "https://spoon.example/a" not in body  # older piece dropped
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
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "Shotlist Tester",
            "company": "Mise Test Kitchen FBQ",
            "email": "shotfbq@example.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "FBQ shoot production project"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")

    # create — priority omitted defaults to 'want'
    r = admin.post(
        f"/admin/studio/projects/{p['id']}/shots",
        data={"title": "Plated hero FBQ three-quarter", "category": "Hero Dish", "sort_order": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = db.one("SELECT * FROM shot_list ORDER BY id DESC LIMIT 1")
    assert s["title"] == "Plated hero FBQ three-quarter"
    assert s["category"] == "Hero Dish" and s["priority"] == "want"
    assert s["project_id"] == p["id"] and s["deleted_at"] is None
    created = db.all_(
        """SELECT * FROM audit_log WHERE entity_type='shot_list'
                         AND entity_id=? AND action='create'""",
        (s["id"],),
    )
    assert len(created) == 1

    # vocab gate — bad category and bad priority both 400 (no row written)
    assert (
        admin.post(
            f"/admin/studio/projects/{p['id']}/shots",
            data={"title": "x", "category": "NotARealCategory"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/studio/projects/{p['id']}/shots",
            data={"title": "x", "priority": "urgent"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    # title required
    assert (
        admin.post(
            f"/admin/studio/projects/{p['id']}/shots", data={"title": "   "}, follow_redirects=False
        ).status_code
        == 400
    )

    # update — change priority + note; diff audit row written
    r = admin.post(
        f"/admin/studio/shots/{s['id']}",
        data={
            "title": s["title"],
            "category": "Hero Dish",
            "priority": "must",
            "sort_order": "5",
            "note": "shoot first FBQ",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    s2 = db.one("SELECT * FROM shot_list WHERE id=?", (s["id"],))
    assert s2["priority"] == "must" and s2["note"] == "shoot first FBQ"
    upd = db.all_(
        """SELECT * FROM audit_log WHERE entity_type='shot_list'
                     AND entity_id=? AND action='update'""",
        (s["id"],),
    )
    assert len(upd) == 1

    # renders on the project page
    assert "Plated hero FBQ three-quarter" in admin.get(f"/admin/studio/projects/{p['id']}").text

    # soft-delete — deleted_at set, vanishes from page and inline query
    r = admin.post(f"/admin/studio/shots/{s['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT deleted_at FROM shot_list WHERE id=?", (s["id"],))["deleted_at"]
    assert (
        "Plated hero FBQ three-quarter" not in admin.get(f"/admin/studio/projects/{p['id']}").text
    )
    assert (
        db.one(
            """SELECT COUNT(*) n FROM shot_list
                     WHERE project_id=? AND deleted_at IS NULL""",
            (p["id"],),
        )["n"]
        == 0
    )


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

    sess = "notion-page-fbq-readapi-001"
    admin.post(
        "/admin/studio/clients",
        data={
            "name": "ReadAPI Tester",
            "company": "Mise Test Kitchen RAPI",
            "email": "rapi@example.com",
            "phone": "",
        },
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/studio/clients/{c['id']}/projects",
        data={"title": "ReadAPI shoot project"},
        follow_redirects=False,
    )
    p = db.one("SELECT * FROM projects ORDER BY id DESC LIMIT 1")
    db.run("UPDATE projects SET notion_page_id=? WHERE id=?", (sess, p["id"]))

    # two shots out of insert order + one soft-deleted, to prove ordering + exclusion
    admin.post(
        f"/admin/studio/projects/{p['id']}/shots",
        data={"title": "RAPI second", "category": "Detail", "priority": "want", "sort_order": "20"},
        follow_redirects=False,
    )
    admin.post(
        f"/admin/studio/projects/{p['id']}/shots",
        data={
            "title": "RAPI first",
            "category": "Hero Dish",
            "priority": "must",
            "sort_order": "10",
        },
        follow_redirects=False,
    )
    admin.post(
        f"/admin/studio/projects/{p['id']}/shots",
        data={"title": "RAPI gone", "priority": "if-time", "sort_order": "5"},
        follow_redirects=False,
    )
    gone = db.one("SELECT id FROM shot_list WHERE title='RAPI gone'")
    admin.post(f"/admin/studio/shots/{gone['id']}/delete", follow_redirects=False)

    url = f"/api/shots?session={sess}"
    saved = config.SHOTS_TOKEN
    try:
        # disarmed -> 503 even with a bearer present
        config.SHOTS_TOKEN = ""
        assert admin.get(url, headers={"Authorization": "Bearer anything"}).status_code == 503

        # armed
        config.SHOTS_TOKEN = "rapi-secret-token"
        bearer = {"Authorization": "Bearer rapi-secret-token"}

        assert admin.get(url).status_code == 401  # no header
        assert (
            admin.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        )  # wrong token

        r = admin.get(url, headers=bearer)
        assert r.status_code == 200
        body = r.json()
        assert body["matched"] is True and body["project_id"] == p["id"]
        titles = [s["title"] for s in body["shots"]]
        assert titles == ["RAPI first", "RAPI second"]  # sort_order order, gone excluded
        assert body["shots"][0] == {
            "title": "RAPI first",
            "category": "Hero Dish",
            "priority": "must",
        }

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

    iid = db.run(
        "INSERT INTO inquiries (name, email, business, message, kind) VALUES (?,?,?,?,?)",
        (
            "Ana Diaz",
            "ana@bistro.test",
            "Bistro Verde",
            "Need a full menu shoot in July.",
            "contact",
        ),
    )

    with (
        mock.patch.object(mailer, "configured", return_value=True),
        mock.patch.object(mailer, "send") as send,
    ):
        r = admin.post(
            f"/admin/inbox/{iid}/reply",
            data={
                "tab": "all",
                "subject": "Re: your inquiry",
                "message": "Hi Ana — happy to help, sending a quote.",
            },
            follow_redirects=False,
        )

    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/inbox?tab=all&sel={iid}"
    send.assert_called_once()
    to, subject, body = send.call_args.args[:3]
    assert to == "ana@bistro.test"
    assert "happy to help" in body
    assert db.one("SELECT emailed FROM inquiries WHERE id=?", (iid,))["emailed"] == 1
    logged = db.one(
        "SELECT doc_kind, doc_id, to_email FROM emails_log WHERE to_email='ana@bistro.test'"
    )
    assert logged is not None
    assert logged["doc_kind"] == "other" and logged["doc_id"] == iid


def test_inbox_reply_blocked_without_email(admin):
    """An inquiry with no email address can't be replied to — 400, no send."""
    from unittest import mock

    from app import mailer

    iid = db.run(
        "INSERT INTO inquiries (name, email, business, message, kind) VALUES (?,?,?,?,?)",
        ("No Email", "", "Ghost Cafe", "hi", "contact"),
    )
    # email is NOT NULL in schema; an empty string is the realistic "missing" case
    with (
        mock.patch.object(mailer, "configured", return_value=True),
        mock.patch.object(mailer, "send") as send,
    ):
        r = admin.post(
            f"/admin/inbox/{iid}/reply",
            data={"subject": "Re", "message": "x"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    send.assert_not_called()


def test_expense_create_and_delete(admin):
    """Expenses are real CRUD over operator-entered data: the row persists with
    cents parsed from a dollar string, and deductible math is honest."""
    r = admin.post(
        "/admin/financials/expenses",
        data={
            "spent_on": "2026-06-15",
            "vendor": "B&H Photo",
            "category": "Equipment",
            "amount": "1,240.00",
            "deductible_pct": "100",
            "notes": "85mm lens",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = db.one("SELECT * FROM expenses WHERE vendor='B&H Photo'")
    assert row is not None and row["amount_cents"] == 124000
    assert row["category"] == "Equipment" and row["deductible_pct"] == 100

    page = admin.get("/admin/financials/expenses")
    assert page.status_code == 200 and "Expense log" in page.text

    r = admin.post(f"/admin/financials/expenses/{row['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM expenses WHERE id=?", (row["id"],)) is None


def test_expense_rejects_bad_amount(admin):
    r = admin.post(
        "/admin/financials/expenses",
        data={"spent_on": "2026-06-15", "vendor": "Junk", "amount": "not-a-number"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_receipt_upload_links_and_serves(admin):
    """A receipt scan uploads to disk, links to an expense, serves back its bytes,
    and flags the expense as having a receipt — no auto-matching, the link is explicit."""
    eid = db.run(
        "INSERT INTO expenses (spent_on, vendor, category, amount_cents) VALUES (?,?,?,?)",
        ("2026-06-12", "Adobe", "Software", 5999),
    )
    png = _logo_png()
    r = admin.post(
        "/admin/financials/receipts",
        files={"file": ("adobe.png", io.BytesIO(png), "image/png")},
        data={"expense_id": str(eid)},
        follow_redirects=False,
    )
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
    r = admin.post(
        "/admin/financials/receipts",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_mileage_create_deduction_and_delete(admin):
    """Mileage is real CRUD; the deduction is miles × the IRS rate frozen per trip."""

    r = admin.post(
        "/admin/financials/mileage",
        data={
            "drove_on": "2026-06-17",
            "from_place": "Studio",
            "to_place": "Cúrate",
            "purpose": "Summer menu shoot",
            "miles": "8.4",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    trip = db.one("SELECT * FROM mileage WHERE to_place='Cúrate'")
    assert trip is not None and abs(trip["miles"] - 8.4) < 1e-6
    assert trip["rate_cents"] == config.MILEAGE_RATE_CENTS

    page = admin.get("/admin/financials/mileage")
    # 8.4 mi × 70¢ = $5.88
    assert page.status_code == 200 and "$5.88" in page.text

    r = admin.post(f"/admin/financials/mileage/{trip['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT id FROM mileage WHERE id=?", (trip["id"],)) is None


def test_dashboard_nudge_dismiss_clears_for_today(admin):
    """A 'Needs you today' nudge can be checked off; the dismissal is keyed to the
    underlying item and only suppresses it for the current local day (the worklist
    is 'needs you TODAY', so the item returns tomorrow if the condition still holds)."""
    iid = db.run(
        "INSERT INTO inquiries (name, business, email, message, created_at) "
        "VALUES (?,?,?,?, datetime('now','-5 days'))",
        ("Nudge Test Co", "Nudge Bistro", "nudge@example.com", "test msg"),
    )
    key = f"inq_reply:{iid}"
    try:
        # the stale inquiry surfaces as a checkable nudge and an ON DECK card
        before = admin.get("/admin/home").text
        assert key in before
        assert "Reply to Nudge Test Co" in before

        # an unknown nudge prefix is rejected (validated input, R18)
        bad = admin.post(
            "/admin/home/nudge/dismiss", data={"key": "bogus:1"}, follow_redirects=False
        )
        assert bad.status_code == 400

        # checking it off records the dismissal and drops it from today's worklist
        ok = admin.post("/admin/home/nudge/dismiss", data={"key": key}, follow_redirects=False)
        assert ok.status_code == 303
        assert db.one("SELECT 1 FROM dismissed_nudges WHERE nudge_key=?", (key,))
        after = admin.get("/admin/home").text
        assert key not in after
        # the deck honors the snooze too: ◯ / the mobile swipe say "until
        # tomorrow", so the whole card leaves the deck for the rest of the day
        assert "Reply to Nudge Test Co" not in after
    finally:
        db.run("DELETE FROM dismissed_nudges WHERE nudge_key=?", (key,))
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_webhook_inert_without_secret(client, monkeypatch):
    """Ships inert: with no signing secret the inbound route refuses (503), writing
    nothing — the same posture as the Stripe webhook."""

    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "")
    r = client.post("/webhooks/quo", content=b"{}")
    assert r.status_code == 503


def test_quo_webhook_rejects_bad_signature(client, monkeypatch):
    """Signature is the gate. A wrong/absent HMAC fails closed (400)."""
    import base64

    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", base64.b64encode(b"k").decode())
    r = client.post(
        "/webhooks/quo",
        content=b'{"type":"message.received"}',
        headers={"openphone-signature": "hmac;1;1700000000;deadbeef"},
    )
    assert r.status_code == 400


def test_quo_inbound_creates_sms_inquiry_and_is_idempotent(client, monkeypatch):
    """A text from an unknown number auto-creates a kind='sms' inquiry and records the
    message; a retried webhook (same provider id) is a no-op."""
    import base64
    import json

    secret = base64.b64encode(b"signing-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15557654321"
    body = {
        "type": "message.received",
        "data": {
            "object": {
                "id": "QUO_MSG_1",
                "direction": "incoming",
                "from": phone,
                "to": "+15550001111",
                "body": "Hi, do you shoot restaurants?",
            }
        },
    }
    raw = json.dumps(body).encode()
    sig = _quo_sig(secret, raw)
    try:
        r = client.post("/webhooks/quo", content=raw, headers={"openphone-signature": sig})
        assert r.status_code == 200 and r.json()["ok"]
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='sms'", (phone,))
        assert inq is not None and inq["message"].startswith("Hi, do you shoot")
        msgs = db.all_("SELECT * FROM messages WHERE inquiry_id=?", (inq["id"],))
        assert len(msgs) == 1 and msgs[0]["direction"] == "in" and msgs[0]["channel"] == "sms"

        # retry with the same provider id → idempotent, no new inquiry/message
        r2 = client.post("/webhooks/quo", content=raw, headers={"openphone-signature": sig})
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
    iid = db.run(
        "INSERT INTO inquiries (name, email, message, kind, phone) VALUES (?,?,?,?,?)",
        ("Texted Lead", "", "(no text)", "sms", phone),
    )
    sent = {}
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send", lambda to, body: sent.update(to=to, body=body) or "QUO_OUT_1")
    try:
        r = admin.post(
            f"/admin/inbox/{iid}/reply",
            data={"channel": "sms", "message": "Yes! Let's talk."},
            follow_redirects=False,
        )
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
    import base64
    import json

    secret = base64.b64encode(b"call-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15558889999"
    body = {
        "type": "call.completed",
        "data": {
            "object": {
                "id": "QUO_CALL_1",
                "direction": "incoming",
                "from": phone,
                "to": "+15550001111",
                "status": "completed",
                "duration": 134,
            }
        },
    }
    raw = json.dumps(body).encode()
    sig = _quo_sig(secret, raw)
    try:
        r = client.post("/webhooks/quo", content=raw, headers={"openphone-signature": sig})
        assert r.status_code == 200 and r.json()["ok"]
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='call'", (phone,))
        assert inq is not None
        msg = db.one("SELECT * FROM messages WHERE inquiry_id=? AND channel='call'", (inq["id"],))
        assert msg is not None and msg["direction"] == "in"
        assert "Incoming call" in msg["body"] and "2m14s" in msg["body"]

        # retry with the same call id → idempotent, no second row
        r2 = client.post("/webhooks/quo", content=raw, headers={"openphone-signature": sig})
        assert r2.status_code == 200 and r2.json().get("duplicate")
        assert db.one("SELECT COUNT(*) n FROM messages WHERE inquiry_id=?", (inq["id"],))["n"] == 1
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_CALL_1'")
        db.run("DELETE FROM inquiries WHERE phone=?", (phone,))


def test_inquiry_dismiss_returns_to_inbox_when_asked(admin):
    """Triage actions invoked from the Inbox honor a safe return_to so Kevin stays
    in the Inbox instead of being bounced to Studio. An unsafe/off-site return_to
    falls back to the default Studio destination."""
    iid = db.run(
        "INSERT INTO inquiries (name, email, message, kind) VALUES (?,?,?,?)",
        ("Triage Lead", "t@x.com", "hi", "contact"),
    )
    try:
        back = f"/admin/inbox?tab=all&sel={iid}"
        r = admin.post(
            f"/admin/studio/inquiries/{iid}/dismiss",
            data={"return_to": back},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == back

        # open-redirect guard: a non-/admin/ target is ignored
        r2 = admin.post(
            f"/admin/studio/inquiries/{iid}/undismiss",
            data={"return_to": "https://evil.example.com"},
            follow_redirects=False,
        )
        assert r2.status_code == 303 and r2.headers["location"] == "/admin/studio"
    finally:
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_inbound_reopens_dismissed_thread(client, monkeypatch):
    """A fresh inbound text on a thread the user had dismissed clears dismissed_at
    so it resurfaces in the active inbox instead of vanishing into the archive."""
    import base64
    import json

    secret = base64.b64encode(b"reopen-key").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15552223333"
    iid = db.run(
        "INSERT INTO inquiries (name, email, message, kind, phone, dismissed_at) "
        "VALUES (?,?,?,?,?, datetime('now'))",
        ("Texted Lead", "", "old text", "sms", phone),
    )
    body = {
        "type": "message.received",
        "data": {
            "object": {
                "id": "QUO_REOPEN_1",
                "direction": "incoming",
                "from": phone,
                "body": "Actually, are you free Friday?",
            }
        },
    }
    raw = json.dumps(body).encode()
    try:
        assert db.one("SELECT dismissed_at FROM inquiries WHERE id=?", (iid,))["dismissed_at"]
        r = client.post(
            "/webhooks/quo", content=raw, headers={"openphone-signature": _quo_sig(secret, raw)}
        )
        assert r.status_code == 200
        # same thread (no fork), now un-dismissed
        assert db.one("SELECT COUNT(*) n FROM inquiries WHERE phone=?", (phone,))["n"] == 1
        assert (
            db.one("SELECT dismissed_at FROM inquiries WHERE id=?", (iid,))["dismissed_at"] is None
        )
    finally:
        db.run("DELETE FROM messages WHERE provider_msg_id='QUO_REOPEN_1'")
        db.run("DELETE FROM inquiries WHERE id=?", (iid,))


def test_quo_missed_call_and_transcript_enrichment(client, monkeypatch):
    """A missed call reads as 'Missed call'; a later transcript event appends to the
    same call row rather than creating a new one, matched by call id."""
    import base64
    import json

    secret = base64.b64encode(b"call-key-2").decode()
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", secret)
    phone = "+15557778888"
    call = {
        "type": "call.completed",
        "data": {
            "object": {
                "id": "QUO_CALL_2",
                "direction": "incoming",
                "from": phone,
                "to": "+15550001111",
                "status": "missed",
            }
        },
    }
    raw = json.dumps(call).encode()
    try:
        r = client.post(
            "/webhooks/quo", content=raw, headers={"openphone-signature": _quo_sig(secret, raw)}
        )
        assert r.status_code == 200
        inq = db.one("SELECT * FROM inquiries WHERE phone=? AND kind='call'", (phone,))
        msg = db.one("SELECT * FROM messages WHERE inquiry_id=? AND channel='call'", (inq["id"],))
        assert msg["body"] == "Missed call"

        # transcript event for the same call appends, not a new row
        tr = {
            "type": "call.transcript.completed",
            "data": {
                "object": {
                    "callId": "QUO_CALL_2",
                    "dialogue": [
                        {"content": "Hi, leaving a voicemail"},
                        {"content": "call me back please"},
                    ],
                }
            },
        }
        traw = json.dumps(tr).encode()
        r2 = client.post(
            "/webhooks/quo", content=traw, headers={"openphone-signature": _quo_sig(secret, traw)}
        )
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


def test_platekit_bridge_disabled_is_dormant(monkeypatch):

    monkeypatch.setattr(config, "PLATEKIT_API_BASE", "")
    monkeypatch.setattr(config, "PLATEKIT_API_TOKEN", "")
    state = platekit.packs_for_client({"name": "Blue Plate", "company": "Blue Plate"})
    assert state["enabled"] is False
    assert state["slug"] == "blue-plate"
    assert state["packs"] == []
