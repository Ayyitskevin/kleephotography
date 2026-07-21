"""Shared smoke fixtures — session-scoped so domain files share one DB.

Collection order is numeric filename prefixes (test_01_… before test_02_…).
Do not rename without preserving that order; many tests still seed state for later ones.
"""

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app

pytestmark = pytest.mark.smoke

os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))
os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin(client):
    r = client.post(
        "/admin/login",
        data={"password": os.environ["MISE_ADMIN_PASSWORD"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit

    ratelimit._hits.clear()
    yield
