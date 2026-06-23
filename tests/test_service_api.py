import datetime as dt

from fastapi.testclient import TestClient

from app import config, db
from app.main import app


def configure_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mise.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "ZIP_DIR", tmp_path / "zips")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "BRAND_DIR", tmp_path / "brand")
    monkeypatch.setattr(config, "RECEIPTS_DIR", tmp_path / "receipts")
    monkeypatch.setattr(config, "SHOTS_TOKEN", "service-test-token")
    db.migrate()


def bearer():
    return {"Authorization": "Bearer service-test-token"}


def test_galleries_expiring_api_is_bearer_gated_and_filters_window(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path, monkeypatch)
    today = dt.date.today()
    db.run(
        """INSERT INTO galleries (slug,title,pin,published,client_name,expires_at)
           VALUES (?,?,?,?,?,?)""",
        ("expiring-demo", "Expiring Demo", "1234", 1, "Blue Plate",
         (today + dt.timedelta(days=3)).isoformat()),
    )
    db.run(
        """INSERT INTO galleries (slug,title,pin,published,client_name,expires_at)
           VALUES (?,?,?,?,?,?)""",
        ("late-demo", "Late Demo", "1234", 1, "Blue Plate",
         (today + dt.timedelta(days=20)).isoformat()),
    )
    db.run(
        """INSERT INTO galleries (slug,title,pin,published,client_name,expires_at)
           VALUES (?,?,?,?,?,?)""",
        ("draft-demo", "Draft Demo", "1234", 0, "Blue Plate",
         (today + dt.timedelta(days=3)).isoformat()),
    )

    client = TestClient(app)
    assert client.get("/api/galleries/expiring?days=7").status_code == 401
    res = client.get("/api/galleries/expiring?days=7", headers=bearer())
    assert res.status_code == 200
    body = res.json()
    assert body["horizon_days"] == 7
    assert [g["slug"] for g in body["galleries"]] == ["expiring-demo"]
    assert client.get("/api/galleries/expiring?days=0", headers=bearer()).status_code == 400


def test_press_recent_api_excludes_pending_deleted_and_future_hits(tmp_path, monkeypatch):
    configure_tmp_db(tmp_path, monkeypatch)
    today = dt.date.today()
    db.run(
        """INSERT INTO clients (name, company) VALUES (?,?)""",
        ("Avery", "Blue Plate"),
    )
    client_row = db.one("SELECT * FROM clients WHERE company='Blue Plate'")
    db.run(
        """INSERT INTO press (client_id,outlet,title,url,publish_date,channel)
           VALUES (?,?,?,?,?,?)""",
        (client_row["id"], "Past Times", "Published", "https://example.com/past",
         (today - dt.timedelta(days=5)).isoformat(), "web"),
    )
    db.run(
        """INSERT INTO press (outlet,title,publish_date,channel)
           VALUES (?,?,?,?)""",
        ("Future Weekly", "Future", (today + dt.timedelta(days=5)).isoformat(), "web"),
    )
    db.run(
        """INSERT INTO press (outlet,title,publish_date,channel,deleted_at)
           VALUES (?,?,?,?,datetime('now'))""",
        ("Deleted Daily", "Deleted", (today - dt.timedelta(days=3)).isoformat(), "web"),
    )
    db.run(
        """INSERT INTO press (outlet,title,publish_date,channel)
           VALUES (?,?,?,?)""",
        ("Old Monthly", "Old", (today - dt.timedelta(days=60)).isoformat(), "web"),
    )
    db.run(
        """INSERT INTO press (outlet,title,channel)
           VALUES (?,?,?)""",
        ("Pending Post", "Pending", "web"),
    )

    client = TestClient(app)
    res = client.get("/api/press/recent?days=30", headers=bearer())
    assert res.status_code == 200
    body = res.json()
    assert [h["outlet"] for h in body["hits"]] == ["Past Times"]
    assert body["hits"][0]["company"] == "Blue Plate"
    assert client.get("/api/press/recent?days=91", headers=bearer()).status_code == 400
