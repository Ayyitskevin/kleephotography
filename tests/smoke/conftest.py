"""Shared smoke fixtures — session-scoped so domain files share one DB.

Order coupling (do not "fix" without a plan):
  - Collection order is numeric filename prefixes (test_01_… before test_07_…).
  - Many later tests still assume rows/assets seeded by earlier files.
  - Renaming or reordering files without migrating that seed chain will flake.
  - Shared helpers live in tests/smoke/_helpers.py — prefer importing those
    over redefining local copies (F811).
  - Job-pool races: use tests.jobtest.freeze_job_pool (stop + block lifespan
    restart). Never null jobs._pool alone — that orphans still-running workers.

Worst seed chains (keep order or rewrite the consumer). Each was confirmed by
running the consumer alone against a fresh DB and watching it fail:
  - test_02 test_studio_clients_projects → test_proposal_lifecycle,
    test_contract_lifecycle and test_invoice_lifecycle each open with
    ``projects ORDER BY id DESC LIMIT 1`` and seed nothing themselves, then
    assert literals that test came from ("Dana Chef", "$1151.00", 115100).
  - test_02 test_invoice_lifecycle → leaves a paid invoice for test_notion_sync
    (``assert inv, "earlier test left a paid invoice"``), which also asserts a
    suite-wide ``notion_sync_invoice`` job count of >= 3.
  - test_01's first gallery → test_04 test_today_consolidated_view takes
    ``galleries ORDER BY id LIMIT 1`` — the OLDEST row — and then its first
    asset, so it TypeErrors on an empty DB.
  - Nested ``with TestClient(app)`` stops the session job pool; later upload
    waits rely on a fresh lifespan (or freeze + ``jobs._execute``).

Already broken and kept that way: test_01 test_pin_lockout / test_expired_gallery
and test_02 test_gallery_notion_writeback seed their own rows, and test_03
test_portal_lifecycle seeds its own client, gallery, visitor and favorite. All
pass alone — don't re-couple them.

Disk note: the per-run mkdtemp from tests/conftest.py often lands on a small
/tmp tmpfs. If uploads return 507, point MISE_TEST_DATA_DIR at a large volume
or set MISE_MIN_FREE_GB=1 for the run.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Stamp ``smoke`` on everything under tests/smoke/.

    CI partitions the suite by marker, so a smoke file that forgot its own
    module-level ``pytestmark`` would be run by no step at all — collected out of
    ``-m unit``/``-m integration`` and out of ``-m smoke`` too. The hook is what
    makes that impossible; a ``pytestmark`` in a conftest does nothing (marks
    apply from test modules only). The hook fires for every collected item, so it
    has to filter by path itself.
    """
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.smoke)


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
