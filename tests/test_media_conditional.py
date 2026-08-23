"""Conditional requests on gallery media.

Starlette's FileResponse sends ETag and Last-Modified but never reads the
conditional request headers back — that logic lives in StaticFiles, which these
routes cannot use because every byte is behind a PIN. So once the 24h freshness
window lapsed, a returning client re-downloaded the whole grid in full, having
just presented the ETag proving nothing had changed.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def gallery():
    slug = f"cond{time.time_ns()}"
    db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)", (slug, "Cond", "1234")
    )
    g = db.one("SELECT * FROM galleries WHERE slug=?", (slug,))
    base = config.MEDIA_DIR / str(g["id"])
    for sub in ("original", "web", "thumb"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / "thumb" / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 4000)
    db.run(
        "INSERT INTO assets (gallery_id,kind,filename,stored,bytes,status) "
        "VALUES (?,?,?,?,?,'ready')",
        (g["id"], "photo", "a.jpg", "a.jpg", 4000),
    )
    aid = db.one("SELECT id FROM assets WHERE gallery_id=?", (g["id"],))["id"]
    yield slug, aid, base / "thumb" / "a.jpg"
    db.run("DELETE FROM assets WHERE gallery_id=?", (g["id"],))
    db.run("DELETE FROM galleries WHERE id=?", (g["id"],))


@pytest.fixture
def unlocked(gallery):
    slug, aid, path = gallery
    with TestClient(app) as c:
        c.post(f"/g/{slug}/pin", data={"pin": "1234"}, follow_redirects=False)
        yield c, f"/media/{slug}/thumb/{aid}", path


def test_matching_etag_gets_304_with_no_body(unlocked):
    c, url, _ = unlocked
    first = c.get(url)
    assert first.status_code == 200 and first.content
    etag = first.headers["etag"]

    again = c.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    # The freshness window must be renewed, or the client re-asks immediately.
    assert again.headers.get("cache-control") == "private, max-age=86400"
    assert again.headers.get("etag") == etag


def test_if_modified_since_also_gets_304(unlocked):
    c, url, _ = unlocked
    first = c.get(url)
    r = c.get(url, headers={"If-Modified-Since": first.headers["last-modified"]})
    assert r.status_code == 304


def test_weak_validator_in_a_list_still_matches(unlocked):
    c, url, _ = unlocked
    etag = c.get(url).headers["etag"]
    r = c.get(url, headers={"If-None-Match": f'W/{etag}, "something-else"'})
    assert r.status_code == 304


def test_stale_etag_gets_the_bytes(unlocked):
    c, url, _ = unlocked
    r = c.get(url, headers={"If-None-Match": '"not-the-current-tag"'})
    assert r.status_code == 200 and r.content


def test_changed_file_invalidates_the_old_etag(unlocked):
    """The safety property: a re-processed derivative must never serve stale."""
    c, url, path = unlocked
    etag = c.get(url).headers["etag"]
    time.sleep(1.1)  # mtime has second resolution
    path.write_bytes(b"\xff\xd8\xff" + b"y" * 9000)

    r = c.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 200
    assert len(r.content) == 9003
    assert r.headers["etag"] != etag


def test_304_still_requires_the_pin(gallery):
    """A conditional request must not become a way around the gate.

    Answering 304 to an unauthenticated caller would confirm the asset exists and
    hand them a live freshness window, so the visitor check has to run first.
    """
    slug, aid, _ = gallery
    with TestClient(app) as authed:
        authed.post(f"/g/{slug}/pin", data={"pin": "1234"}, follow_redirects=False)
        etag = authed.get(f"/media/{slug}/thumb/{aid}").headers["etag"]

    with TestClient(app) as anon:
        r = anon.get(f"/media/{slug}/thumb/{aid}", headers={"If-None-Match": etag})
        assert r.status_code != 304, "served a 304 to a caller with no gallery cookie"
        assert r.status_code == 403


# ── Public portfolio media (/site/img, /site/vid, /site/poster) ───────────────
#
# The same defect, in the routes that face crawlers and every repeat visitor:
# they set a 24h public window but never read the conditional headers back, so
# every returning browser and every cold Cloudflare edge re-pulled full-size
# frames it already held. Same seam now (app/http_cache.py), opposite sharing
# rule — portfolio bytes are public by definition, so they may sit in a shared
# cache where gallery bytes may not.


@pytest.fixture
def starred(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    gid = db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        (f"pub{time.time_ns()}", "Pub", "1234"),
    )
    web = tmp_path / str(gid) / "web"
    thumb = tmp_path / str(gid) / "thumb"
    web.mkdir(parents=True)
    thumb.mkdir(parents=True)
    (thumb / "p.jpg").write_bytes(b"\xff\xd8\xff" + b"t" * 800)
    (web / "p.jpg").write_bytes(b"\xff\xd8\xff" + b"w" * 4000)
    (web / "v.mp4").write_bytes(b"mp4" + b"v" * 2000)
    (web / "v_poster.jpg").write_bytes(b"\xff\xd8\xff" + b"p" * 1500)
    photo = db.run(
        "INSERT INTO assets (gallery_id,kind,filename,stored,status,portfolio) "
        "VALUES (?,'photo','p.jpg','p.jpg','ready',1)",
        (gid,),
    )
    video = db.run(
        "INSERT INTO assets (gallery_id,kind,filename,stored,status,portfolio) "
        "VALUES (?,'video','v.mp4','v.mp4','ready',1)",
        (gid,),
    )
    try:
        with TestClient(app) as c:
            yield c, photo, video, web / "p.jpg"
    finally:
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


def test_public_portfolio_routes_answer_a_matching_etag_with_304(starred):
    c, photo, video, _ = starred
    for url in (f"/site/img/{photo}", f"/site/vid/{video}", f"/site/poster/{video}"):
        first = c.get(url)
        assert first.status_code == 200 and first.content, url
        etag = first.headers["etag"]

        again = c.get(url, headers={"If-None-Match": etag})
        assert again.status_code == 304, url
        assert again.content == b"", url
        # Renew the window, or the client re-asks on the very next navigation.
        assert again.headers.get("etag") == etag, url
        assert again.headers.get("cache-control") == "public, max-age=86400", url


def test_public_portfolio_media_stays_shared_cacheable(starred):
    """Portfolio frames are public; marking them `private` would disable the
    Cloudflare edge cache that keeps the marketing site fast."""
    c, photo, _, _ = starred
    assert c.get(f"/site/img/{photo}").headers["cache-control"] == "public, max-age=86400"


def test_public_thumb_variant_gets_its_own_validator(starred):
    """thumb and web are different bytes behind one route — one ETag each."""
    c, photo, _, _ = starred
    web = c.get(f"/site/img/{photo}").headers["etag"]
    thumb = c.get(f"/site/img/{photo}?variant=thumb").headers["etag"]
    assert web != thumb


def test_changed_public_derivative_invalidates_the_old_etag(starred):
    c, photo, _, path = starred
    etag = c.get(f"/site/img/{photo}").headers["etag"]
    time.sleep(1.1)  # mtime has second resolution
    path.write_bytes(b"\xff\xd8\xff" + b"z" * 9000)

    r = c.get(f"/site/img/{photo}", headers={"If-None-Match": etag})
    assert r.status_code == 200
    assert len(r.content) == 9003
    assert r.headers["etag"] != etag


def test_unstarred_asset_is_still_404_on_a_conditional_request(starred):
    """A 304 must never become a way to probe an unpublished frame."""
    c, photo, _, _ = starred
    etag = c.get(f"/site/img/{photo}").headers["etag"]
    db.run("UPDATE assets SET portfolio=0 WHERE id=?", (photo,))
    r = c.get(f"/site/img/{photo}", headers={"If-None-Match": etag})
    assert r.status_code == 404


# ── Content negotiation (AVIF / WebP alongside the JPEG) ─────────────────────


@pytest.fixture
def negotiable(tmp_path, monkeypatch):
    """A starred photo with real AVIF/WebP siblings next to its JPEG."""
    from PIL import Image

    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    gid = db.run(
        "INSERT INTO galleries (slug,title,pin,published) VALUES (?,?,?,1)",
        (f"neg{time.time_ns()}", "Neg", "1234"),
    )
    web = tmp_path / str(gid) / "web"
    thumb = tmp_path / str(gid) / "thumb"
    original = tmp_path / str(gid) / "original"
    for d in (web, thumb, original):
        d.mkdir(parents=True)
    src = original / "n.jpg"
    Image.new("RGB", (900, 600), (140, 90, 40)).save(src, "JPEG", quality=92)
    from app import imaging

    imaging.make_derivatives(str(src), str(web / "n.jpg"), str(thumb / "n.jpg"), 600, 200, 85)
    aid = db.run(
        "INSERT INTO assets (gallery_id,kind,filename,stored,status,portfolio) "
        "VALUES (?,'photo','n.jpg','n.jpg','ready',1)",
        (gid,),
    )
    slug = db.one("SELECT slug FROM galleries WHERE id=?", (gid,))["slug"]
    try:
        with TestClient(app) as c:
            yield c, slug, aid, web
    finally:
        db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))


AVIF_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
WEBP_ACCEPT = "image/webp,image/apng,image/*,*/*;q=0.8"
LEGACY_ACCEPT = "image/*,*/*;q=0.8"


def test_public_frame_is_negotiated_down_and_always_varies(negotiable):
    c, _, aid, _ = negotiable
    avif = c.get(f"/site/img/{aid}", headers={"Accept": AVIF_ACCEPT})
    webp = c.get(f"/site/img/{aid}", headers={"Accept": WEBP_ACCEPT})
    jpeg = c.get(f"/site/img/{aid}", headers={"Accept": LEGACY_ACCEPT})

    assert avif.headers["content-type"] == "image/avif"
    assert webp.headers["content-type"] == "image/webp"
    assert jpeg.headers["content-type"] == "image/jpeg"
    # Without Vary, Cloudflare would hand a cached AVIF to a browser that
    # cannot decode one — the classic negotiation cache-poisoning bug.
    for r in (avif, webp, jpeg):
        assert r.headers.get("vary") == "Accept", r.headers


def test_each_encoding_gets_its_own_validator(negotiable):
    """One URL, three bodies — a shared ETag would let a 304 return the wrong
    bytes to a client holding a different encoding."""
    c, _, aid, _ = negotiable
    tags = {
        c.get(f"/site/img/{aid}", headers={"Accept": a}).headers["etag"]
        for a in (AVIF_ACCEPT, WEBP_ACCEPT, LEGACY_ACCEPT)
    }
    assert len(tags) == 3


def test_a_304_on_a_negotiated_frame_still_varies(negotiable):
    c, _, aid, _ = negotiable
    first = c.get(f"/site/img/{aid}", headers={"Accept": AVIF_ACCEPT})
    again = c.get(
        f"/site/img/{aid}", headers={"Accept": AVIF_ACCEPT, "If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304
    assert again.headers.get("vary") == "Accept"


def test_an_avif_etag_does_not_satisfy_a_jpeg_request(negotiable):
    """The safety property: switching Accept must re-fetch, not 304."""
    c, _, aid, _ = negotiable
    avif_tag = c.get(f"/site/img/{aid}", headers={"Accept": AVIF_ACCEPT}).headers["etag"]
    r = c.get(f"/site/img/{aid}", headers={"Accept": LEGACY_ACCEPT, "If-None-Match": avif_tag})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_gallery_frames_negotiate_but_originals_never_do(negotiable):
    """The original is the master file the client paid for. thumb/web are
    presentation copies and may be substituted; the original may not."""
    c, slug, aid, _ = negotiable
    c.post(f"/g/{slug}/pin", data={"pin": "1234"}, follow_redirects=False)

    thumb = c.get(f"/media/{slug}/thumb/{aid}", headers={"Accept": AVIF_ACCEPT})
    assert thumb.headers["content-type"] == "image/avif"
    assert thumb.headers.get("vary") == "Accept"

    # Originals are behind the email gate as well as the PIN.
    gid = db.one("SELECT id FROM galleries WHERE slug=?", (slug,))["id"]
    db.run("UPDATE visitors SET email=? WHERE gallery_id=?", ("client@example.com", gid))
    original = c.get(f"/media/{slug}/original/{aid}", headers={"Accept": AVIF_ACCEPT})
    assert original.status_code == 200
    assert original.headers["content-type"] == "image/jpeg"


def test_a_gallery_with_no_siblings_behaves_exactly_as_before(negotiable):
    """Deployable with no backfill: delete the siblings and nothing changes."""
    c, _, aid, web = negotiable
    for sibling in list(web.glob("n.*")):
        if sibling.suffix != ".jpg":
            sibling.unlink()
    r = c.get(f"/site/img/{aid}", headers={"Accept": AVIF_ACCEPT})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
