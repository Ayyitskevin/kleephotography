"""The per-file save path (G2) — one original, gated, logged, conditional.

Mobile is the point: iOS Safari handles a large ZIP badly, and an
``application/octet-stream`` attachment can only ever land in the Files app.
The lightbox Save button fetches this route and hands the original to the OS
share sheet, so the real image type and the Content-Disposition grammar are
load-bearing — as is the rule that the route reuses EXACTLY the same gates
(PIN session, email capture, expiry) and per-visitor download log as every
sibling download route. No new path around the gate.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

JPEG = b"\xff\xd8\xff" + b"x" * 4000
JPEG_B = (
    b"\xff\xd8\xff" + b"y" * 4000
)  # distinct bytes: a wrong-sibling-path bug must fail the content assert


@pytest.fixture
def gallery():
    slug = f"save{time.time_ns()}"
    db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)", (slug, "Save", "1234")
    )
    g = db.one("SELECT * FROM galleries WHERE slug=?", (slug,))
    orig = config.MEDIA_DIR / str(g["id"]) / "original"
    orig.mkdir(parents=True, exist_ok=True)
    # Two originals: an ASCII filename and a non-ASCII one (the RFC 5987 axis).
    # `stored` stays ASCII on disk either way — disposition reads `filename`.
    (orig / "a.jpg").write_bytes(JPEG)
    (orig / "b.jpg").write_bytes(JPEG_B)
    for filename, stored in (("dish-one.jpg", "a.jpg"), ("café menü.jpg", "b.jpg")):
        db.run(
            "INSERT INTO assets (gallery_id,kind,filename,stored,bytes,status) "
            "VALUES (?,?,?,?,?,'ready')",
            (g["id"], "photo", filename, stored, len(JPEG)),
        )
    ids = [
        r["id"] for r in db.all_("SELECT id FROM assets WHERE gallery_id=? ORDER BY id", (g["id"],))
    ]
    yield g, ids
    db.run("DELETE FROM assets WHERE gallery_id=?", (g["id"],))
    db.run("DELETE FROM galleries WHERE id=?", (g["id"],))


def _unlock(c, g, email=True):
    c.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
    if email:
        c.post(
            f"/g/{g['slug']}/email",
            data={"email": "chef@bistro.com"},
            follow_redirects=False,
        )


def _rows(g):
    return db.all_("SELECT * FROM downloads WHERE gallery_id=?", (g["id"],))


@pytest.mark.integration
def test_no_gallery_session_means_no_file_and_no_log(gallery):
    g, (aid, _) = gallery
    with TestClient(app) as anon:
        r = anon.get(f"/g/{g['slug']}/download/asset/{aid}")
        assert r.status_code == 403
    assert _rows(g) == []


@pytest.mark.integration
def test_email_gate_still_intercepts_the_single_file(gallery):
    g, (aid, _) = gallery
    with TestClient(app) as c:
        _unlock(c, g, email=False)
        r = c.get(f"/g/{g['slug']}/download/asset/{aid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/g/{g['slug']}/download?asset_id={aid}"
    assert _rows(g) == []


@pytest.mark.integration
def test_expired_gallery_refuses_even_an_admitted_visitor(gallery):
    g, (aid, _) = gallery
    with TestClient(app) as c:
        _unlock(c, g)
        db.run("UPDATE galleries SET expires_at='2000-01-01' WHERE id=?", (g["id"],))
        assert c.get(f"/g/{g['slug']}/download/asset/{aid}").status_code == 410


@pytest.mark.integration
def test_serves_the_original_with_real_type_and_logs_the_visitor(gallery):
    g, (aid, _) = gallery
    with TestClient(app) as c:
        _unlock(c, g)
        r = c.get(f"/g/{g['slug']}/download/asset/{aid}")
        assert r.status_code == 200
        assert r.content == JPEG  # the master file, byte-for-byte
        # The share-sheet save path needs the real type — octet-stream made
        # the shared blob a nameless "file" iOS could not put in Photos.
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["content-disposition"] == 'attachment; filename="dish-one.jpg"'
        assert r.headers.get("etag")
        assert r.headers.get("cache-control") == "private, max-age=86400"
    rows = _rows(g)
    assert len(rows) == 1
    assert rows[0]["asset_id"] == aid and rows[0]["visitor_id"] is not None


@pytest.mark.integration
def test_non_ascii_filename_uses_rfc5987(gallery):
    g, (_, bid) = gallery
    with TestClient(app) as c:
        _unlock(c, g)
        r = c.get(f"/g/{g['slug']}/download/asset/{bid}")
        assert r.status_code == 200
        assert (
            r.headers["content-disposition"]
            == "attachment; filename*=utf-8''caf%C3%A9%20men%C3%BC.jpg"
        )


@pytest.mark.integration
def test_conditional_resave_is_304_and_does_not_double_log(gallery):
    g, (aid, _) = gallery
    with TestClient(app) as c:
        _unlock(c, g)
        url = f"/g/{g['slug']}/download/asset/{aid}"
        etag = c.get(url).headers["etag"]
        again = c.get(url, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        assert again.headers.get("cache-control") == "private, max-age=86400"
    # One delivery, one row — a revalidation of bytes the client already
    # holds is not a second download.
    assert len(_rows(g)) == 1


@pytest.mark.integration
def test_a_conditional_request_is_not_a_way_around_the_gate(gallery):
    g, (aid, _) = gallery
    url = f"/g/{g['slug']}/download/asset/{aid}"
    with TestClient(app) as authed:
        _unlock(authed, g)
        etag = authed.get(url).headers["etag"]
    with TestClient(app) as anon:
        r = anon.get(url, headers={"If-None-Match": etag})
        assert r.status_code != 304, "served a 304 to a caller with no gallery cookie"
        assert r.status_code == 403


@pytest.mark.integration
def test_gallery_page_ships_the_save_hook(gallery):
    g, _ = gallery
    with TestClient(app) as c:
        c.post(f"/g/{g['slug']}/pin", data={"pin": "1234"}, follow_redirects=False)
        page = c.get(f"/g/{g['slug']}").text
    # Pinned markup hook: lightbox.js reveals this button on share-capable
    # devices; it must ship hidden so desktop stays exactly as it was.
    assert 'class="lb-save"' in page
    assert '<button type="button" class="lb-save" aria-label="Save photo to device" hidden>' in page


@pytest.mark.unit
def test_lightbox_save_source_contract():
    """The Save wiring keeps the gate honest and degrades to the plain anchor."""
    root = Path(__file__).resolve().parents[1]
    javascript = (root / "static/lightbox.js").read_text()

    # Feature detection must not crash a navigator-less runtime (the Node
    # contract harness), and must ask specifically about sharing FILES.
    assert 'typeof navigator !== "undefined"' in javascript
    assert "navigator.canShare({ files:" in javascript

    # Stills only — a multi-GB camera original must not ride a blob.
    render = javascript.split("function render(i)", 1)[1].split("// Return focus", 1)[0]
    assert (
        'saveBtn.hidden = !(canShareFiles && t.dataset.dl && t.dataset.kind !== "video");' in render
    )

    handler = javascript.split('if (saveBtn) saveBtn.addEventListener("click"', 1)[1]
    # The fetch rides the SAME gated URL as the download anchor — no separate
    # ungated endpoint for the save path.
    assert 'dlLink && dlLink.getAttribute("href")' in handler
    assert "navigator.share({ files: [file] })" in handler
    # Non-image response = the gate intercepted → navigate so the visitor sees
    # it; a dismissed share sheet (AbortError) is not a failure and must not
    # trigger the fallback download.
    assert '!type.startsWith("image/")' in handler
    assert handler.count("window.location.href = url;") == 2
    assert 'err.name !== "AbortError"' in handler

    # Both legacy sheets of the chip stay addressable when hidden (the button
    # ships hidden and is revealed per-frame by render()).
    css = (root / "static/mise.css").read_text()
    assert ".lightbox .lb-save[hidden] { display: none; }" in css
