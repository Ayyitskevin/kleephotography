"""Platekit Argus completion hook — mock outbound HTTP only."""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, db, platekit
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
        r = client.post("/admin/login", data={"password": "test-pw"}, follow_redirects=False)
        assert r.status_code == 303
        yield client


def test_notify_argus_complete_posts_to_platekit(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PLATEKIT_API_BASE", "http://platekit:8450")
    monkeypatch.setattr(config, "PLATEKIT_API_TOKEN", "pk-secret")

    client_id = db.run(
        "INSERT INTO clients (name, company, email, platekit_slug) VALUES (?,?,?,?)",
        ("Avery", "Blue Plate", "a@example.com", "blue-plate"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, client_id) VALUES (?,?,?,?,?)",
        ("HookGal01", "Spring shoot", "1234", 1, client_id),
    )

    captured = {}

    class FakeResp:
        def read(self):
            return json.dumps({"ok": True, "pack_id": 99}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(platekit.urllib.request, "urlopen", fake_urlopen)
    platekit.notify_argus_complete(gid, 55)

    assert "/api/mise/organizations/blue-plate/argus-pack" in captured["url"]
    assert captured["body"]["argus_run_id"] == 55
    assert captured["body"]["mise_gallery_id"] == gid
    assert captured["body"]["gallery_title"] == "Spring shoot"
    assert captured["auth"] == "Bearer pk-secret"


def test_notify_skipped_when_bridge_disarmed(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PLATEKIT_API_BASE", "")
    monkeypatch.setattr(config, "PLATEKIT_API_TOKEN", "")

    called = {"n": 0}

    def fake_urlopen(req, timeout):
        called["n"] += 1

    monkeypatch.setattr(platekit.urllib.request, "urlopen", fake_urlopen)
    platekit.notify_argus_complete(1, 2)
    assert called["n"] == 0


def test_argus_callback_triggers_platekit_hook(admin_client, monkeypatch):
    monkeypatch.setattr(config, "ARGUS_TOKEN", "api-secret")
    monkeypatch.setattr(config, "PLATEKIT_API_BASE", "http://platekit:8450")
    monkeypatch.setattr(config, "PLATEKIT_API_TOKEN", "pk-secret")

    client_id = db.run(
        "INSERT INTO clients (name, company, email, platekit_slug) VALUES (?,?,?,?)",
        ("Avery", "Blue Plate", "a@example.com", "blue-plate"),
    )
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, client_id) VALUES (?,?,?,?,?)",
        ("CbHook01", "Callback hook", "1234", 1, client_id),
    )

    hooked = {"n": 0}

    def fake_notify(gallery_id, run_id):
        hooked["n"] += 1
        assert gallery_id == gid
        assert run_id == 99

    monkeypatch.setattr(platekit, "notify_argus_complete", fake_notify)

    headers = {"Authorization": "Bearer api-secret", "Content-Type": "application/json"}
    r = admin_client.post(
        f"/api/argus/callback?gallery_id={gid}",
        headers=headers,
        json={"status": "done", "run_id": 99, "job_id": "job-xyz"},
    )
    assert r.status_code == 200
    assert hooked["n"] == 1
