"""Public-surface security gates — favorites-ZIP keying + disk floor, the /media
originals email gate, the zip_status visitor gate, and webhook body caps."""

from __future__ import annotations

import collections
import shutil

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
