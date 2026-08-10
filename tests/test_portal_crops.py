"""Portal social crops — honesty about an encode that is never landing, and the
disk floor / in-flight guard on the crops archive."""

from __future__ import annotations

import collections
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, jobs, presets
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def portal(client):
    """A published portal with one favorited, ready photo — the shape the crops
    block renders from. Torn down so the shared session DB stays clean."""
    cid = db.run("INSERT INTO clients (name) VALUES (?)", ("Crop Snag Co",))
    pid = db.run(
        "INSERT INTO portals (client_id, slug, pin, published) VALUES (?,?,?,1)",
        (cid, "crop-snag", "2468"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, client_id) VALUES (?,?,?,1,?)",
        ("crop-snag-g", "Crop Snag", "1234", cid),
    )
    aid = db.run(
        "INSERT INTO assets (gallery_id, filename, stored, kind, status) VALUES (?,?,?,?,?)",
        (gid, "hero.jpg", "hero.jpg", "photo", "ready"),
    )
    vid = db.run("INSERT INTO visitors (gallery_id, token) VALUES (?,?)", (gid, "crop-snag-vis"))
    db.run("INSERT INTO favorites (visitor_id, asset_id) VALUES (?,?)", (vid, aid))
    client.post("/portal/crop-snag/pin", data={"pin": "2468"}, follow_redirects=False)
    try:
        yield {"client_id": cid, "portal_id": pid, "gallery_id": gid, "asset_id": aid}
    finally:
        db.run("DELETE FROM jobs WHERE kind='social_crops'")
        db.run("DELETE FROM favorites WHERE visitor_id=?", (vid,))
        db.run("DELETE FROM visitors WHERE id=?", (vid,))
        db.run("DELETE FROM assets WHERE id=?", (aid,))
        db.run("DELETE FROM galleries WHERE id=?", (gid,))
        db.run("DELETE FROM portals WHERE id=?", (pid,))
        db.run("DELETE FROM clients WHERE id=?", (cid,))
        shutil.rmtree(jobs.crops_dir(gid), ignore_errors=True)
        # DATA_DIR is shared across the session and portal ids recycle after
        # DELETE — drop built zips so the next test's glob starts empty
        for z in config.ZIP_DIR.glob(f"p{pid}-*.zip"):
            z.unlink()
        client.cookies.clear()


def _job(asset_id: int, status: str) -> int:
    return db.run(
        "INSERT INTO jobs (kind, payload, status) VALUES ('social_crops', ?, ?)",
        (f'{{"asset_id": {asset_id}}}', status),
    )


def _write_crops(gallery_id: int, stem: str) -> None:
    out = jobs.crops_dir(gallery_id)
    out.mkdir(parents=True, exist_ok=True)
    for ps in presets.active():
        (out / f"{stem}_{ps['slug']}.jpg").write_bytes(b"fake-crop-bytes")


def test_dead_crop_job_stops_the_poll_and_says_so(client, portal):
    """A social_crops job that exhausted its retries is never landing. The block
    must say so plainly and drop the 8s self-poll instead of spinning forever."""
    _job(portal["asset_id"], "failed")
    page = client.get("/portal/crop-snag").text
    assert "hit a snag" in page
    assert "preparing" not in page
    assert "hx-trigger" not in page


def test_requeued_crop_job_keeps_the_client_waiting(client, portal):
    """The operator's retry queues a fresh job for the same asset — that live
    row supersedes the dead one, so the block goes back to preparing + polling."""
    _job(portal["asset_id"], "failed")
    _job(portal["asset_id"], "queued")
    page = client.get("/portal/crop-snag").text
    assert "hit a snag" not in page
    assert "preparing" in page
    assert 'hx-trigger="every 8s"' in page


def test_ready_crops_outlive_a_dead_job(client, portal):
    """A ratio already on disk stays a download link even when a later encode
    attempt for that asset died — the snag chip is only for missing files."""
    _write_crops(portal["gallery_id"], "hero")
    _job(portal["asset_id"], "failed")
    page = client.get("/portal/crop-snag").text
    assert f"/portal/crop-snag/crop/{portal['asset_id']}/1x1" in page
    assert "hit a snag" not in page
    assert "hx-trigger" not in page


def test_crops_zip_refused_on_low_disk(client, portal, monkeypatch):
    """A client click must not be able to force an unbounded ZIP build onto a
    full disk — same floor and same 507 the gallery favorites ZIP uses."""
    _write_crops(portal["gallery_id"], "hero")
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 0))
    r = client.get("/portal/crop-snag/crops.zip")
    assert r.status_code == 507
    assert list(config.ZIP_DIR.glob(f"p{portal['portal_id']}-*.zip")) == []


def test_crops_zip_builds_once_under_concurrent_clicks(client, portal, monkeypatch):
    """Two clicks on one archive is one build. Without the in-flight guard both
    requests copy the same files into the same .part temp at the same time and
    the loser of the rename raises."""
    _write_crops(portal["gallery_id"], "hero")
    real_build = jobs.build_zip
    calls = []

    def slow_build(out, entries):
        calls.append(out)
        time.sleep(0.3)
        real_build(out, entries)

    monkeypatch.setattr(jobs, "build_zip", slow_build)
    results: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(client.get("/portal/crop-snag/crops.zip").status_code)
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [200, 200]
    assert len(calls) == 1
