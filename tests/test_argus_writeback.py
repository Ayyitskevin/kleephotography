"""Argus vision writeback into Mise asset rows."""

import json

import pytest

from app import argus_writeback, config, db


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
    monkeypatch.setattr(config, "ARGUS_URL", "http://argus:8010")
    monkeypatch.setattr(config, "ARGUS_TOKEN", "secret")
    db.migrate()


def test_apply_to_gallery_matches_assets_by_stored_name(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published) VALUES (?,?,?,1)",
        ("Write01", "Write", "1234"),
    )
    a1 = db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'hero.jpg', 'stored-hero.jpg', 'ready')""",
        (gid,),
    )
    db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'detail.jpg', 'stored-detail.jpg', 'ready')""",
        (gid,),
    )

    export = {
        "photos": [
            {
                "basename": "stored-hero.jpg",
                "image_path": f"/media/{gid}/original/stored-hero.jpg",
                "keywords": ["seared", "scallop"],
                "alt_text": "Seared scallop on slate",
                "culling": {"keeper_score": 0.92, "hero_potential": 0.88},
            },
            {
                "basename": "stored-detail.jpg",
                "image_path": f"/media/{gid}/original/stored-detail.jpg",
                "keywords": ["texture"],
                "alt_text": "Charred broccolini detail",
                "culling": {"keeper_score": 0.41, "hero_potential": 0.2},
            },
        ]
    }

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(export).encode()

    monkeypatch.setattr(argus_writeback.urllib.request, "urlopen", lambda req, timeout: FakeResp())
    out = argus_writeback.apply_to_gallery(gid, 77)

    assert out["matched"] == 2
    hero = db.one(
        "SELECT argus_hero_asset_ids, argus_analyzed_count FROM galleries WHERE id=?", (gid,)
    )
    assert hero["argus_analyzed_count"] == 2
    assert json.loads(hero["argus_hero_asset_ids"]) == [a1]

    row = db.one(
        "SELECT argus_alt_text, argus_keywords, argus_keeper_score FROM assets WHERE id=?", (a1,)
    )
    assert row["argus_alt_text"] == "Seared scallop on slate"
    assert json.loads(row["argus_keywords"]) == ["seared", "scallop"]
    assert row["argus_keeper_score"] == pytest.approx(0.92)


def test_media_count_changed_detects_new_uploads(tmp_path, monkeypatch):
    _configure_tmp_db(tmp_path, monkeypatch)
    from app import argus_analyze

    gid = db.run(
        "INSERT INTO galleries (slug, title, pin, published, argus_analyzed_count) VALUES (?,?,?,1,2)",
        ("Count01", "Count", "1234"),
    )
    db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'a.jpg', 'a.jpg', 'ready')""",
        (gid,),
    )
    assert argus_analyze.media_count_changed(gid) is True

    db.run(
        """INSERT INTO assets (gallery_id, kind, filename, stored, status)
           VALUES (?, 'photo', 'b.jpg', 'b.jpg', 'ready')""",
        (gid,),
    )
    assert argus_analyze.media_count_changed(gid) is False
