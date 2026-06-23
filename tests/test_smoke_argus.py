"""Argus Phase 6 wiring — mock outbound HTTP only."""

import json

import pytest
from fastapi.testclient import TestClient

from app import argus_analyze, config, db, jobs
from app.main import app


def _configure_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "zips")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "BRAND_DIR", tmp_path / "brand")
    monkeypatch.setattr(config, "RECEIPTS_DIR", tmp_path / "receipts")
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "test-pw")
    db.migrate()


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/admin/login", data={"password": "test-pw"},
                        follow_redirects=False)
        assert r.status_code == 303
        yield client
    jobs.stop()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit
    ratelimit._hits.clear()
    yield


def test_argus_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "ARGUS_URL", "")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "")
    assert argus_analyze.is_enabled() is False
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    assert argus_analyze.is_enabled() is False
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    assert argus_analyze.is_enabled() is True


def test_publish_enqueues_argus_job(admin_client, monkeypatch):
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
                 ("ArgusPub01", "Argus Pub", "1234"))

    def n_jobs():
        return db.one("""SELECT COUNT(*) AS n FROM jobs WHERE kind='argus_analyze_gallery'
                         AND json_extract(payload,'$.gallery_id')=?""", (gid,))["n"]

    r = admin_client.post(f"/admin/galleries/{gid}/settings",
                          data={"title": "Argus Pub", "pin": "1234", "published": "true"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert n_jobs() == 1
    admin_client.post(f"/admin/galleries/{gid}/settings",
                      data={"title": "Argus Pub", "pin": "1234", "published": "true"},
                      follow_redirects=False)
    assert n_jobs() == 1


def test_publish_does_not_enqueue_when_argus_disabled(admin_client, monkeypatch):
    # Argus off → publishing must not stamp spurious "error" state via a job
    # that only fails the enabled check.
    monkeypatch.setattr(config, "ARGUS_URL", "")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "")
    gid = db.run("INSERT INTO galleries (slug, title, pin) VALUES (?,?,?)",
                 ("ArgusOff01", "Argus Off", "1234"))
    r = admin_client.post(f"/admin/galleries/{gid}/settings",
                          data={"title": "Argus Off", "pin": "1234", "published": "true"},
                          follow_redirects=False)
    assert r.status_code == 303
    n = db.one("""SELECT COUNT(*) AS n FROM jobs WHERE kind='argus_analyze_gallery'
                  AND json_extract(payload,'$.gallery_id')=?""", (gid,))["n"]
    assert n == 0


def test_run_for_gallery_records_queued(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("ArgusRun01", "Run", "1234"))

    class FakeResp:
        def read(self):
            return json.dumps({"mode": "queued", "job_id": "job-abc", "status": "queued"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(argus_analyze.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp())
    argus_analyze.run_for_gallery(gid)
    row = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    assert row["argus_last_job_id"] == "job-abc"
    assert row["argus_last_status"] == "queued"
    assert row["argus_last_at"]


def test_run_for_gallery_records_sync_run(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("ArgusSync01", "Sync", "1234"))

    class FakeResp:
        def read(self):
            return json.dumps({"mode": "sync", "run_id": 42, "count": 3}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(argus_analyze.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp())
    argus_analyze.run_for_gallery(gid)
    row = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    assert row["argus_last_run_id"] == 42
    assert row["argus_last_status"] == "done"


def test_run_for_gallery_swallows_errors(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("ArgusErr01", "Err", "1234"))

    def boom(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(argus_analyze.urllib.request, "urlopen", boom)
    argus_analyze.run_for_gallery(gid)
    row = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    assert row["argus_last_status"] == "error"
    assert "timed out" in row["argus_last_error"]


def test_manual_analyze_route(admin_client, monkeypatch):
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("ArgusMan01", "Manual", "1234"))
    before = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='argus_analyze_gallery'")["n"]
    r = admin_client.post(f"/admin/galleries/{gid}/argus-analyze", follow_redirects=False)
    assert r.status_code == 303
    after = db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='argus_analyze_gallery'")["n"]
    assert after == before + 1


def test_argus_callback_updates_gallery(admin_client, monkeypatch):
    monkeypatch.setattr(config, "ARGUS_TOKEN", "api-secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("CbGal01", "Callback", "1234"))
    headers = {"Authorization": "Bearer api-secret", "Content-Type": "application/json"}
    r = admin_client.post(
        f"/api/argus/callback?gallery_id={gid}",
        headers=headers,
        json={"status": "done", "run_id": 99, "job_id": "job-xyz"},
    )
    assert r.status_code == 200
    row = db.one("SELECT * FROM galleries WHERE id=?", (gid,))
    assert row["argus_last_run_id"] == 99
    assert row["argus_last_status"] == "done"


def test_galleries_api(admin_client, monkeypatch):
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "")
    r = admin_client.get("/api/galleries")
    assert r.status_code == 503

    monkeypatch.setattr(config, "ARGUS_TOKEN", "api-secret")
    gid = db.run("INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
                 ("ApiGal01", "API Gal", "1234"))
    headers = {"Authorization": "Bearer api-secret"}
    ok = admin_client.get("/api/galleries", headers=headers)
    assert ok.status_code == 200
    body = ok.json()
    ids = [g["id"] for g in body["galleries"]]
    assert gid in ids
    match = next(g for g in body["galleries"] if g["id"] == gid)
    assert match["slug"] == "ApiGal01"
    assert match["published"] is True
    assert match["originals_path"].endswith(f"/media/{gid}/original")

    bad = admin_client.get("/api/galleries", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401