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
