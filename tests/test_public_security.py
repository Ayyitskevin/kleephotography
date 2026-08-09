"""Public-surface security gates — favorites-ZIP keying + disk floor, the /media
originals email gate, the zip_status visitor gate, and webhook body caps."""

from __future__ import annotations

import collections
import hashlib
import io
import json
import shutil
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config, db, security
from app.main import app
from app.public import sms_webhook

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit

    ratelimit._hits.clear()
    yield


def _seed_gallery(slug: str, gtype: str = "gallery", expires: str | None = None) -> int:
    return db.run(
        "INSERT INTO galleries (slug, title, pin, published, type, expires_at) "
        "VALUES (?,?,?,1,?,?)",
        (slug, "Sec Test", "1234", gtype, expires),
    )


def _seed_asset(gid: int, stored: str) -> int:
    src = config.MEDIA_DIR / str(gid) / "original"
    src.mkdir(parents=True, exist_ok=True)
    (src / stored).write_bytes(b"fake-image-bytes")
    return db.run(
        "INSERT INTO assets (gallery_id, kind, filename, stored, status) VALUES (?,?,?,?,?)",
        (gid, "photo", stored, stored, "ready"),
    )


def _visitor(gid: int, token: str, email: str | None = None) -> int:
    return db.run(
        "INSERT INTO visitors (gallery_id, token, email) VALUES (?,?,?)", (gid, token, email)
    )


def _auth(client: TestClient, gid: int, token: str) -> None:
    client.cookies.set(security.visitor_cookie_name(gid), security.sign(token))


def _cleanup(client: TestClient, gid: int) -> None:
    db.run("DELETE FROM downloads WHERE gallery_id=?", (gid,))
    db.run(
        "DELETE FROM favorites WHERE asset_id IN (SELECT id FROM assets WHERE gallery_id=?)",
        (gid,),
    )
    db.run("DELETE FROM visitors WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM assets WHERE gallery_id=?", (gid,))
    db.run("DELETE FROM galleries WHERE id=?", (gid,))
    # DATA_DIR is shared across the session and gallery ids recycle after
    # DELETE — drop built zips so the next test's glob starts empty
    for z in config.ZIP_DIR.glob(f"g{gid}-*.zip"):
        z.unlink()
    client.cookies.clear()


# ── favorites ZIP: fav-set keying + disk floor ───────────────────────────────


def test_favorites_zip_shared_by_identical_fav_sets(client):
    gid = _seed_gallery("favzip-01")
    a1 = _seed_asset(gid, "one.jpg")
    a2 = _seed_asset(gid, "two.jpg")
    v1 = _visitor(gid, "favzip-t1", "one@x.test")
    v2 = _visitor(gid, "favzip-t2", "two@x.test")
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v1, a1))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v2, a1))
    try:
        _auth(client, gid, "favzip-t1")
        r = client.get("/g/favzip-01/download/favorites")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        zips = list(config.ZIP_DIR.glob(f"g{gid}-*.zip"))
        # keyed by fav-set hash, not visitor id — a fresh visitor can't force a
        # fresh build of an identical bundle
        assert len(zips) == 1 and zips[0].name.startswith(f"g{gid}-fav-")
        _auth(client, gid, "favzip-t2")
        r = client.get("/g/favzip-01/download/favorites")
        assert r.status_code == 200
        assert list(config.ZIP_DIR.glob(f"g{gid}-*.zip")) == zips
        # a different fav-set builds its successor and cleans the stale one up
        db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v2, a2))
        r = client.get("/g/favzip-01/download/favorites")
        assert r.status_code == 200
        zips2 = list(config.ZIP_DIR.glob(f"g{gid}-*.zip"))
        assert len(zips2) == 1 and zips2[0] != zips[0]
    finally:
        _cleanup(client, gid)


def test_favorites_zip_refused_on_low_disk(client, monkeypatch):
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 0))
    gid = _seed_gallery("favzip-low")
    a1 = _seed_asset(gid, "one.jpg")
    v1 = _visitor(gid, "favzip-low-t1", "low@x.test")
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v1, a1))
    try:
        _auth(client, gid, "favzip-low-t1")
        r = client.get("/g/favzip-low/download/favorites")
        assert r.status_code == 507
        assert list(config.ZIP_DIR.glob(f"g{gid}-*.zip")) == []
    finally:
        _cleanup(client, gid)


# ── favorites / section ZIP: oversized bundles leave the request path ────────


def _drain_zip_subset(client: TestClient, slug: str, name: str) -> dict:
    for _ in range(100):
        s = client.get(f"/g/{slug}/download/zip/status?name={name}").json()
        if s["ready"] or s["failed"]:
            return s
        time.sleep(0.05)
    return s


def test_large_favorites_zip_is_queued_not_built_in_the_request(client):
    """A client who favorites everything in a big gallery must not trigger a
    full byte copy inside the (async, event-loop-blocking) request handler.
    Past config.ZIP_INLINE_MAX_BYTES the bundle is enqueued and the visitor
    gets the same wait/poll page the full-gallery ZIP has always used."""
    gid = _seed_gallery("favbig-01")
    ids = [_seed_asset(gid, f"big{i}.jpg") for i in range(3)]
    ph = ",".join("?" * len(ids))
    db.run(f"UPDATE assets SET bytes=? WHERE id IN ({ph})", (config.ZIP_INLINE_MAX_BYTES, *ids))
    v1 = _visitor(gid, "favbig-t1", "big@x.test")
    for aid in ids:
        db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v1, aid))
    try:
        _auth(client, gid, "favbig-t1")
        r = client.get("/g/favbig-01/download/favorites")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "Packing your export" in r.text
        job = db.one("SELECT payload FROM jobs WHERE kind='zip_subset' ORDER BY id DESC")
        payload = json.loads(job["payload"])
        assert payload["gallery_id"] == gid and payload["asset_ids"] == ids
        # same content-keyed filename the inline branch would have written
        key = hashlib.sha256(",".join(str(i) for i in ids).encode()).hexdigest()[:8]
        assert payload["name"] == f"g{gid}-fav-{key}.zip"
        assert payload["prune"] == f"g{gid}-fav-*.zip"
        # the page polls the shared status endpoint, scoped to THIS bundle
        assert f"/g/favbig-01/download/zip/status?name={payload['name']}" in r.text
        assert _drain_zip_subset(client, "favbig-01", payload["name"]) == {
            "ready": True,
            "failed": False,
        }
        # built by the worker → the same URL now serves the file
        r = client.get("/g/favbig-01/download/favorites")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == [
            f"big{i}.jpg" for i in range(3)
        ]
    finally:
        db.run("DELETE FROM jobs WHERE kind='zip_subset'")
        _cleanup(client, gid)


def test_large_section_zip_is_queued_on_the_asset_count_backstop(client):
    """assets.bytes can be NULL, so the count ceiling has to catch an unmetered
    bundle on its own — and the section route takes the same branch."""
    gid = _seed_gallery("secbig-01")
    sid = db.run("INSERT INTO sections (gallery_id, name) VALUES (?,?)", (gid, "All"))
    n = config.ZIP_INLINE_MAX_ASSETS + 1
    for i in range(n):
        aid = _seed_asset(gid, f"s{i}.jpg")
        db.run("UPDATE assets SET section_id=?, bytes=NULL WHERE id=?", (sid, aid))
    _visitor(gid, "secbig-t1", "sec@x.test")
    try:
        _auth(client, gid, "secbig-t1")
        r = client.get(f"/g/secbig-01/download/section/{sid}")
        assert r.status_code == 200 and "Packing your export" in r.text
        job = db.one("SELECT payload FROM jobs WHERE kind='zip_subset' ORDER BY id DESC")
        payload = json.loads(job["payload"])
        assert payload["name"].startswith(f"g{gid}-s{sid}-") and len(payload["asset_ids"]) == n
        assert _drain_zip_subset(client, "secbig-01", payload["name"])["ready"] is True
    finally:
        db.run("DELETE FROM jobs WHERE kind='zip_subset'")
        db.run("DELETE FROM sections WHERE gallery_id=?", (gid,))
        _cleanup(client, gid)


def test_many_small_metered_favorites_still_download_inline(client):
    """The count ceiling is a backstop for unmetered rows ONLY. A delivery
    client favoriting well past it — the ordinary case, not an abusive one —
    weighs almost nothing, so it must still come back as one immediate file
    rather than being pushed onto the wait page for a sub-second zip."""
    gid = _seed_gallery("favmany-01")
    ids = [_seed_asset(gid, f"m{i}.jpg") for i in range(config.ZIP_INLINE_MAX_ASSETS + 5)]
    ph = ",".join("?" * len(ids))
    db.run(f"UPDATE assets SET bytes=? WHERE id IN ({ph})", (1024, *ids))
    v1 = _visitor(gid, "favmany-t1", "many@x.test")
    for aid in ids:
        db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (v1, aid))
    try:
        _auth(client, gid, "favmany-t1")
        r = client.get("/g/favmany-01/download/favorites")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == len(ids)
        assert db.one("SELECT 1 AS x FROM jobs WHERE kind='zip_subset'") is None
    finally:
        db.run("DELETE FROM jobs WHERE kind='zip_subset'")
        _cleanup(client, gid)


def test_a_superseded_failure_does_not_poison_the_next_subset_build(client):
    """The bundle name is a content hash of the favorite set, so for a stable
    set it never changes. One exhausted build must not make the wait page paint
    the error screen on every later attempt while a fresh job is queued."""
    gid = _seed_gallery("favstale-01")
    name = f"g{gid}-fav-deadbeef.zip"
    payload = json.dumps({"gallery_id": gid, "name": name})
    _visitor(gid, "favstale-t1", "stale@x.test")
    try:
        _auth(client, gid, "favstale-t1")
        db.run(
            "INSERT INTO jobs (kind, payload, status) VALUES ('zip_subset', ?, 'failed')",
            (payload,),
        )
        url = f"/g/favstale-01/download/zip/status?name={name}"
        assert client.get(url).json() == {"ready": False, "failed": True}
        # a fresh attempt for the same bundle supersedes the dead one
        db.run(
            "INSERT INTO jobs (kind, payload, status) VALUES ('zip_subset', ?, 'queued')",
            (payload,),
        )
        assert client.get(url).json() == {"ready": False, "failed": False}
    finally:
        db.run("DELETE FROM jobs WHERE kind='zip_subset'")
        _cleanup(client, gid)


def test_zip_status_subset_name_is_scoped_to_the_gallery(client):
    """?name= is server-minted; the endpoint must refuse anything that isn't
    shaped like a bundle of the gallery the visitor just cleared the gate for
    (and must still refuse a visitor-less caller outright)."""
    gid = _seed_gallery("zipname-01")
    mine = f"g{gid}-fav-0123abcd.zip"
    try:
        assert client.get(f"/g/zipname-01/download/zip/status?name={mine}").status_code == 403
        _visitor(gid, "zipname-t1")
        _auth(client, gid, "zipname-t1")
        r = client.get(f"/g/zipname-01/download/zip/status?name={mine}")
        assert r.status_code == 200 and r.json() == {"ready": False, "failed": False}
        for bad in (f"g{gid + 1}-fav-0123abcd.zip", "../mise.db", "nope.zip", "g1-fav-zz.zip"):
            assert client.get(f"/g/zipname-01/download/zip/status?name={bad}").status_code == 404
        # no ?name at all is still the full-gallery answer, unchanged
        assert client.get("/g/zipname-01/download/zip/status").json() == {
            "ready": False,
            "failed": False,
        }
    finally:
        _cleanup(client, gid)


# ── /media originals email gate ──────────────────────────────────────────────


def test_media_original_requires_email_when_gallery_gates(client):
    gid = _seed_gallery("med-gate-01")
    aid = _seed_asset(gid, "hero.jpg")
    # thumb + web derivatives exist too — they must stay open
    for sub in ("thumb", "web"):
        d = config.MEDIA_DIR / str(gid) / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "hero.jpg").write_bytes(b"deriv")
    vid = _visitor(gid, "med-t1")
    try:
        _auth(client, gid, "med-t1")
        assert client.get(f"/media/med-gate-01/original/{aid}").status_code == 403
        assert client.get(f"/media/med-gate-01/thumb/{aid}").status_code == 200
        assert client.get(f"/media/med-gate-01/web/{aid}").status_code == 200
        db.run("UPDATE visitors SET email='gate@x.test' WHERE id=?", (vid,))
        assert client.get(f"/media/med-gate-01/original/{aid}").status_code == 200
    finally:
        _cleanup(client, gid)


def test_media_original_open_on_drop_gallery(client):
    """Transfers (drops) don't email-gate — the grab goes straight through."""
    gid = _seed_gallery("med-drop-01", gtype="drop")
    aid = _seed_asset(gid, "hero.jpg")
    _visitor(gid, "med-drop-t1")
    try:
        _auth(client, gid, "med-drop-t1")
        assert client.get(f"/media/med-drop-01/original/{aid}").status_code == 200
    finally:
        _cleanup(client, gid)


# ── zip_status visitor/expiry gate ───────────────────────────────────────────


def test_zip_status_requires_visitor(client):
    gid = _seed_gallery("zipst-01")
    try:
        assert client.get("/g/zipst-01/download/zip/status").status_code == 403
        _visitor(gid, "zipst-t1")
        _auth(client, gid, "zipst-t1")
        r = client.get("/g/zipst-01/download/zip/status")
        assert r.status_code == 200 and r.json() == {"ready": False, "failed": False}
    finally:
        _cleanup(client, gid)


def test_zip_status_expired_gallery_is_410(client):
    gid = _seed_gallery("zipst-old", expires="2000-01-01")
    try:
        assert client.get("/g/zipst-old/download/zip/status").status_code == 410
    finally:
        _cleanup(client, gid)


# ── Quo webhook body cap ─────────────────────────────────────────────────────


def test_quo_webhook_rejects_oversized_body(client, monkeypatch):
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "cXVvLXNlY3JldA==")
    r = client.post("/webhooks/quo", content=b"x" * (sms_webhook._MAX_BODY + 1))
    assert r.status_code == 413


def test_quo_webhook_caps_body_without_content_length(client, monkeypatch):
    """A missing Content-Length must not bypass the cap — enforced post-read."""
    monkeypatch.setattr(config, "QUO_WEBHOOK_SECRET", "cXVvLXNlY3JldA==")
    r = client.post("/webhooks/quo", content=iter([b"x" * (sms_webhook._MAX_BODY + 1)]))
    assert r.status_code == 413
