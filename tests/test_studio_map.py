"""The map uses the existing admin projection and never changes project state."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app


@pytest.mark.unit
def test_map_layout_contract():
    node = shutil.which("node")
    assert node, "Node.js is required for the map layout contract"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests/js/studio-map.test.cjs")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=root,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
def test_studio_map_is_an_admin_only_read_projection():
    before = [tuple(row) for row in db.all_("SELECT id, status FROM projects ORDER BY id")]
    with TestClient(app) as client:
        assert client.get("/admin/studio", follow_redirects=False).status_code == 303
        client.post("/admin/login", data={"password": config.ADMIN_PASSWORD})
        response = client.get("/admin/studio")
        assert response.status_code == 200
        assert 'data-view="map"' in response.text
        assert 'id="studio-map"' in response.text
        assert 'data-map-clock aria-label="Map age metric"' in response.text
        assert "/static/studio-map.js?v=" in response.text
        assert "/static/studio-map.css?v=" in response.text
        public = client.get("/")
        assert 'id="studio-map"' not in public.text
        assert "/static/studio-map.js" not in public.text
    after = [tuple(row) for row in db.all_("SELECT id, status FROM projects ORDER BY id")]
    assert after == before
