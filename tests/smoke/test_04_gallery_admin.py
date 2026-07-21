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


def test_upload_all_rejected_reports_zero_accepted(admin):
    # Every unsupported file → route reports accepted=0 (the admin upload JS
    # reads this to avoid a false "Uploaded — processing…" message + reload) and
    # stores nothing.
    admin.post(
        "/admin/galleries", data={"title": "Reject Test", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries WHERE title='Reject Test' ORDER BY id DESC LIMIT 1")
    try:
        with TestClient(app):
            r = admin.post(
                f"/admin/galleries/{g['id']}/upload",
                files=[("files", ("notes.txt", b"not an image", "text/plain"))],
            )
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] == 0 and "notes.txt" in body["rejected"]
        assert db.one("SELECT COUNT(*) AS n FROM assets WHERE gallery_id=?", (g["id"],))["n"] == 0
        # the section 'remove' control carries a confirm guard against accidental
        # destruction of curation
        admin.post(f"/admin/galleries/{g['id']}/sections", data={"name": "Mains"})
        page = admin.get(f"/admin/galleries/{g['id']}").text
        assert "sections/" in page and 'data-confirm="Remove this section?' in page
    finally:
        db.run("DELETE FROM sections WHERE gallery_id=?", (g["id"],))
        db.run("DELETE FROM galleries WHERE id=?", (g["id"],))


def test_gallery_activity_404_on_missing_gallery(admin):
    # a deleted/nonexistent gallery must 404, not ghost-render the activity page
    # with an empty title and empty lists
    assert admin.get("/admin/galleries/999999/activity").status_code == 404


def test_gallery_delete(admin):
    from app import config as cfg

    admin.post(
        "/admin/galleries", data={"title": "Doomed", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/galleries/{g['id']}/upload",
        files=[("files", ("bye.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    media_dir = cfg.MEDIA_DIR / str(g["id"])
    assert media_dir.is_dir()

    # delete button only offered while unpublished; published galleries refuse
    # (two-step on purpose — a live client link shouldn't vanish on one click)
    assert "Delete gallery" in admin.get(f"/admin/galleries/{g['id']}").text
    db.run("UPDATE galleries SET published=1 WHERE id=?", (g["id"],))
    assert "Delete gallery" not in admin.get(f"/admin/galleries/{g['id']}").text
    assert (
        admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False).status_code == 400
    )
    db.run("UPDATE galleries SET published=0 WHERE id=?", (g["id"],))

    # deleting the cover asset clears the dangling cover_asset_id
    a = db.one("SELECT id FROM assets WHERE gallery_id=?", (g["id"],))
    admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/cover")
    assert (
        db.one("SELECT cover_asset_id FROM galleries WHERE id=?", (g["id"],))["cover_asset_id"]
        == a["id"]
    )
    admin.post(f"/admin/galleries/{g['id']}/assets/{a['id']}/delete")
    assert (
        db.one("SELECT cover_asset_id FROM galleries WHERE id=?", (g["id"],))["cover_asset_id"]
        is None
    )

    r = admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g["id"],))
    assert not db.one("SELECT 1 AS x FROM assets WHERE gallery_id=?", (g["id"],))
    assert not media_dir.exists()
    assert (
        admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False).status_code == 404
    )

    # portal-favorites safety: an unpublished gallery linked to a client with
    # a portal AND has favorited photos can't be silently deleted (would break
    # the client's social-crops view). Require force=1 as opt-in.
    admin.post(
        "/admin/galleries",
        data={"title": "PortalSafetyTest", "client_name": ""},
        follow_redirects=False,
    )
    g2 = db.one("SELECT * FROM galleries WHERE title='PortalSafetyTest'")
    admin.post(
        f"/admin/galleries/{g2['id']}/upload",
        files=[("files", ("safe.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    a2 = db.one("SELECT id FROM assets WHERE gallery_id=?", (g2["id"],))
    # plant a client + portal + linked favorite
    safety_cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Safety Co",))
    db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (safety_cid, "safety-portal-slug", "1234"),
    )
    db.run("UPDATE galleries SET client_id=? WHERE id=?", (safety_cid, g2["id"]))
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (g2["id"], "vtok-safety"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, a2["id"]))

    # gallery admin page surfaces the portal-fav count + safety copy
    page = admin.get(f"/admin/galleries/{g2['id']}").text
    assert "with 1 portal fav" in page
    # plain delete refused with explanatory 400
    r = admin.post(f"/admin/galleries/{g2['id']}/delete", follow_redirects=False)
    assert r.status_code == 400
    assert "social-crops" in r.json()["detail"] or "social-crops" in r.text
    # gallery still exists after the refused delete
    assert db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g2["id"],))

    # force=1 lets it through
    r = admin.post(
        f"/admin/galleries/{g2['id']}/delete", data={"force": "1"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert not db.one("SELECT 1 AS x FROM galleries WHERE id=?", (g2["id"],))


def test_asset_reorder(admin):
    admin.post(
        "/admin/galleries", data={"title": "Ordered", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    r = admin.post(
        f"/admin/galleries/{g['id']}/upload",
        files=[("files", (f"{n}.jpg", _jpeg_bytes(), "image/jpeg")) for n in "abc"],
    )
    assert r.status_code == 200 and r.json()["accepted"] == 3
    assert db.one("SELECT content_rev FROM galleries WHERE id=?", (g["id"],))["content_rev"] == (
        g["content_rev"] + 1
    )

    def order():  # same ORDER BY the public gallery uses — this IS the client-facing order
        return [
            r["id"]
            for r in db.all_(
                "SELECT id FROM assets WHERE gallery_id=? ORDER BY position, id", (g["id"],)
            )
        ]

    a1, a2, a3 = order()
    # move last one earlier; whole section gets renumbered from the legacy all-zero state
    r = admin.post(
        f"/admin/galleries/{g['id']}/assets/{a3}/move", data={"dir": "left"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert order() == [a1, a3, a2]
    # edge is a no-op, not an error
    admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/move", data={"dir": "left"})
    assert order() == [a1, a3, a2]
    admin.post(f"/admin/galleries/{g['id']}/assets/{a1}/move", data={"dir": "right"})
    assert order() == [a3, a1, a2]
    # bad direction 400s, unknown asset 404s
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{a1}/move",
            data={"dir": "up"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/assets/999999/move",
            data={"dir": "left"},
            follow_redirects=False,
        ).status_code
        == 404
    )
    # arrows render on the admin grid
    assert "Move earlier" in admin.get(f"/admin/galleries/{g['id']}").text

    # bulk section assignment: two at once, third untouched (stays in the section
    # it was uploaded into — the gallery's default first section)
    default_sec = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1", (g["id"],)
    )["id"]
    s = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Drinks'", (g["id"],))
    r = admin.post(
        f"/admin/galleries/{g['id']}/assets/bulk-section",
        data={"section_id": str(s["id"]), "asset_ids": [str(a1), str(a2)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    secs = {
        row["id"]: row["section_id"]
        for row in db.all_("SELECT id, section_id FROM assets WHERE gallery_id=?", (g["id"],))
    }
    assert secs[a1] == s["id"] and secs[a2] == s["id"] and secs[a3] == default_sec
    # empty section_id moves back to (none)
    admin.post(
        f"/admin/galleries/{g['id']}/assets/bulk-section",
        data={"section_id": "", "asset_ids": [str(a1)]},
    )
    assert db.one("SELECT section_id FROM assets WHERE id=?", (a1,))["section_id"] is None
    # a section the gallery doesn't own is rejected
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/assets/bulk-section",
            data={"section_id": "999999", "asset_ids": [str(a1)]},
            follow_redirects=False,
        ).status_code
        == 400
    )


def test_asset_section_assignment_rejects_unowned_sections(admin):
    """A real section id is not sufficient: it must belong to this gallery."""
    admin.post(
        "/admin/galleries",
        data={"title": "Section Owner Target", "client_name": ""},
        follow_redirects=False,
    )
    target = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(
        "/admin/galleries",
        data={"title": "Section Owner Foreign", "client_name": ""},
        follow_redirects=False,
    )
    foreign = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    target_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (target["id"],),
    )
    foreign_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (foreign["id"],),
    )
    missing_section_id = db.one("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM sections")["id"]
    asset_id = db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, stored) VALUES (?,?,?,?,?)",
        (target["id"], target_section["id"], "photo", "owned.jpg", "owned.jpg"),
    )
    foreign_asset_id = db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, stored) VALUES (?,?,?,?,?)",
        (foreign["id"], foreign_section["id"], "photo", "foreign.jpg", "foreign.jpg"),
    )

    for invalid_section_id in (foreign_section["id"], missing_section_id, 0):
        r = admin.post(
            f"/admin/galleries/{target['id']}/assets/{asset_id}/section",
            data={"section_id": str(invalid_section_id)},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "unknown section"
        assert (
            db.one("SELECT section_id FROM assets WHERE id=?", (asset_id,))["section_id"]
            == (target_section["id"])
        )

        r = admin.post(
            f"/admin/galleries/{target['id']}/assets/bulk-section",
            data={"section_id": str(invalid_section_id), "asset_ids": [str(asset_id)]},
            follow_redirects=False,
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "unknown section"
        assert (
            db.one("SELECT section_id FROM assets WHERE id=?", (asset_id,))["section_id"]
            == (target_section["id"])
        )

    # Asset ownership is the other half of the relation: a forged target-gallery
    # path must not move an asset owned by another gallery.
    r = admin.post(
        f"/admin/galleries/{target['id']}/assets/{foreign_asset_id}/section",
        data={"section_id": str(target_section["id"])},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = admin.post(
        f"/admin/galleries/{target['id']}/assets/bulk-section",
        data={"section_id": str(target_section["id"]), "asset_ids": [str(foreign_asset_id)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        db.one("SELECT section_id FROM assets WHERE id=?", (foreign_asset_id,))["section_id"]
        == foreign_section["id"]
    )

    # The unsectioned bucket remains an intentional, valid choice.
    r = admin.post(
        f"/admin/galleries/{target['id']}/assets/{asset_id}/section",
        data={"section_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.one("SELECT section_id FROM assets WHERE id=?", (asset_id,))["section_id"] is None


def test_section_rename_reorder(admin):
    admin.post(
        "/admin/galleries", data={"title": "Sectioned", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")

    def order():  # public gallery chapters render in this order
        return [
            r["name"]
            for r in db.all_(
                "SELECT name FROM sections WHERE gallery_id=? ORDER BY position, id", (g["id"],)
            )
        ]

    names = order()
    assert names[0] == "Hero Dishes"  # F&B presets seeded
    first = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Hero Dishes'", (g["id"],))

    # rename keeps assets attached (no delete/re-add dance)
    r = admin.post(
        f"/admin/galleries/{g['id']}/sections/{first['id']}/rename",
        data={"name": "Signature Dishes"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert order()[0] == "Signature Dishes"
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/sections/{first['id']}/rename",
            data={"name": "  "},
            follow_redirects=False,
        ).status_code
        == 400
    )

    # move down swaps with the neighbor; edge moves no-op
    admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move", data={"dir": "down"})
    assert order()[1] == "Signature Dishes"
    admin.post(f"/admin/galleries/{g['id']}/sections/{first['id']}/move", data={"dir": "up"})
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{first['id']}/move", data={"dir": "up"}
    )  # already first — no-op
    assert order()[0] == "Signature Dishes"

    # bad input
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/sections/{first['id']}/move",
            data={"dir": "sideways"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/sections/999999/move",
            data={"dir": "up"},
            follow_redirects=False,
        ).status_code
        == 404
    )


def test_upload_defaults_to_first_section(admin):
    """New uploads land in the gallery's first section (display order), not the
    catch-all 'More'. Explicit choices are still honored; a gallery with no
    sections keeps the clean None default."""
    admin.post(
        "/admin/galleries",
        data={"title": "Default Section Gal", "client_name": ""},
        follow_redirects=False,
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    secs = db.all_("SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id", (g["id"],))
    assert len(secs) >= 2  # F&B presets seed sections on create

    # no section_id given → lands in the first section, not unsectioned
    admin.post(
        f"/admin/galleries/{g['id']}/upload",
        files=[("files", ("hero.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    a = db.one(
        "SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1", (g["id"],)
    )
    assert a["section_id"] == secs[0]["id"]

    # an explicit section_id is honored, not overridden by the default
    admin.post(
        f"/admin/galleries/{g['id']}/upload?section_id={secs[1]['id']}",
        files=[("files", ("interior.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    a = db.one(
        "SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1", (g["id"],)
    )
    assert a["section_id"] == secs[1]["id"]

    # sectionless gallery → section_id stays None (current clean default preserved)
    db.run("DELETE FROM sections WHERE gallery_id=?", (g["id"],))
    admin.post(
        f"/admin/galleries/{g['id']}/upload",
        files=[("files", ("loose.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    a = db.one(
        "SELECT section_id FROM assets WHERE gallery_id=? ORDER BY id DESC LIMIT 1", (g["id"],)
    )
    assert a["section_id"] is None


def test_upload_rejects_unowned_section_before_side_effects(admin, monkeypatch):
    from app.admin import uploads as admin_uploads

    async def unexpected_save(*_args, **_kwargs):
        pytest.fail("invalid section reached the upload stream")

    monkeypatch.setattr(admin_uploads.common, "save_upload", unexpected_save)
    admin.post(
        "/admin/galleries",
        data={"title": "Upload Section Target", "client_name": ""},
        follow_redirects=False,
    )
    target = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(
        "/admin/galleries",
        data={"title": "Upload Section Foreign", "client_name": ""},
        follow_redirects=False,
    )
    foreign = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    foreign_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (foreign["id"],),
    )
    missing_section_id = db.one("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM sections")["id"]
    media_dir = config.MEDIA_DIR / str(target["id"])
    jobs_before = db.one("SELECT COUNT(*) AS n FROM jobs")["n"]
    rev_before = target["content_rev"]

    for invalid_section_id in (foreign_section["id"], missing_section_id, 0):
        r = admin.post(
            f"/admin/galleries/{target['id']}/upload?section_id={invalid_section_id}",
            files=[("files", ("foreign.jpg", _jpeg_bytes(), "image/jpeg"))],
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "unknown section"
        assert not db.one("SELECT id FROM assets WHERE gallery_id=?", (target["id"],))
        assert (
            db.one("SELECT content_rev FROM galleries WHERE id=?", (target["id"],))["content_rev"]
            == rev_before
        )
        assert db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == jobs_before
        assert not media_dir.exists()


def test_upload_batch_revalidates_section_ownership_at_insert(admin, monkeypatch):
    """A failing batch cannot corrupt or erase a concurrent successful upload."""
    from pathlib import Path

    from app.admin import uploads as admin_uploads

    admin.post(
        "/admin/galleries",
        data={"title": "Upload Race Target", "client_name": ""},
        follow_redirects=False,
    )
    target = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(
        "/admin/galleries",
        data={"title": "Upload Race Foreign", "client_name": ""},
        follow_redirects=False,
    )
    foreign = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    target_section = db.one(
        "SELECT id FROM sections WHERE gallery_id=? ORDER BY position, id LIMIT 1",
        (target["id"],),
    )
    original_save = admin_uploads.common.save_upload
    save_count = 0
    inside_inner = False
    inner_result = None

    async def save_then_reuse_section_id(file, dest):
        nonlocal inside_inner, inner_result, save_count
        if inside_inner:
            return await original_save(file, dest)
        size = await original_save(file, dest)
        save_count += 1
        if save_count == 2:
            inside_inner = True
            try:
                inner_file = admin_uploads.UploadFile(
                    io.BytesIO(_jpeg_bytes()), filename="inner.jpg"
                )
                try:
                    inner_result = await admin_uploads.upload(
                        target["id"], [inner_file], section_id=target_section["id"]
                    )
                finally:
                    await inner_file.close()
            finally:
                inside_inner = False
            db.run("DELETE FROM sections WHERE id=?", (target_section["id"],))
            db.run(
                "INSERT INTO sections (id, gallery_id, name, position) VALUES (?,?,?,?)",
                (target_section["id"], foreign["id"], "Reused foreign id", 99),
            )
        return size

    monkeypatch.setattr(admin_uploads.common, "save_upload", save_then_reuse_section_id)
    monkeypatch.setattr(admin_uploads.jobs, "_pool", None)
    media_dir = config.MEDIA_DIR / str(target["id"])
    last_job_id = db.one("SELECT COALESCE(MAX(id), 0) AS id FROM jobs")["id"]
    rev_before = target["content_rev"]

    r = admin.post(
        f"/admin/galleries/{target['id']}/upload?section_id={target_section['id']}",
        files=[
            ("files", ("first.jpg", _jpeg_bytes(), "image/jpeg")),
            ("files", ("raced.jpg", _jpeg_bytes(), "image/jpeg")),
        ],
    )
    assert save_count == 2
    assert inner_result == {"accepted": 1, "rejected": []}
    assert r.status_code == 400
    assert r.json()["detail"] == "unknown section"
    assets = db.all_("SELECT * FROM assets WHERE gallery_id=?", (target["id"],))
    assert len(assets) == 1 and assets[0]["section_id"] is None
    assert not db.one(
        """SELECT a.id FROM assets AS a JOIN sections AS s ON s.id=a.section_id
           WHERE a.gallery_id<>s.gallery_id"""
    )
    assert (
        db.one("SELECT content_rev FROM galleries WHERE id=?", (target["id"],))["content_rev"]
        == rev_before + 1
    )
    new_jobs = db.all_("SELECT * FROM jobs WHERE id>? ORDER BY id", (last_job_id,))
    assert len(new_jobs) == 1 and new_jobs[0]["status"] == "queued"
    assert {path.name for path in (media_dir / "original").iterdir()} == {assets[0]["stored"]}
    assert (media_dir / "web").is_dir() and (media_dir / "thumb").is_dir()

    admin_uploads.jobs._execute(new_jobs[0]["id"])
    asset = db.one("SELECT * FROM assets WHERE id=?", (assets[0]["id"],))
    assert asset["status"] == "ready"
    stem = Path(asset["stored"]).stem
    assert (media_dir / "web" / f"{stem}.jpg").is_file()
    assert (media_dir / "thumb" / f"{stem}.jpg").is_file()


def test_section_jump_nav(admin):

    admin.post(
        "/admin/galleries", data={"title": "Navved", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    with TestClient(app):  # fresh lifespan: job pool may have been stopped upstream
        admin.post(
            f"/admin/galleries/{g['id']}/upload",
            files=[("files", (f"{n}.jpg", _jpeg_bytes(), "image/jpeg")) for n in "ab"],
        )
        for _ in range(50):
            rows = db.all_("SELECT status FROM assets WHERE gallery_id=?", (g["id"],))
            if rows and all(r["status"] == "ready" for r in rows):
                break
            time.sleep(0.2)
        assert all(r["status"] == "ready" for r in rows)

    a1, a2 = [
        r["id"] for r in db.all_("SELECT id FROM assets WHERE gallery_id=? ORDER BY id", (g["id"],))
    ]
    hero = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Hero Dishes'", (g["id"],))
    admin.post(
        f"/admin/galleries/{g['id']}/assets/{a1}/section", data={"section_id": str(hero["id"])}
    )
    # a2 to the unsectioned "More" bucket so we have one section + More = 2 targets
    # (uploads now default into the first section, so push it back out)
    admin.post(
        f"/admin/galleries/{g['id']}/assets/bulk-section",
        data={"section_id": "", "asset_ids": [str(a2)]},
    )
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Navved", "pin": "5151", "published": "true"},
    )

    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": "5151"})
        # one populated section + unsectioned "More" = 2 targets → nav renders
        r = pub.get(f"/g/{g['slug']}")
        assert "section-nav" in r.text
        assert f'href="#sec-{hero["id"]}"' in r.text and 'id="sec-more"' in r.text

        # per-section ZIP: heading carries ↓, email gate first, then exact bundle
        assert f"/g/{g['slug']}/download/section/{hero['id']}" in r.text
        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}", follow_redirects=False)
        assert r2.status_code == 303  # no email yet → gate
        # /download?section=N must render the gate (catches decorator-misplacement)
        gate = pub.get(f"/g/{g['slug']}/download?section={hero['id']}")
        assert gate.status_code == 200 and 'name="section"' in gate.text
        assert f'value="{hero["id"]}"' in gate.text
        pub.post(
            f"/g/{g['slug']}/email",
            data={"email": "nav@bistro.com", "section": str(hero["id"])},
            follow_redirects=False,
        )
        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}")
        assert r2.headers["content-type"] == "application/zip"
        assert zipfile.ZipFile(io.BytesIO(r2.content)).namelist() == ["a.jpg"]
        # empty and foreign sections refuse
        empty = db.one("SELECT id FROM sections WHERE gallery_id=? AND name='Drinks'", (g["id"],))
        assert pub.get(f"/g/{g['slug']}/download/section/{empty['id']}").status_code == 404
        assert pub.get(f"/g/{g['slug']}/download/section/999999").status_code == 404

        # collapse everything into one chapter → nav disappears (nothing to jump between)
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{a2}/section", data={"section_id": str(hero["id"])}
        )
        r = pub.get(f"/g/{g['slug']}")
        assert "section-nav" not in r.text and f'id="sec-{hero["id"]}"' in r.text

        # section content changed → new content-keyed bundle, old rev pruned
        from app import config

        r2 = pub.get(f"/g/{g['slug']}/download/section/{hero['id']}")
        assert sorted(zipfile.ZipFile(io.BytesIO(r2.content)).namelist()) == ["a.jpg", "b.jpg"]
        assert len(list(config.ZIP_DIR.glob(f"g{g['id']}-s{hero['id']}-*.zip"))) == 1


def test_jobs_admin_view(admin):

    # plant a failed job whose handler no-ops cleanly on retry (asset gone)
    jid = db.run(
        "INSERT INTO jobs (kind, payload, status, attempts, error) VALUES "
        "('social_crops', '{\"asset_id\": 999999}', 'failed', 3, 'boom')"
    )

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
    assert admin.post(f"/admin/jobs/{jid}/retry", follow_redirects=False).status_code == 404


def test_case_studies(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    photos = db.all_("SELECT * FROM assets WHERE gallery_id=? AND kind='photo'", (g["id"],))
    assert photos, "fixture should leave at least one photo to star"

    with TestClient(app) as pub:
        # before publishing: /work is empty, /work/{slug} 404s, sitemap silent
        r = pub.get("/work")
        assert r.status_code == 200 and "New work is being curated" in r.text
        assert pub.get(f"/work/{g['slug']}").status_code == 404
        sm = pub.get("/sitemap.xml").text
        assert f"/work/{g['slug']}" not in sm

        # star a photo + fill case-study fields via the admin settings form
        admin.post(
            f"/admin/galleries/{g['id']}/assets/{photos[0]['id']}/portfolio", follow_redirects=False
        )
        admin.post(
            f"/admin/galleries/{g['id']}/settings",
            data={
                "title": g["title"],
                "client_name": g["client_name"] or "",
                "pin": g["pin"],
                "expires_at": "",
                "published": "true",
                "captions": "",
                "cs_published": "true",
                "cs_tagline": "Spring menu for Café Lune",
                "cs_brief": "A 40-dish refresh shot over two days.",
                "cs_credits": "Chef: Mara Sun\nStylist: Lou Mendez",
                "cs_location": "Cleveland, OH",
            },
        )

        # /work index lists the study with hero + tagline; tile links to /work/{slug}
        r = pub.get("/work")
        assert "Spring menu for Café Lune" in r.text
        assert f'href="/work/{g["slug"]}"' in r.text
        assert f"/site/img/{photos[0]['id']}" in r.text
        assert 'content="index, follow"' in r.text and "x-robots-tag" not in r.headers
        # /portfolio surfaces published studies as a trust-building band.
        portfolio = pub.get("/portfolio").text
        assert "Featured clients" in portfolio
        assert f'href="/work/{g["slug"]}"' in portfolio

        # /work/{slug} renders brief, credits, location, photo, and OG/SEO meta
        r = pub.get(f"/work/{g['slug']}")
        assert r.status_code == 200
        assert "x-robots-tag" not in r.headers
        assert "Spring menu for Café Lune" in r.text
        assert "40-dish refresh" in r.text
        # credits render as a label/value grid ("Chef: Mara Sun" is split on the colon)
        assert "work-credit-label" in r.text
        assert "Mara Sun" in r.text and "Lou Mendez" in r.text
        assert "Cleveland, OH" in r.text
        from app import config as cfg

        assert (
            f'property="og:image" content="{cfg.BASE_URL}/site/img/{photos[0]["id"]}"'
        ) in r.text
        assert 'property="og:type" content="article"' in r.text
        assert 'name="description"' in r.text and "40-dish refresh" in r.text
        # brief in the og:description too (first 200 chars)
        assert 'property="og:description" content="A 40-dish refresh' in r.text
        # the hero figure actually renders — hero is {% set %} at template top
        # level; a set inside one block is invisible to sibling blocks, which
        # silently dropped the first photo from the page entirely
        assert 'class="work-detail-hero"' in r.text
        assert f"/site/img/{photos[0]['id']}?variant=web" in r.text
        # the og override must not eat the base head: fonts, site JS, canonical
        assert "/static/fonts.css" in r.text and "/static/site.js" in r.text
        assert f'rel="canonical" href="{cfg.BASE_URL}/work/{g["slug"]}"' in r.text
        assert '"@type": "Article"' in r.text
        assert '"@type": "BreadcrumbList"' in r.text
        # Untagged legacy work maps to F&B, so its CTA stays menu-specific
        # and carries a structured service prefill.
        assert "shoot your menu." in r.text
        assert 'href="/contact?service=Food%20%26%20Beverage"' in r.text

        # sitemap now lists the case study; robots.txt unchanged (no exclusion needed)
        sm = pub.get("/sitemap.xml").text
        assert f"<loc>{cfg.BASE_URL}/work/{g['slug']}</loc>" in sm
        assert f"<loc>{cfg.BASE_URL}/work</loc>" in sm
        assert "<lastmod>" in sm

        # noindex on a non-/work prefix stays noindex (middleware is path-prefixed)
        assert "x-robots-tag" in pub.get(f"/g/{g['slug']}").headers

        # unpublishing the case study hides it again, without touching the client gallery
        admin.post(
            f"/admin/galleries/{g['id']}/settings",
            data={
                "title": g["title"],
                "client_name": g["client_name"] or "",
                "pin": g["pin"],
                "expires_at": "",
                "published": "true",
                "captions": "",
                "cs_tagline": "",
                "cs_brief": "",
                "cs_credits": "",
                "cs_location": "",
            },
        )
        assert pub.get(f"/work/{g['slug']}").status_code == 404
        assert "New work is being curated" in pub.get("/work").text
        # client gallery still serves — the case-study flag is independent
        assert pub.get(f"/g/{g['slug']}").status_code == 200


def test_case_study_images_use_actual_derivative_metadata(admin):
    import re
    import shutil

    slug = "image-delivery-metadata"
    gallery_id = db.run(
        """INSERT INTO galleries
           (slug, title, client_name, pin, cs_published, cs_tagline, cs_brief,
            cs_location, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            slug,
            "Image Delivery Metadata",
            "Metadata Client",
            "4455",
            1,
            "A portrait-oriented case study",
            "Known derivative files should drive the public image markup.",
            "Asheville, NC",
            "2999-01-01 00:00:00",
        ),
    )
    stored = "deliverymetadata.jpg"
    asset_id = db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, width, height, position, portfolio)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            gallery_id,
            "photo",
            "source.jpg",
            stored,
            "ready",
            6000,
            4000,
            0,
            1,
        ),
    )
    media = config.MEDIA_DIR / str(gallery_id)
    try:
        # Deliberately disagree with the landscape source metadata above. The
        # public descriptors must follow these portrait derivative bytes.
        for variant, size in (("thumb", (80, 120)), ("web", (320, 480))):
            directory = media / variant
            directory.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", size, (80, 100, 120)).save(directory / stored, "JPEG")

        web_url = f"/site/img/{asset_id}?variant=web"
        thumb_url = f"/site/img/{asset_id}?variant=thumb"
        expected_srcset = f"{thumb_url} 80w, {web_url} 320w"

        with TestClient(app) as public:
            work = public.get("/work")
            detail = public.get(f"/work/{slug}")

        assert work.status_code == 200
        work_hero = re.search(rf'<img src="{re.escape(web_url)}"[^>]*>', work.text).group(0)
        assert 'width="320" height="480"' in work_hero
        assert f'srcset="{expected_srcset}"' in work_hero
        assert 'sizes="(max-width: 900px) 100vw, 55vw"' in work_hero
        assert 'fetchpriority="high"' in work_hero
        assert 'loading="lazy"' not in work_hero
        assert f'<link rel="preload" as="image" href="{web_url}" fetchpriority="high"' in work.text
        assert f'imagesrcset="{expected_srcset}"' in work.text

        assert detail.status_code == 200
        detail_hero = re.search(rf'<img src="{re.escape(web_url)}"[^>]*>', detail.text).group(0)
        assert 'width="320" height="480"' in detail_hero
        assert f'srcset="{expected_srcset}"' in detail_hero
        assert 'sizes="(max-width: 1100px) 100vw, 1320px"' in detail_hero
        assert 'fetchpriority="high"' in detail_hero
        assert 'loading="lazy"' not in detail_hero
        assert (
            f'<link rel="preload" as="image" href="{web_url}" fetchpriority="high"' in detail.text
        )
        assert f'imagesrcset="{expected_srcset}"' in detail.text

        gallery_tile = re.search(rf'<img src="{re.escape(thumb_url)}"[^>]*>', detail.text).group(0)
        assert 'width="80" height="120"' in gallery_tile
        assert f'srcset="{expected_srcset}"' in gallery_tile
        assert 'loading="lazy"' in gallery_tile
        assert 'fetchpriority="high"' not in gallery_tile
        # A single valid derivative becomes the sole source; no speculative
        # candidate or duplicate fetch is advertised.
        (media / "web" / stored).unlink()
        with TestClient(app) as public:
            thumb_only = public.get(f"/work/{slug}")
        thumb_only_hero = re.search(
            rf'<img src="{re.escape(thumb_url)}"[^>]*>', thumb_only.text
        ).group(0)
        assert 'width="80" height="120"' in thumb_only_hero
        assert "srcset=" not in thumb_only_hero and "sizes=" not in thumb_only_hero
        assert 'fetchpriority="high"' in thumb_only_hero
        assert f'<link rel="preload" as="image" href="{thumb_url}" fetchpriority="high"' in (
            thumb_only.text
        )
        assert "imagesrcset=" not in thumb_only.text

        # If both encoded derivatives are gone, preserve the old broken-image
        # fallback without preloading or prioritizing a URL known to be absent.
        (media / "thumb" / stored).unlink()
        with TestClient(app) as public:
            unavailable = public.get(f"/work/{slug}")
        unavailable_hero = re.search(
            rf'<img src="{re.escape(web_url)}"[^>]*>', unavailable.text
        ).group(0)
        assert 'loading="lazy"' in unavailable_hero
        assert 'fetchpriority="high"' not in unavailable_hero
        assert "width=" not in unavailable_hero
        assert "srcset=" not in unavailable_hero and "sizes=" not in unavailable_hero
        assert f'<link rel="preload" as="image" href="{web_url}"' not in unavailable.text
    finally:
        db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))
        shutil.rmtree(media, ignore_errors=True)


def test_secondary_marketing_images_use_actual_derivative_metadata(
    admin, tmp_path, monkeypatch, request
):
    from app import security

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "ABOUT_PORTRAIT", "missing-test-portrait.jpg")
    monkeypatch.setattr(security, "inquiry_throttled", lambda _ip, _bucket: False)
    gallery_id = db.run(
        """INSERT INTO galleries
           (slug, title, client_name, pin, cs_published, cs_tagline, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "secondary-image-metadata",
            "Secondary Image Metadata",
            "Metadata Client",
            "4455",
            1,
            "Encoded dimensions, everywhere",
            "2999-01-02 00:00:00",
        ),
    )
    request.addfinalizer(lambda: db.run("DELETE FROM galleries WHERE id=?", (gallery_id,)))
    stored = "secondary-image.jpg"
    asset_id = db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, width, height, portfolio,
            portfolio_tag)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            gallery_id,
            "photo",
            stored,
            stored,
            "ready",
            6000,
            4000,
            1,
            "fb/dishes",
        ),
    )
    media = config.MEDIA_DIR / str(gallery_id)
    for variant, size in (("thumb", (80, 120)), ("web", (320, 480))):
        directory = media / variant
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (80, 100, 120)).save(directory / stored, "JPEG")

    thumb_url = f"/site/img/{asset_id}?variant=thumb"
    web_url = f"/site/img/{asset_id}?variant=web"
    srcset = f"{thumb_url} 80w, {web_url} 320w"
    scopes = {
        "home": (
            r'<a class="sr-card sp-door sr-stock-fb".*?</a>',
            thumb_url,
            "(max-width: 700px) 92vw, 440px",
        ),
        "portfolio": (
            r'<a class="work-editorial-card" href="/work/secondary-image-metadata">.*?</a>',
            thumb_url,
            "(max-width: 700px) 100vw, 33vw",
        ),
        "about": (
            r'<div class="about-photo ab-photo".*?</div>',
            web_url,
            "(max-width: 900px) 100vw, 40vw",
        ),
        "contact": (
            r'<div class="contact-photo".*?</div>',
            web_url,
            "(max-width: 900px) 100vw, 38vw",
        ),
        "contact_error": (
            r'<div class="contact-photo".*?</div>',
            web_url,
            "(max-width: 900px) 100vw, 38vw",
        ),
        "book": (
            r'<div class="book-aside-photo".*?</div>',
            web_url,
            "(max-width: 900px) 100vw, 40vw",
        ),
    }

    def responses():
        with TestClient(app) as public:
            return {
                "home": public.get("/"),
                "portfolio": public.get("/portfolio"),
                "about": public.get("/about"),
                "contact": public.get("/contact"),
                "contact_error": public.post(
                    "/contact",
                    data={"name": "Metadata", "email": "invalid", "message": "Test"},
                ),
                "book": public.get("/book"),
            }

    def scoped_image(response, pattern):
        scope = re.search(pattern, response.text, re.S)
        assert scope is not None
        tag = re.search(r"<img\b[^>]*>", scope.group(0))
        assert tag is not None
        return tag.group(0)

    def attribute(tag, name):
        match = re.search(rf'\s{re.escape(name)}="([^"]*)"', tag)
        return match.group(1) if match else None

    rendered = responses()
    for name, (pattern, source, sizes) in scopes.items():
        expected_status = 400 if name == "contact_error" else 200
        assert rendered[name].status_code == expected_status
        tag = scoped_image(rendered[name], pattern)
        expected_size = (80, 120) if source == thumb_url else (320, 480)
        assert attribute(tag, "src") == source
        assert attribute(tag, "width") == str(expected_size[0])
        assert attribute(tag, "height") == str(expected_size[1])
        assert attribute(tag, "srcset") == srcset
        assert attribute(tag, "sizes") == sizes
        assert "480w" not in tag and "2048w" not in tag

    # Remove the preferred thumbnail to force the two thumb-primary card
    # surfaces through the same validated web fallback as every other surface.
    (media / "thumb" / stored).unlink()
    web_only = responses()
    for name, (pattern, _source, _sizes) in scopes.items():
        expected_status = 400 if name == "contact_error" else 200
        assert web_only[name].status_code == expected_status
        tag = scoped_image(web_only[name], pattern)
        assert attribute(tag, "src") == web_url
        assert attribute(tag, "width") == "320"
        assert attribute(tag, "height") == "480"
        assert attribute(tag, "srcset") is None
        assert attribute(tag, "sizes") is None
    Image.new("RGB", (80, 120), (80, 100, 120)).save(media / "thumb" / stored, "JPEG")

    # A lone valid derivative becomes the sole identity everywhere; candidate
    # selection hints must disappear instead of describing a missing web file.
    (media / "web" / stored).unlink()
    fallback = responses()
    for name, (pattern, _source, _sizes) in scopes.items():
        expected_status = 400 if name == "contact_error" else 200
        assert fallback[name].status_code == expected_status
        tag = scoped_image(fallback[name], pattern)
        assert attribute(tag, "src") == thumb_url
        assert attribute(tag, "width") == "80"
        assert attribute(tag, "height") == "120"
        assert attribute(tag, "srcset") is None
        assert attribute(tag, "sizes") is None

    # With neither derivative available, preserve each surface's previous
    # plain URL fallback but never claim dimensions for bytes that were not read.
    (media / "thumb" / stored).unlink()
    unavailable = responses()
    for name, (pattern, original_source, _sizes) in scopes.items():
        expected_status = 400 if name == "contact_error" else 200
        assert unavailable[name].status_code == expected_status
        tag = scoped_image(unavailable[name], pattern)
        assert attribute(tag, "src") == original_source
        assert attribute(tag, "width") is None
        assert attribute(tag, "height") is None
        assert attribute(tag, "srcset") is None
        assert attribute(tag, "sizes") is None


def test_portfolio_tiles_use_actual_derivative_metadata(admin, tmp_path, monkeypatch, request):
    from collections import Counter

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("portfolio-delivery-metadata", "Portfolio Delivery Metadata", "4455"),
    )
    cleanup_done = False

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        try:
            db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))
        finally:
            cleanup_done = True

    request.addfinalizer(cleanup)

    def add_asset(kind, stored):
        return db.run(
            """INSERT INTO assets
               (gallery_id, kind, filename, stored, status, width, height, portfolio,
                portfolio_tag)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (gallery_id, kind, stored, stored, "ready", 6000, 4000, 1, "fb/dishes"),
        )

    # Insert the photo first, then the video: the mixed ID ordering makes the
    # video—not merely the first photo—the initial masonry/LCP candidate.
    photo_id = add_asset("photo", "portfolio-photo.jpg")
    video_id = add_asset("video", "portfolio-video.mp4")
    media = config.MEDIA_DIR / str(gallery_id)
    for variant, size in (("thumb", (80, 120)), ("web", (320, 480))):
        directory = media / variant
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (80, 100, 120)).save(directory / "portfolio-photo.jpg", "JPEG")
    (media / "thumb").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (90, 51), (40, 60, 80)).save(media / "thumb" / "portfolio-video.jpg", "JPEG")
    (media / "web").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (360, 203), (40, 60, 80)).save(
        media / "web" / "portfolio-video_poster.jpg", "JPEG"
    )

    tile_sizes = "(max-width: 641px) calc(100vw - 32px), (max-width: 700px) calc(50vw - 21px), (max-width: 705px) calc(100vw - 96px), (max-width: 1015px) calc(50vw - 53px), (max-width: 1325px) calc(33.333333vw - 38.666667px), (max-width: 1415px) calc(25vw - 31.5px), 322.5px"
    video_url = f"/site/img/{video_id}?variant=thumb"
    video_poster = f"/site/poster/{video_id}"
    photo_thumb = f"/site/img/{photo_id}?variant=thumb"
    photo_web = f"/site/img/{photo_id}?variant=web"
    photo_srcset = f"{photo_thumb} 80w, {photo_web} 320w"

    def masonry_figures(response):
        masonry = re.search(
            r'<div class="portfolio-masonry">(.*?)</div>\s*<p class="pf-empty"',
            response.text,
            re.S,
        )
        assert masonry is not None
        figures = re.findall(
            r'<figure class="tile pf-masonry-tile[^>]*>.*?</figure>',
            masonry.group(1),
            re.S,
        )
        assert figures
        return figures

    def image_tag(figure):
        tag = re.search(r"<img\b[^>]*>", figure)
        assert tag is not None
        return tag.group(0)

    def assert_priority_budget(response, expected_url):
        image_preloads = [
            tag
            for tag in re.findall(r"<link\b[^>]*>", response.text)
            if 'rel="preload"' in tag and 'as="image"' in tag
        ]
        assert len(image_preloads) == 1
        priority_tags = re.findall(r'<(?:link|img)\b[^>]*fetchpriority="high"[^>]*>', response.text)
        targets = []
        for tag in priority_tags:
            target = re.search(r'(?:href|src)="([^"]+)"', tag)
            assert target is not None
            targets.append(target.group(1))
        assert len(targets) == 2
        assert Counter(targets) == Counter({expected_url: 2})
        return image_preloads[0]

    with TestClient(app) as public:
        mixed = public.get("/portfolio")
    figures = masonry_figures(mixed)
    assert len(figures) >= 2
    assert f'data-web="/site/vid/{video_id}"' in figures[0]
    video_tag = image_tag(figures[0])
    assert f'src="{video_url}"' in video_tag
    assert 'width="90" height="51"' in video_tag
    assert 'fetchpriority="high"' in video_tag and 'loading="lazy"' not in video_tag
    assert "srcset=" not in video_tag
    video_preload = assert_priority_budget(mixed, video_url)
    assert "imagesrcset=" not in video_preload
    assert "imagesizes=" not in video_preload
    assert f'data-poster="{video_poster}"' in figures[0]

    # The lightbox poster follows the same validated full-poster -> tile-thumb
    # -> omitted contract as the marketing heroes. A corrupt JPEG must not stay
    # wired merely because the filesystem entry still exists.
    (media / "web" / "portfolio-video_poster.jpg").write_bytes(b"corrupt poster")
    with TestClient(app) as public:
        fallback_poster = public.get("/portfolio")
    fallback_video_figure = masonry_figures(fallback_poster)[0]
    assert f'data-poster="{video_url}"' in fallback_video_figure
    assert f'data-poster="{video_poster}"' not in fallback_video_figure

    # Even while lazy, the next photo must describe its encoded derivatives,
    # not the 6000x4000 source row or configured 480/2048 maxima.
    assert f'data-web="{photo_web}"' in figures[1]
    photo_tag = image_tag(figures[1])
    assert f'src="{photo_thumb}"' in photo_tag
    assert 'width="80" height="120"' in photo_tag
    assert f'srcset="{photo_srcset}"' in photo_tag
    assert f'sizes="{tile_sizes}"' in photo_tag
    assert 'loading="lazy"' in photo_tag and 'fetchpriority="high"' not in photo_tag

    # A known-bad lead thumbnail is not speculatively prioritized and does not
    # promote a different DOM tile behind the visitor's first visible tile.
    (media / "thumb" / "portfolio-video.jpg").write_bytes(b"corrupt thumb")
    with TestClient(app) as public:
        unavailable_video = public.get("/portfolio")
    unavailable_video_figure = masonry_figures(unavailable_video)[0]
    unavailable_video_tag = image_tag(unavailable_video_figure)
    assert "data-poster=" not in unavailable_video_figure
    assert 'loading="lazy"' in unavailable_video_tag
    assert 'fetchpriority="high"' not in unavailable_video_tag
    assert "width=" not in unavailable_video_tag and "height=" not in unavailable_video_tag
    assert "srcset=" not in unavailable_video_tag and "sizes=" not in unavailable_video_tag
    assert '<link rel="preload" as="image"' not in unavailable_video.text
    assert 'fetchpriority="high"' not in unavailable_video.text

    db.run("UPDATE assets SET portfolio=0 WHERE id=?", (video_id,))
    with TestClient(app) as public:
        photo_first = public.get("/portfolio")
    photo_first_figure = masonry_figures(photo_first)[0]
    photo_first_tag = image_tag(photo_first_figure)
    assert f'src="{photo_thumb}"' in photo_first_tag
    assert 'width="80" height="120"' in photo_first_tag
    assert f'srcset="{photo_srcset}"' in photo_first_tag
    assert f'sizes="{tile_sizes}"' in photo_first_tag
    assert 'fetchpriority="high"' in photo_first_tag
    assert 'loading="lazy"' not in photo_first_tag
    preload = re.search(
        rf'<link rel="preload" as="image" href="{re.escape(photo_thumb)}"[^>]*>',
        photo_first.text,
    )
    assert preload is not None
    assert f'imagesrcset="{photo_srcset}"' in preload.group(0)
    assert f'imagesizes="{tile_sizes}"' in preload.group(0)
    assert_priority_budget(photo_first, photo_thumb)

    # The opposite single-derivative direction is equally exact: a valid web
    # image becomes the grid, preload, and lightbox identity when thumb is gone.
    (media / "thumb" / "portfolio-photo.jpg").unlink()
    with TestClient(app) as public:
        web_only = public.get("/portfolio")
    web_only_figure = masonry_figures(web_only)[0]
    web_only_tag = image_tag(web_only_figure)
    assert f'data-web="{photo_web}"' in web_only_figure
    assert f'src="{photo_web}"' in web_only_tag
    assert 'width="320" height="480"' in web_only_tag
    assert "srcset=" not in web_only_tag and "sizes=" not in web_only_tag
    assert 'fetchpriority="high"' in web_only_tag and 'loading="lazy"' not in web_only_tag
    web_only_preload = assert_priority_budget(web_only, photo_web)
    assert "imagesrcset=" not in web_only_preload
    assert "imagesizes=" not in web_only_preload
    Image.new("RGB", (80, 120), (80, 100, 120)).save(
        media / "thumb" / "portfolio-photo.jpg", "JPEG"
    )

    # A single valid derivative is the sole image identity; a fully unavailable
    # lead preserves the lazy fallback without false dimensions or priority.
    (media / "web" / "portfolio-photo.jpg").unlink()
    with TestClient(app) as public:
        thumb_only = public.get("/portfolio")
    thumb_only_figure = masonry_figures(thumb_only)[0]
    thumb_only_tag = image_tag(thumb_only_figure)
    assert f'data-web="{photo_thumb}"' in thumb_only_figure
    assert 'width="80" height="120"' in thumb_only_tag
    assert "srcset=" not in thumb_only_tag and "sizes=" not in thumb_only_tag
    assert 'fetchpriority="high"' in thumb_only_tag and 'loading="lazy"' not in thumb_only_tag
    thumb_only_preload = assert_priority_budget(thumb_only, photo_thumb)
    assert "imagesrcset=" not in thumb_only_preload
    assert "imagesizes=" not in thumb_only_preload

    (media / "thumb" / "portfolio-photo.jpg").write_bytes(b"corrupt thumb")
    with TestClient(app) as public:
        unavailable_photo = public.get("/portfolio")
    unavailable_photo_tag = image_tag(masonry_figures(unavailable_photo)[0])
    assert 'loading="lazy"' in unavailable_photo_tag
    assert 'fetchpriority="high"' not in unavailable_photo_tag
    assert "width=" not in unavailable_photo_tag and "height=" not in unavailable_photo_tag
    assert "srcset=" not in unavailable_photo_tag and "sizes=" not in unavailable_photo_tag
    assert '<link rel="preload" as="image"' not in unavailable_photo.text
    assert 'fetchpriority="high"' not in unavailable_photo.text

    cleanup()


def test_marketing_heroes_use_actual_media_metadata(admin, tmp_path, monkeypatch, request):
    import re

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    existing_video_ids = [
        row["id"] for row in db.all_("SELECT id FROM assets WHERE kind='video' AND portfolio=1")
    ]

    gallery_id = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("marketing-hero-metadata", "Marketing Hero Metadata", "4455"),
    )

    cleanup_done = False

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        try:
            db.run("DELETE FROM galleries WHERE id=?", (gallery_id,))
        finally:
            try:
                for asset_id in existing_video_ids:
                    db.run("UPDATE assets SET portfolio=1 WHERE id=?", (asset_id,))
            finally:
                cleanup_done = True

    request.addfinalizer(cleanup)

    def add_asset(kind, stored, tag):
        return db.run(
            """INSERT INTO assets
               (gallery_id, kind, filename, stored, status, width, height, portfolio,
                portfolio_tag)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (gallery_id, kind, stored, stored, "ready", 6000, 4000, 1, tag),
        )

    re_photo = add_asset("photo", "hero-re.jpg", "re/exteriors")
    pl_photo = add_asset("photo", "hero-pl.jpg", "pl/headshots")
    fb_photo = add_asset("photo", "hero-fb.jpg", "fb/dishes")
    re_video = add_asset("video", "hero-re-video.mp4", "re/motion")
    fb_video = add_asset("video", "hero-fb-video.mp4", "fb/motion")
    pl_video = add_asset("video", "hero-pl-video.mp4", "pl/motion")
    media = config.MEDIA_DIR / str(gallery_id)

    photo_sizes = {
        "hero-re.jpg": ((120, 80), (480, 320)),
        "hero-pl.jpg": ((80, 120), (320, 480)),
        "hero-fb.jpg": ((100, 100), (400, 400)),
    }
    for stored, (thumb_size, web_size) in photo_sizes.items():
        for variant, size in (("thumb", thumb_size), ("web", web_size)):
            directory = media / variant
            directory.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", size, (80, 100, 120)).save(directory / stored, "JPEG")

    video_sizes = {
        "hero-re-video": ((420, 236), (105, 59)),
        "hero-fb-video": ((300, 169), (75, 42)),
        "hero-pl-video": ((360, 203), (90, 51)),
    }
    for stem, (poster_size, thumb_size) in video_sizes.items():
        (media / "web").mkdir(parents=True, exist_ok=True)
        (media / "thumb").mkdir(parents=True, exist_ok=True)
        (media / "web" / f"{stem}.mp4").write_bytes(b"video fixture")
        Image.new("RGB", poster_size, (40, 60, 80)).save(
            media / "web" / f"{stem}_poster.jpg", "JPEG"
        )
        Image.new("RGB", thumb_size, (40, 60, 80)).save(media / "thumb" / f"{stem}.jpg", "JPEG")

    def assert_priority_budget(response, expected_url):
        preload_tags = [
            tag
            for tag in re.findall(r"<link\b[^>]*>", response.text)
            if 'rel="preload"' in tag and 'as="image"' in tag
        ]
        assert len(preload_tags) == 1
        priority_tags = re.findall(r'<(?:link|img)\b[^>]*fetchpriority="high"[^>]*>', response.text)
        targets = []
        for tag in priority_tags:
            target = re.search(r'(?:href|src)="([^"]+)"', tag)
            assert target is not None
            targets.append(target.group(1))
        assert set(targets) == {expected_url}

    def assert_photo_hero(response, asset_id, thumb_size, web_size, sizes):
        thumb_url = f"/site/img/{asset_id}?variant=thumb"
        web_url = f"/site/img/{asset_id}?variant=web"
        srcset = f"{thumb_url} {thumb_size[0]}w, {web_url} {web_size[0]}w"
        tag = re.search(rf'<img src="{re.escape(web_url)}"[^>]*>', response.text).group(0)
        assert f'width="{web_size[0]}" height="{web_size[1]}"' in tag
        assert f'srcset="{srcset}"' in tag and f'sizes="{sizes}"' in tag
        assert 'fetchpriority="high"' in tag and 'loading="lazy"' not in tag
        preload_tag = re.search(
            rf'<link rel="preload" as="image" href="{re.escape(web_url)}"[^>]*>', response.text
        ).group(0)
        assert preload_tag.startswith(f'<link rel="preload" as="image" href="{web_url}"')
        assert 'fetchpriority="high"' in preload_tag
        assert f'imagesrcset="{srcset}"' in preload_tag
        assert f'imagesizes="{sizes}"' in preload_tag
        assert_priority_budget(response, web_url)

    def assert_video_hero(response, url, size):
        tag = re.search(rf'<video[^>]*poster="{re.escape(url)}"[^>]*>', response.text).group(0)
        assert f'width="{size[0]}" height="{size[1]}"' in tag
        preload_tag = re.search(
            rf'<link rel="preload" as="image" href="{re.escape(url)}"[^>]*>',
            response.text,
        ).group(0)
        assert preload_tag == (f'<link rel="preload" as="image" href="{url}" fetchpriority="high">')
        assert "imagesrcset=" not in preload_tag
        assert_priority_budget(response, url)

    def assert_plain_photo_hero(response, url, size):
        tag = re.search(rf'<img src="{re.escape(url)}"[^>]*>', response.text).group(0)
        assert f'width="{size[0]}" height="{size[1]}"' in tag
        assert "srcset=" not in tag and "sizes=" not in tag
        assert 'fetchpriority="high"' in tag and 'loading="lazy"' not in tag
        preload_tag = re.search(
            rf'<link rel="preload" as="image" href="{re.escape(url)}"[^>]*>', response.text
        ).group(0)
        assert preload_tag.startswith(
            f'<link rel="preload" as="image" href="{url}" fetchpriority="high"'
        )
        assert "imagesrcset=" not in preload_tag and "imagesizes=" not in preload_tag
        assert_priority_budget(response, url)

    try:
        for asset_id in existing_video_ids:
            db.run("UPDATE assets SET portfolio=0 WHERE id=?", (asset_id,))
        with TestClient(app) as public:
            home = public.get("/")
            real_estate = public.get("/real-estate")
            portraits = public.get("/portraits")
            food = public.get("/food-beverage")

            assert_video_hero(home, f"/site/poster/{pl_video}", (360, 203))
            assert_video_hero(real_estate, f"/site/poster/{re_video}", (420, 236))
            assert_video_hero(food, f"/site/poster/{fb_video}", (300, 169))
            assert_photo_hero(
                portraits,
                pl_photo,
                (80, 120),
                (320, 480),
                "(max-width: 1000px) 100vw, 560px",
            )
            assert f'<link rel="preload" as="image" href="/site/poster/{pl_video}"' not in (
                portraits.text
            )

            assert Image.open(io.BytesIO(public.get(f"/site/poster/{pl_video}").content)).size == (
                360,
                203,
            )
            assert Image.open(
                io.BytesIO(public.get(f"/site/img/{re_photo}?variant=web").content)
            ).size == (480, 320)
            video_response = public.get(f"/site/vid/{re_video}")
            assert video_response.status_code == 200 and video_response.content == b"video fixture"

        (media / "web" / "hero-pl-video_poster.jpg").write_bytes(b"corrupt poster")
        with TestClient(app) as public:
            thumb_fallback = public.get("/")
        assert_video_hero(
            thumb_fallback,
            f"/site/img/{pl_video}?variant=thumb",
            (90, 51),
        )

        (media / "thumb" / "hero-pl-video.jpg").write_bytes(b"corrupt thumb")
        with TestClient(app) as public:
            unavailable = public.get("/")
        video_tag = re.search(
            rf'<video[^>]*>\s*<source src="/site/vid/{pl_video}"', unavailable.text
        ).group(0)
        assert "poster=" not in video_tag and "width=" not in video_tag
        assert '<link rel="preload" as="image"' not in unavailable.text
        assert 'fetchpriority="high"' not in unavailable.text

        db.run(
            "UPDATE assets SET portfolio=0 WHERE id IN (?,?,?)",
            (re_video, fb_video, pl_video),
        )
        with TestClient(app) as public:
            home_photo = public.get("/")
            real_estate_photo = public.get("/real-estate")
            food_photo = public.get("/food-beverage")
        assert_photo_hero(
            real_estate_photo,
            re_photo,
            (120, 80),
            (480, 320),
            "(max-width: 1000px) 100vw, 900px",
        )
        assert_photo_hero(
            home_photo,
            fb_photo,
            (100, 100),
            (400, 400),
            "(max-width: 700px) 100vw, 1344px",
        )
        assert_photo_hero(food_photo, fb_photo, (100, 100), (400, 400), "100vw")
        (media / "web" / "hero-fb.jpg").write_bytes(b"corrupt photo")
        with TestClient(app) as public:
            home_thumb = public.get("/")
            food_thumb = public.get("/food-beverage")
        thumb_url = f"/site/img/{fb_photo}?variant=thumb"
        assert_plain_photo_hero(home_thumb, thumb_url, (100, 100))
        assert_plain_photo_hero(food_thumb, thumb_url, (100, 100))

        (media / "thumb" / "hero-fb.jpg").write_bytes(b"corrupt thumb")
        with TestClient(app) as public:
            photo_unavailable = public.get("/")
        web_url = f"/site/img/{fb_photo}?variant=web"
        unavailable_tag = re.search(
            rf'<img src="{re.escape(web_url)}"[^>]*>', photo_unavailable.text
        ).group(0)
        assert 'loading="lazy"' in unavailable_tag
        assert 'fetchpriority="high"' not in unavailable_tag
        assert "width=" not in unavailable_tag
        assert "srcset=" not in unavailable_tag
        assert "sizes=" not in unavailable_tag
        assert '<link rel="preload" as="image"' not in photo_unavailable.text
    finally:
        cleanup()


def test_gallery_public_site_readiness(admin):
    ready_id = db.run(
        """INSERT INTO galleries
           (slug, title, pin, published, cs_published, cs_tagline, cs_location,
            cs_brief, cs_credits)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "readiness-ready",
            "Readiness Ready",
            "1234",
            1,
            1,
            "A mountain listing",
            "Asheville, NC",
            "Architecture and detail coverage.",
            "Agent: Alex",
        ),
    )
    db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, portfolio, portfolio_tag)
           VALUES (?,?,?,?,?,?,?)""",
        (ready_id, "photo", "hero.jpg", "readinesshero.jpg", "ready", 1, "re/exteriors"),
    )
    db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, portfolio, portfolio_tag)
           VALUES (?,?,?,?,?,?,?)""",
        (ready_id, "video", "tour.mp4", "readinesstour.mp4", "ready", 1, "re/walkthrough"),
    )

    r = admin.get(f"/admin/galleries/{ready_id}")
    assert r.status_code == 200
    assert "Public site readiness" in r.text
    assert "1 starred ready photo" in r.text
    assert "1 starred ready video" in r.text
    assert "Inferred specialty: Real Estate" in r.text
    assert "Ready:</b> Case-study fields are complete." in r.text
    assert f'href="{config.BASE_URL}/g/readiness-ready"' in r.text
    assert f'href="{config.BASE_URL}/work/readiness-ready"' in r.text
    assert 'href="/admin/share?path=/work/readiness-ready#selected-card"' in r.text
    assert "Published case study has no eligible hero" not in r.text
    assert "shoot your listing" in admin.get("/work/readiness-ready").text
    assert f"{config.BASE_URL}/work/readiness-ready" in admin.get("/admin/share").text

    warning_id = db.run(
        "INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
        ("readiness-warning", "Readiness Warning", "5678"),
    )
    r = admin.get(f"/admin/galleries/{warning_id}")
    assert r.status_code == 200
    assert "0 starred ready photos" in r.text
    assert "0 starred ready videos" in r.text
    assert (
        "Inferred specialty: Food &amp; Beverage (public default; no eligible starred assets)"
        in r.text
    )
    assert "Missing case-study fields: tagline, location, brief, credits." in r.text
    assert "Public gallery is unpublished; preview unavailable." in r.text
    assert "Case study is unpublished; preview unavailable." in r.text
    assert "Preview public gallery" not in r.text
    assert "Preview public case study" not in r.text

    db.run("UPDATE galleries SET cs_published=1 WHERE id=?", (warning_id,))
    r = admin.get(f"/admin/galleries/{warning_id}")
    assert "Published case study has no eligible hero/starred ready photo." in r.text
    assert "Preview public case study" in r.text
    assert "Open share debugger for this route" in r.text
    assert "Preview public gallery" not in r.text
    db.run("DELETE FROM galleries WHERE id IN (?,?)", (ready_id, warning_id))


def test_share_debugger(admin):
    import html
    import re

    from app import config
    from app.admin.share import _build_urls

    # baseline: marketing pages always listed; case-studies section only when
    # there's at least one published study
    r = admin.get("/admin/share")
    assert r.status_code == 200
    assert "Marketing pages" in r.text
    indexable_paths = (
        "/",
        "/real-estate",
        "/portraits",
        "/food-beverage",
        "/portfolio",
        "/work",
        "/services",
        "/about",
        "/contact",
        "/book",
        "/reels",
        "/press",
    )
    for path in indexable_paths:
        assert f"{config.BASE_URL}{path}" in r.text, path
    marketing_rows = {u["path"]: u for u in _build_urls() if u["kind"] == "marketing"}
    assert set(marketing_rows) == set(indexable_paths)
    with TestClient(app) as pub:
        for path, row in marketing_rows.items():
            page = pub.get(path)
            assert page.status_code == 200
            title = html.unescape(
                re.search(r"<title>(.*?)</title>", page.text, re.S).group(1)
            ).strip()
            description = html.unescape(
                re.search(r'<meta name="description" content="([^"]*)">', page.text).group(1)
            )
            canonical = re.search(r'<link rel="canonical" href="([^"]+)">', page.text).group(1)
            og_image = re.search(
                r'<meta property="og:image" content="[^"]+/site/img/(\d+)">', page.text
            )
            assert row["title"] == title
            assert row["meta_description"] == description
            assert row["full_url"] == canonical
            assert row["og_image_id"] == (int(og_image.group(1)) if og_image else None)
    # per-row debugger links (Facebook + LinkedIn + OpenGraph.xyz)
    assert "developers.facebook.com/tools/debug" in r.text
    assert "linkedin.com/post-inspector" in r.text
    assert "opengraph.xyz/url/" in r.text
    # URLs are url-encoded so colons + slashes survive the inspector links
    assert "https%3A" in r.text or "http%3A" in r.text

    # the 1:1 reskin dropped the old Galleries nav strip; Share is now reached
    # from any admin page via the ⌘K command palette (a JS-built CMDS entry).
    assert '"/admin/share"' in admin.get("/admin/galleries").text

    # Publish a self-contained case study and confirm its exact public metadata.
    gid = db.run(
        """INSERT INTO galleries
           (slug, title, pin, published, cs_published, cs_tagline, cs_brief, cs_location)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "share-debugger-study",
            "Share Debugger Study",
            "4422",
            1,
            1,
            "Spring dish series",
            "A two-day shoot covering the spring menu refresh.",
            "Asheville, NC",
        ),
    )
    db.run(
        """INSERT INTO assets
           (gallery_id, kind, filename, stored, status, portfolio, position)
           VALUES (?,?,?,?,?,?,?)""",
        (gid, "photo", "share-hero.jpg", "sharehero.jpg", "ready", 1, 0),
    )
    g = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    saved = db.one(
        "SELECT cs_published, cs_location, cs_tagline FROM galleries WHERE id=?", (g["id"],)
    )
    assert saved["cs_published"] == 1
    assert saved["cs_location"] == "Asheville, NC"
    assert saved["cs_tagline"] == "Spring dish series"
    r = admin.get(f"/admin/share?path=/work/{g['slug']}")
    assert "Case studies" in r.text
    assert f"/work/{g['slug']}" in r.text
    assert 'id="selected-card"' in r.text
    assert "Spring dish series" in r.text  # cs_tagline became og:title
    assert "spring menu refresh" in r.text  # cs_brief became description
    # Jinja escapes the comma differently? No — just look for Asheville
    assert "Asheville" in r.text
    # a hero photo thumb shows as the same og:image used by the public study
    assert re.search(r"/site/img/\d+\?variant=thumb", r.text)
    with TestClient(app) as pub:
        study = pub.get(f"/work/{g['slug']}")
    row = next(u for u in _build_urls() if u["path"] == f"/work/{g['slug']}")
    assert (
        row["title"]
        == html.unescape(re.search(r"<title>(.*?)</title>", study.text, re.S).group(1)).strip()
    )
    assert row["meta_description"] == html.unescape(
        re.search(r'<meta name="description" content="([^"]*)">', study.text).group(1)
    )
    study_og = re.search(r'<meta property="og:image" content="[^"]+/site/img/(\d+)">', study.text)
    assert row["og_image_id"] == int(study_og.group(1))

    # Unpublish → case study drops off the debugger.
    db.run("UPDATE galleries SET cs_published=0 WHERE id=?", (g["id"],))
    r = admin.get("/admin/share")
    assert "Spring dish series" not in r.text
    db.run("DELETE FROM galleries WHERE id=?", (g["id"],))


def test_section_captions(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    # Seed a fresh section + asset so this test doesn't depend on prior
    # tests' shuffling. Public gallery skips sections with no ready assets.
    sec_id = db.run(
        "INSERT INTO sections (gallery_id, name, position) VALUES (?,?,?)",
        (g["id"], "Captioned Chapter", 99),
    )
    db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, "
        "stored, status) VALUES (?,?,?,?,?,?)",
        (g["id"], sec_id, "photo", "cap.jpg", "cafe1234deadbeef.jpg", "ready"),
    )
    sec = {"id": sec_id, "name": "Captioned Chapter"}

    with TestClient(app) as pub:
        # baseline: no caption → no <p class="section-caption"> rendered
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert "section-caption" not in r.text

    # admin sets a caption
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
        data={"caption": "Hero dishes from the spring menu."},
        follow_redirects=False,
    )
    assert (
        db.one("SELECT caption FROM sections WHERE id=?", (sec["id"],))["caption"]
        == "Hero dishes from the spring menu."
    )

    # admin gallery page shows the caption pre-filled in the form
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert 'name="caption"' in r.text
    assert 'value="Hero dishes from the spring menu."' in r.text

    # public gallery renders the caption under the section heading
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert 'class="section-caption"' in r.text
        assert "Hero dishes from the spring menu." in r.text
        # caption sits between the section h2 and the grid div
        heading_at = r.text.index(f'id="sec-{sec["id"]}"')
        grid_at = r.text.index("Hero dishes from the spring menu.")
        assert heading_at < grid_at

    # clearing caption (empty string) → NULL → not rendered
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
        data={"caption": ""},
        follow_redirects=False,
    )
    assert db.one("SELECT caption FROM sections WHERE id=?", (sec["id"],))["caption"] is None
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        r = pub.get(f"/g/{g['slug']}")
        assert "section-caption" not in r.text
        assert "Hero dishes from the spring menu." not in r.text

    # whitespace-only collapses to NULL too
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{sec['id']}/caption",
        data={"caption": "   "},
        follow_redirects=False,
    )
    assert db.one("SELECT caption FROM sections WHERE id=?", (sec["id"],))["caption"] is None


def test_portfolio_tag_filter(admin):
    g = db.one("SELECT * FROM galleries ORDER BY id LIMIT 1")
    # plant 3 fresh portfolio-eligible photos in this gallery
    ids = []
    for i in range(3):
        aid = db.run(
            "INSERT INTO assets (gallery_id, kind, filename, stored, "
            "status, portfolio) VALUES (?,?,?,?,?,?)",
            (g["id"], "photo", f"p{i}.jpg", f"feedface0{i}feedface.jpg", "ready", 1),
        )
        ids.append(aid)

    with TestClient(app) as pub:
        # baseline: no tags → no filter chip nav; tiles render flat
        r = pub.get("/portfolio")
        assert r.status_code == 200
        assert "portfolio-filter" not in r.text
        assert "pf-chip" not in r.text
        # untagged tiles don't carry data-tag
        for aid in ids:
            assert f'data-web="/site/img/{aid}?variant=web"' in r.text
            assert (
                "data-tag=" not in r.text
                or f'data-tag="" data-web="/site/img/{aid}?variant=web"' not in r.text
            )

    # admin sets tags via the tag endpoint
    admin.post(
        f"/admin/galleries/{g['id']}/assets/{ids[0]}/tag",
        data={"portfolio_tag": "Dishes"},
        follow_redirects=False,
    )
    admin.post(
        f"/admin/galleries/{g['id']}/assets/{ids[1]}/tag",
        data={"portfolio_tag": "Dishes"},
        follow_redirects=False,
    )
    admin.post(
        f"/admin/galleries/{g['id']}/assets/{ids[2]}/tag",
        data={"portfolio_tag": "Drinks"},
        follow_redirects=False,
    )
    # db round-trip
    assert (
        db.one("SELECT portfolio_tag FROM assets WHERE id=?", (ids[0],))["portfolio_tag"]
        == "Dishes"
    )

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
        assert "data-pf" in r.text
        assert "/static/portfolio-filter.js?v=" in r.text
        assert "data-pf-empty" in r.text
        assert 'data-filter=""' in r.text and ">All" in r.text  # 'All' chip
        # alphabetical: Dishes (2) before Drinks (1); tag filters are namespaced
        assert r.text.index('data-filter="tag:dishes"') < r.text.index('data-filter="tag:drinks"')
        # per-tag counts visible
        assert ">Dishes" in r.text and "(2)" in r.text
        assert ">Drinks" in r.text and "(1)" in r.text
        # tiles carry lowercased tag attrs + the derived specialty bucket
        assert 'data-tag="dishes"' in r.text
        assert 'data-tag="drinks"' in r.text
        assert 'data-sp="fb"' in r.text  # legacy unprefixed tags = F&B
        # filter chip data-filter is lowercased to match
        assert 'data-filter="tag:dishes"' in r.text
        assert 'data-filter="tag:drinks"' in r.text

    # clearing a tag (empty string) → DB stores NULL, chip count drops
    admin.post(
        f"/admin/galleries/{g['id']}/assets/{ids[1]}/tag",
        data={"portfolio_tag": ""},
        follow_redirects=False,
    )
    assert db.one("SELECT portfolio_tag FROM assets WHERE id=?", (ids[1],))["portfolio_tag"] is None
    with TestClient(app) as pub:
        r = pub.get("/portfolio")
        assert ">Dishes" in r.text and "(1)" in r.text  # was 2, now 1
        assert ">Drinks" in r.text and "(1)" in r.text

    # unstarring a photo removes it from the public count (and the grid)
    admin.post(f"/admin/galleries/{g['id']}/assets/{ids[0]}/portfolio", follow_redirects=False)
    with TestClient(app) as pub:
        r = pub.get("/portfolio")
        # Dishes tag now has nothing → chip gone (we only show tags actually in use)
        assert 'data-filter="tag:dishes"' not in r.text
        assert 'data-filter="tag:drinks"' in r.text  # Drinks still has the lone tagged photo


def test_proofing_mode(admin):
    # own gallery so the proofing section starts clean — uploads elsewhere now
    # default into the first section, which would pollute a shared gallery's counts
    admin.post(
        "/admin/galleries", data={"title": "Proofing", "client_name": ""}, follow_redirects=False
    )
    g = db.one("SELECT * FROM galleries ORDER BY id DESC LIMIT 1")
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={"title": "Proofing", "pin": "7777", "published": "true"},
    )
    g = db.one("SELECT * FROM galleries WHERE id=?", (g["id"],))
    sec = db.one("SELECT id FROM sections WHERE gallery_id=? ORDER BY position LIMIT 1", (g["id"],))
    # park 3 assets in this section
    for i in range(3):
        db.run(
            "INSERT INTO assets (gallery_id, section_id, kind, filename, "
            "stored, status) VALUES (?,?,?,?,?,?)",
            (g["id"], sec["id"], "photo", f"d{i}.jpg", f"deadbeef0{i}deadbeef.jpg", "ready"),
        )
    assets = db.all_(
        "SELECT id FROM assets WHERE gallery_id=? AND section_id=? ORDER BY id",
        (g["id"], sec["id"]),
    )

    # admin sets proof_target=2
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
        data={"proof_target": "2"},
        follow_redirects=False,
    )
    assert db.one("SELECT proof_target FROM sections WHERE id=?", (sec["id"],))["proof_target"] == 2
    # admin gallery page reflects target + the live picks count badge
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert 'name="proof_target"' in r.text and 'value="2"' in r.text
    assert "0 / 2 picked" in r.text

    # public visitor unlocks the gallery and starts picking
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]}, follow_redirects=False)
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
        assert (
            db.one(
                "SELECT COUNT(*) AS n FROM favorites f "
                "JOIN assets a ON a.id=f.asset_id "
                "WHERE a.id=?",
                (assets[2]["id"],),
            )["n"]
            == 0
        )

        # unfav one → progress drops to 1 of 2; can now pick the 3rd
        r = pub.post(f"/g/{g['slug']}/fav/{assets[0]['id']}")
        assert r.status_code == 200 and "1 of 2 picked" in r.text
        r = pub.post(f"/g/{g['slug']}/fav/{assets[2]['id']}")
        assert r.status_code == 200 and "2 of 2 picked" in r.text

    # admin badge flips to "ready" once the target is hit
    r = admin.get(f"/admin/galleries/{g['id']}")
    assert "2 / 2 ready" in r.text

    # clearing the target unblocks unlimited faves and removes the label
    admin.post(
        f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
        data={"proof_target": ""},
        follow_redirects=False,
    )
    assert (
        db.one("SELECT proof_target FROM sections WHERE id=?", (sec["id"],))["proof_target"] is None
    )
    with TestClient(app) as pub:
        pub.post(f"/g/{g['slug']}/pin", data={"pin": g["pin"]}, follow_redirects=False)
        # now we can fav the leftover asset[0] without trouble — target is gone
        r = pub.post(f"/g/{g['slug']}/fav/{assets[0]['id']}")
        assert r.status_code == 200
        # the response has no OOB progress fragment (no proof_target)
        assert 'id="proof-' not in r.text
        # public gallery page also drops the badge
        r = pub.get(f"/g/{g['slug']}")
        assert 'id="proof-' + str(sec["id"]) + '"' not in r.text

    # bad input: non-numeric target → 400
    assert (
        admin.post(
            f"/admin/galleries/{g['id']}/sections/{sec['id']}/proof",
            data={"proof_target": "twelve"},
            follow_redirects=False,
        ).status_code
        == 400
    )


def test_today_consolidated_view(admin):
    # baseline: page renders with friendly empty-states even when nothing
    # has happened in the last 24h
    page = admin.get("/admin/today").text
    assert "Today" in page and "last 24h" in page
    # nav cross-link from dashboard
    assert 'href="/admin/today"' in admin.get("/admin").text

    # seed one of each kind of activity (relative to "now" via SQLite default)
    iid = db.run(
        "INSERT INTO inquiries (name, email, business, message, kind, "
        "service, shoot_date) VALUES (?,?,?,?,?,?,?)",
        (
            "Today Tester",
            "today@cafe.com",
            "Bistro Today",
            "test booking",
            "booking",
            "Photography",
            "2026-06-20",
        ),
    )
    g = db.one("SELECT id FROM galleries ORDER BY id LIMIT 1")
    db.run(
        "INSERT INTO downloads (gallery_id, asset_id) VALUES (?,?)", (g["id"], None)
    )  # full-zip download (asset NULL)
    # a single visitor + fav seeded for the favorites-by-gallery roll-up
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (g["id"], "tok-today"))
    a = db.one("SELECT id FROM assets WHERE gallery_id=? LIMIT 1", (g["id"],))
    db.run("INSERT OR IGNORE INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, a["id"]))
    db.run(
        "INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, "
        "subject) VALUES (NULL, 'other', NULL, ?, ?)",
        ("today-recipient@cafe.com", "Hi from /admin/today test"),
    )

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
    fav_section = page[page.index("Favorites (by gallery)") :]
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
    cid = db.run(
        "INSERT INTO clients (name, company, email) VALUES (?,?,?)",
        ("Sent-Log Co", "Bistro Lune", "sl@cafe.com"),
    )
    pid = db.run("INSERT INTO projects (client_id, title) VALUES (?,?)", (cid, "Spring shoot"))
    for kind, subj in [
        ("proposal", "Your proposal — Spring shoot"),
        ("contract", "Sign here — Spring shoot"),
        ("invoice", "Invoice #001 — Spring shoot"),
        ("other", "Your photos are ready — Spring shoot"),
    ]:
        db.run(
            "INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, subject) "
            "VALUES (?,?,?,?,?)",
            (pid, kind, 1, "sl@cafe.com", subj),
        )

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
        db.run(
            "INSERT INTO emails_log (project_id, doc_kind, doc_id, to_email, "
            "subject) VALUES (?,?,?,?,?)",
            (pid, "other", i, "sl@cafe.com", f"Page subject {i}"),
        )
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
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, reply_to="", ics=None: None)
    # Wipe any prior pin_attempts so this test is isolated
    db.run(
        "DELETE FROM pin_attempts WHERE gallery_id IN (?,?)",
        (security.INQUIRY_BUCKET_CONTACT, security.INQUIRY_BUCKET_BOOK),
    )

    import datetime as dt

    contact_data = lambda i: {
        "name": f"User{i}",
        "email": f"u{i}@cafe.com",
        "business": "Cafe",
        "message": "hello",
    }

    with TestClient(app) as pub:
        # /contact: 3 succeed, 4th is throttled with 429
        for i in range(3):
            r = pub.post("/contact", data=contact_data(i))
            assert r.status_code == 200 and "Thanks" in r.text, i
        r = pub.post("/contact", data=contact_data(99))
        assert r.status_code == 429
        assert "chance to reply" in r.text
        # the throttled submit must not store another inquiry row
        assert (
            db.one("SELECT COUNT(*) AS n FROM inquiries WHERE email=?", ("u99@cafe.com",))["n"] == 0
        )

        # /book (the scheduler) has its OWN throttle bucket — bookings still go
        # through even though /contact was throttled. Seed a bookable event +
        # a day of open slots (idempotent: the suite shares one module DB).
        from app import scheduling as S

        if not S.event_by_slug("rl-book"):
            _eid = db.run(
                "INSERT INTO event_types (slug, name, duration_min, "
                "min_notice_hours, booking_window_days, active) "
                "VALUES ('rl-book','Rate Test',60,1,60,1)"
            )
            for _wd in range(5):
                db.run(
                    "INSERT INTO availability_rules (event_type_id, weekday, "
                    "start_min, end_min) VALUES (?,?,?,?)",
                    (_eid, _wd, 540, 1020),
                )
        _et = S.event_by_slug("rl-book")
        _day = dt.date.today() + dt.timedelta(days=3)
        while _day.weekday() >= 5:
            _day += dt.timedelta(days=1)
        _slots = S.slots_for_day(_et, _day)
        assert len(_slots) >= 4, "need >=4 open slots to prove the booking throttle"
        # first 3 bookings (distinct slots) succeed; the 4th trips the BOOK bucket
        for i in range(3):
            r = pub.post(
                "/book/rl-book",
                data={
                    "name": f"User{i}",
                    "email": f"u{i}@cafe.com",
                    "start": _slots[i]["utc"],
                    "tz": "America/New_York",
                },
                follow_redirects=False,
            )
            assert r.status_code == 303, (i, r.status_code)
        r = pub.post(
            "/book/rl-book",
            data={
                "name": "User99",
                "email": "u99@cafe.com",
                "start": _slots[3]["utc"],
                "tz": "America/New_York",
            },
        )
        assert r.status_code == 429
        assert "booked a few times" in r.text

        # honeypot still wins silently — doesn't decrement counter, doesn't 429
        # (we wipe and start fresh to confirm honeypot bypasses the throttle path)
        db.run(
            "DELETE FROM pin_attempts WHERE gallery_id IN (?,?)",
            (security.INQUIRY_BUCKET_CONTACT, security.INQUIRY_BUCKET_BOOK),
        )
        for _ in range(5):
            r = pub.post("/contact", data={**contact_data(1), "website": "bot.com"})
            assert r.status_code == 200 and "Thanks" in r.text

        # Validation failure (bad email) does NOT consume a token — only
        # successful sends record the attempt. Otherwise a single typo could
        # lock you out before any inquiry lands.
        db.run("DELETE FROM pin_attempts WHERE gallery_id=?", (security.INQUIRY_BUCKET_CONTACT,))
        for _ in range(10):
            r = pub.post("/contact", data={"name": "Bad", "email": "not-email", "message": "x"})
            assert r.status_code == 400
        # Now 3 legit succeed — the typos never burned the budget
        for i in range(3):
            r = pub.post("/contact", data=contact_data(100 + i))
            assert r.status_code == 200, i

    # Tear down the rl-book bookings + auto-linked clients/inquiries so the
    # leftover confirmed slots don't collide with downstream studio/conflict
    # tests that share this module DB. FK order: drop the bookings (which point
    # at clients + inquiries) before the rows they reference.
    _cids = [
        r["client_id"]
        for r in db.all_(
            "SELECT DISTINCT client_id FROM bookings WHERE event_type_id=? "
            "AND client_id IS NOT NULL",
            (_et["id"],),
        )
    ]
    _iids = [
        r["inquiry_id"]
        for r in db.all_(
            "SELECT inquiry_id FROM bookings WHERE event_type_id=? AND inquiry_id IS NOT NULL",
            (_et["id"],),
        )
    ]
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
    assert 'favBtn.addEventListener("click", triggerFav)' in js
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
    row = r.text[row_start : r.text.index("</tr>", row_start)]
    assert "no portal" in row

    # add an unpublished portal (visits=0) → "never visited"
    db.run(
        "INSERT INTO portals (client_id, slug, pin) VALUES (?,?,?)",
        (cid, "portal-hint-aaaa", "1234"),
    )
    row = (
        lambda t: t[t.index("Portal Hint Cafe") : t.index("</tr>", t.index("Portal Hint Cafe"))]
    )(admin.get("/admin/studio/clients").text)
    assert "never visited" in row

    # set last_visit to 2 hours ago → "👁 2h ago"
    two_hours = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=2)
    ).isoformat()
    db.run("UPDATE portals SET visits=5, last_visit=? WHERE client_id=?", (two_hours, cid))
    row = (
        lambda t: t[t.index("Portal Hint Cafe") : t.index("</tr>", t.index("Portal Hint Cafe"))]
    )(admin.get("/admin/studio/clients").text)
    assert "👁" in row and "2h ago" in row

    # 5 minutes ago → "Xm ago"
    five_min = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=5)
    ).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (five_min, cid))
    row = (
        lambda t: t[t.index("Portal Hint Cafe") : t.index("</tr>", t.index("Portal Hint Cafe"))]
    )(admin.get("/admin/studio/clients").text)
    assert "5m ago" in row or "4m ago" in row  # tolerant to second-edge

    # 3 days ago → "3d ago"
    three_days = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=3)
    ).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (three_days, cid))
    row = (
        lambda t: t[t.index("Portal Hint Cafe") : t.index("</tr>", t.index("Portal Hint Cafe"))]
    )(admin.get("/admin/studio/clients").text)
    assert "3d ago" in row

    # 45 days ago → falls back to ISO date
    long_ago = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45)
    ).isoformat()
    db.run("UPDATE portals SET last_visit=? WHERE client_id=?", (long_ago, cid))
    row = (
        lambda t: t[t.index("Portal Hint Cafe") : t.index("</tr>", t.index("Portal Hint Cafe"))]
    )(admin.get("/admin/studio/clients").text)
    # ISO date (YYYY-MM-DD) appears in the hint
    iso = (
        (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=45))
        .date()
        .isoformat()
    )
    assert iso in row


def test_dashboard_proofing_status(admin):
    # The strict-1:1 grid card carries a single derived status badge. A published
    # gallery whose targeted proofing sections are still short of their pick count
    # reads "Proofing"; once every targeted section hits target it flips to
    # "Delivered". This replaces the old "✓ selects in" badge — same signal,
    # rendered in the prototype's status-pill shape.
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("SelectsBadge001", "Loose pickin", "1234"),
    )

    def card():
        # anchor on the grid card (last href — the orphan picker may list it
        # earlier) and read to the card's closing </a>
        page = admin.get("/admin/galleries").text
        start = page.rindex(f"/admin/galleries/{gid}")
        return page[start : page.index("</a>", start)]

    # no proofing sections at all → nothing pending → Delivered
    assert ">Delivered<" in card()

    # add a targeted section with assets but no picks → Proofing
    sid = db.run(
        "INSERT INTO sections (gallery_id, name, position, proof_target) VALUES (?,?,?,?)",
        (gid, "Hero", 0, 2),
    )
    aids = [
        db.run(
            "INSERT INTO assets (gallery_id, section_id, kind, filename, "
            "stored, status) VALUES (?,?,?,?,?,?)",
            (gid, sid, "photo", f"p{i}.jpg", f"selbadge0{i}.jpg", "ready"),
        )
        for i in range(3)
    ]
    assert ">Proofing<" in card()

    # one fav of two needed → still short → Proofing
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, "vtok-badge"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aids[0]))
    assert ">Proofing<" in card()

    # hit the target → proofing complete → Delivered
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aids[1]))
    assert ">Delivered<" in card()

    # a SECOND targeted section that's still empty → partially done → Proofing
    sid2 = db.run(
        "INSERT INTO sections (gallery_id, name, position, proof_target) VALUES (?,?,?,?)",
        (gid, "Drinks", 1, 2),
    )
    db.run(
        "INSERT INTO assets (gallery_id, section_id, kind, filename, stored, "
        "status) VALUES (?,?,?,?,?,?)",
        (gid, sid2, "photo", "d.jpg", "selbadge99.jpg", "ready"),
    )
    assert ">Proofing<" in card()

    # zero-target sections don't count (proof_target=0 is the "off" sentinel) →
    # only the first (complete) section remains → Delivered
    db.run("UPDATE sections SET proof_target=0 WHERE id=?", (sid2,))
    assert ">Delivered<" in card()


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
    assert "spark-window-active" in page30[thirty_idx : page30.index("</a>", thirty_idx)]

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
    db.run(
        "INSERT INTO inquiries (name, email, message) VALUES (?,?,?)",
        ("Spark Tester", "spark@cafe.com", "test message"),
    )
    page = admin.get("/admin/studio/activity").text
    import re

    m = re.search(r"<strong>(\d+)</strong> Inquiries", page)
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
    db.run(
        "INSERT INTO inquiries (name, email, message, created_at) VALUES (?,?,?,?)",
        ("Evening Boundary", "evening@cafe.com", "late local", created_utc),
    )
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

    admin.post(
        "/admin/studio/clients",
        data={"name": "Overdue Che", "company": "Wall Clock Bistro"},
        follow_redirects=False,
    )
    c = db.one("SELECT * FROM clients ORDER BY id DESC LIMIT 1")
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (c["id"], "Overdue Boundary Project", "contract_signed"),
    )
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents,
                                          due_date, status)
                    VALUES (?,?,?,?,?,?)""",
        (pid, "ovd-boundary-12345", "Boundary Invoice", 50000, anchor.isoformat(), "sent"),
    )

    def row(page):
        # Projects render as board <article> cards; scope the overdue check to
        # this project's card so a stray "overdue" elsewhere can't fool it. The
        # card surfaces an overdue invoice via its step pill ("N overdue").
        i = page.index("Overdue Boundary Project")
        return page[page.rindex("<article", 0, i) : page.index("</article>", i)]

    # due ON the wall-clock anchor -> still due today -> NOT overdue
    # ("1 overdue" is the step-pill text; bare "overdue" also lives in data-search)
    assert "1 overdue" not in row(admin.get("/admin/studio").text)

    # due the day BEFORE the anchor -> genuinely past -> overdue
    db.run(
        "UPDATE invoices SET due_date=? WHERE id=?",
        ((anchor - _dt.timedelta(days=1)).isoformat(), iid),
    )
    assert "1 overdue" in row(admin.get("/admin/studio").text)


def test_studio_proofing_waiting(admin):
    # baseline: nothing in the proofing-waiting strip → section hidden
    r = admin.get("/admin/studio/activity")
    assert "Proofing waiting" not in r.text

    # set up: client → project → published gallery linked to project → proofing
    # section with 3 ready assets but only 1 fav (target 3, picks 1 → 2 remaining)
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Mara Sun",))
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Spring shoot — proofing", "session_planning"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, project_id, published) VALUES (?,?,?,?,1)",
        ("ProofWaiting001", "Spring shoot", "1234", pid),
    )
    sid = db.run(
        "INSERT INTO sections (gallery_id, name, position, proof_target) VALUES (?,?,?,?)",
        (gid, "Hero Dishes", 0, 3),
    )
    asset_ids = []
    for i in range(3):
        asset_ids.append(
            db.run(
                "INSERT INTO assets (gallery_id, section_id, kind, filename, "
                "stored, status) VALUES (?,?,?,?,?,?)",
                (gid, sid, "photo", f"d{i}.jpg", f"deadbeef0{i}deadbeef.jpg", "ready"),
            )
        )
    # one visitor faved one photo → 1 of 3 picked, 2 remaining
    vid = db.run(
        "INSERT INTO visitors (gallery_id, token, email) VALUES (?,?,?)",
        (gid, "vtoken-proof-1", "mara@cafe.com"),
    )
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, asset_ids[0]))

    # the all-projects table below renders every project too — slice the
    # proofing-waiting strip specifically so assertions only watch the chip
    def waiting_strip(text):
        if 'aria-label="Proofing waiting"' not in text:
            return ""
        start = text.index('aria-label="Proofing waiting"')
        return text[start : text.index("</section>", start)]

    r = admin.get("/admin/studio/activity")
    assert "Proofing waiting" in r.text
    strip = waiting_strip(r.text)
    assert "Spring shoot — proofing" in strip
    assert "1 chapter" in strip and "2 picks remaining" in strip
    # chip links to the gallery admin (where the Proofing prompt email lives)
    assert f'href="/admin/galleries/{gid}"' in strip

    # client picks the remaining two → section satisfied → project drops off
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, asset_ids[1]))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, asset_ids[2]))
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

    cid = db.run(
        "INSERT INTO clients (name, company, email) VALUES (?,?,?)",
        ("Mara Sun", "Café Lune", "mara@cafe.com"),
    )
    # 4 projects spanning the upcoming window + a control out of range
    plans = [
        ("Today launch", today.isoformat(), "inquiry_received", "today", True),
        (
            "Tomorrow shoot",
            (today + dt.timedelta(days=1)).isoformat(),
            "proposal_sent",
            "tomorrow",
            True,
        ),
        (
            "Next week shoot",
            (today + dt.timedelta(days=8)).isoformat(),
            "contract_signed",
            "in 8d",
            True,
        ),
        (
            "Overdue not shooting",
            (today - dt.timedelta(days=3)).isoformat(),
            "proposal_sent",
            "3d ago",
            True,
        ),
        (
            "Way out — skip",
            (today + dt.timedelta(days=30)).isoformat(),
            "inquiry_received",
            "in 30d",
            False,
        ),
        (
            "Long past — skip",
            (today - dt.timedelta(days=30)).isoformat(),
            "session_planning",
            "30d ago",
            False,
        ),
        ("No shoot date — skip", None, "inquiry_received", "—", False),
    ]
    for title, sdate, status, _label, _in_strip in plans:
        db.run(
            "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
            (cid, title, status, sdate),
        )

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
    strip = r.text[
        r.text.index('class="upcoming-strip"', sec_start) : r.text.index(
            "</ul>", r.text.index('class="upcoming-strip"', sec_start)
        )
    ]
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
    pid_a = db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Salt Bar shoot", "contract_signed", d_collide),
    )
    pid_b = db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Curate breakfast", "inquiry_received", d_collide),
    )
    # solo upcoming → no collision
    db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Solo gig", "inquiry_received", d_solo),
    )
    # archived on a collision date → ignored
    db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Archived ghost", "archived", d_collide),
    )
    # far out → outside window, ignored
    db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Far future", "inquiry_received", d_far),
    )

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
    db.run(
        "INSERT INTO projects (client_id, title, status, shoot_date) VALUES (?,?,?,?)",
        (cid, "Tasting menu", "proposal_sent", d_inq),
    )
    db.run(
        "INSERT INTO inquiries (name, email, message, kind, shoot_date, service) "
        "VALUES (?,?,?,?,?,?)",
        ("Drop-in chef", "chef@x.com", "Need photos", "booking", d_inq, "Photography"),
    )
    # converted inquiry on the SAME date → ignored (already accounted for as a project elsewhere)
    db.run(
        "INSERT INTO inquiries (name, email, message, kind, shoot_date, service, "
        "converted_at) VALUES (?,?,?,?,?,?, datetime('now'))",
        ("Already booked", "ab@x.com", "", "booking", d_inq, "Videography"),
    )

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
    pid = db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Spring menu", "project_closed"),
    )
    db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Old gig", "archived"),
    )
    db.run(
        "INSERT INTO projects (client_id, title, status) VALUES (?,?,?)",
        (cid, "Pitch", "inquiry_received"),
    )

    # a draft invoice (excluded from invoiced) + a sent invoice (counted)
    db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status)
              VALUES (?,?,?,?,?)""",
        (pid, "rollup-draft", "Draft", 99999, "draft"),
    )
    iid = db.run(
        """INSERT INTO invoices (project_id, slug, title, total_cents, status)
                    VALUES (?,?,?,?,?)""",
        (pid, "rollup-sent", "Issued", 100000, "sent"),
    )
    # a partial deposit payment — paid is the ground truth, leaving an outstanding balance
    db.run(
        """INSERT INTO payments (invoice_id, amount_cents, kind) VALUES (?,?,?)""",
        (iid, 40000, "deposit"),
    )

    r = admin.get(f"/admin/studio/clients/{cid}")
    s_start = r.text.index('class="pgtop-sub"')
    sec = r.text[s_start : r.text.index("</p>", s_start)]
    assert "$400.00</b> paid lifetime" in sec
    assert "$1000.00 invoiced" in sec  # draft's $999.99 excluded
    assert "across 1 invoice" in sec  # draft not counted
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
        admin.post(
            "/admin/studio/testimonials",
            data={
                "quote": "They captured our menu better than we imagined.",
                "attribution_name": "Mara Sun",
                "business": "Owner, Café Lune",
                "gallery_id": "",
                "position": "0",
                "published": "true",
            },
            follow_redirects=False,
        )
        admin.post(
            "/admin/studio/testimonials",
            data={
                "quote": "Spring shoot felt effortless.",
                "attribution_name": "Lou Mendez",
                "business": "Bistro Vert",
                "gallery_id": str(g["id"]),
                "position": "0",
                "published": "true",
            },
            follow_redirects=False,
        )
        admin.post(
            "/admin/studio/testimonials",
            data={
                "quote": "Draft only — should not show.",
                "attribution_name": "Sam Draft",
                "gallery_id": "",
                "position": "0",
            },  # no published flag
            follow_redirects=False,
        )

        # home + services now show the general one (and only the general one)
        for path in ("/", "/services"):
            r = pub.get(path)
            assert "captured our menu better" in r.text, path
            assert "Mara Sun" in r.text and "Café Lune" in r.text
            assert "Spring shoot felt effortless" not in r.text, path
            assert "Draft only" not in r.text, path
            # Testimonials stay human-readable without self-serving Review JSON-LD.
            assert '"@type": "Review"' not in r.text

        # A general F&B quote must not be used as proof for unrelated specialties.
        for path in ("/real-estate", "/portraits"):
            r = pub.get(path)
            assert "captured our menu better" not in r.text, path

    # publish the case study + verify the gallery-scoped testimonial shows there
    admin.post(
        f"/admin/galleries/{g['id']}/assets/"
        f"{db.one('SELECT id FROM assets WHERE gallery_id=? AND kind=' + chr(34) + 'photo' + chr(34), (g['id'],))['id']}"
        "/portfolio",
        follow_redirects=False,
    )
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={
            "title": g["title"],
            "client_name": g["client_name"] or "",
            "pin": g["pin"],
            "expires_at": "",
            "published": "true",
            "captions": "",
            "cs_published": "true",
            "cs_tagline": "Test case study",
            "cs_brief": "Brief.",
            "cs_credits": "",
            "cs_location": "",
        },
    )
    with TestClient(app) as pub:
        r = pub.get(f"/work/{g['slug']}")
        assert "Spring shoot felt effortless" in r.text
        assert "Lou Mendez" in r.text
        # The general testimonial does NOT appear on the case-study page
        assert "captured our menu better" not in r.text
        # the gallery-scoped testimonial renders as the editorial pull-quote,
        # full attribution (name · business) — proves the right one is scoped here
        assert "Bistro Vert" in r.text

    # admin list + update + delete flow
    r = admin.get("/admin/studio/testimonials")
    assert r.status_code == 200
    assert "Mara Sun" in r.text and "Lou Mendez" in r.text and "Sam Draft" in r.text
    sam = db.one("SELECT id FROM testimonials WHERE attribution_name='Sam Draft'")
    admin.post(
        f"/admin/studio/testimonials/{sam['id']}",
        data={
            "quote": "Now published.",
            "attribution_name": "Sam Drafted",
            "business": "",
            "gallery_id": "",
            "position": "0",
            "published": "true",
        },
        follow_redirects=False,
    )
    row = db.one("SELECT * FROM testimonials WHERE id=?", (sam["id"],))
    assert row["published"] == 1 and row["attribution_name"] == "Sam Drafted"
    admin.post(f"/admin/studio/testimonials/{sam['id']}/delete", follow_redirects=False)
    assert db.one("SELECT id FROM testimonials WHERE id=?", (sam["id"],)) is None

    # deleting a gallery unbinds testimonials (FK ON DELETE SET NULL)
    # — first unpublish the gallery so delete_gallery accepts it
    admin.post(
        f"/admin/galleries/{g['id']}/settings",
        data={
            "title": g["title"],
            "client_name": g["client_name"] or "",
            "pin": g["pin"],
            "expires_at": "",
            "published": "",
            "captions": "",
            "cs_published": "",
            "cs_tagline": "",
            "cs_brief": "",
            "cs_credits": "",
            "cs_location": "",
        },
    )
    admin.post(f"/admin/galleries/{g['id']}/delete", follow_redirects=False)
    lou = db.one("SELECT gallery_id FROM testimonials WHERE attribution_name='Lou Mendez'")
    assert lou is not None and lou["gallery_id"] is None
