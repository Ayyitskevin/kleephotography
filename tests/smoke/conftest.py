"""Shared smoke fixtures — session-scoped so domain files share one DB.

Order coupling (do not "fix" without a plan):
  - Collection order is numeric filename prefixes (test_01_… before test_07_…).
  - Many later tests still assume rows/assets seeded by earlier files.
  - Renaming or reordering files without migrating that seed chain will flake.
  - Shared helpers live in tests/smoke/_helpers.py — prefer importing those
    over redefining local copies (F811).
  - Job-pool races: use tests.jobtest.freeze_job_pool (stop + block lifespan
    restart). Never null jobs._pool alone — that orphans still-running workers.

Disk note: default MISE_DATA_DIR via mktemp often lands on a small /tmp
tmpfs. If uploads return 507, put DATA_DIR on a large volume or set
MISE_MIN_FREE_GB=1 for the run.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

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
