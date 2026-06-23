"""Plutus upsell integration smoke tests."""
from __future__ import annotations

import json

from app import config, db, jobs, plutus_recommend


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


def test_plutus_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "")
    assert plutus_recommend.is_enabled() is False
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    assert plutus_recommend.is_enabled() is False
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    assert plutus_recommend.is_enabled() is True


def test_run_for_gallery_records_done(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, type, published) VALUES (?,?,?,?,1)",
        ("abc", "Test", "1234", "gallery"),
    )
    payload = {"run_id": 12, "bundles": [{"id": "wall-hero"}]}

    class _Resp:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(plutus_recommend.urllib.request, "urlopen", lambda req, timeout: _Resp())
    plutus_recommend.run_for_gallery(gid)
    row = db.one("SELECT plutus_last_run_id, plutus_last_status FROM galleries WHERE id=?", (gid,))
    assert row["plutus_last_run_id"] == 12
    assert row["plutus_last_status"] == "done"


def test_argus_callback_enqueues_plutus(tmp_path, monkeypatch):
    from app import argus_analyze

    _configure_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, type, published) VALUES (?,?,?,?,1)",
        ("abc", "Test", "1234", "gallery"),
    )
    enqueued: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        jobs, "enqueue",
        lambda kind, payload: enqueued.append((kind, payload)) or 1,
    )
    argus_analyze.apply_callback(gid, {"status": "done", "run_id": 5})
    assert ("plutus_recommend_gallery", {"gallery_id": gid}) in enqueued