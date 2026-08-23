"""Test-environment defaults.

Pytest imports this before any test module — and therefore before ``app.config``,
which reads these at import time. Setting them here gives the unit suite a
self-contained, writable environment instead of depending on the CI workflow to
export them (the unit step did not, so app-dependent unit tests failed with
``MISE_SECRET_KEY is not set``).

The credential/env-file keys ``setdefault``, so a caller that exported them still
wins. ``MISE_DATA_DIR`` deliberately does not — see below.
"""

import os
import tempfile

import pytest

os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")

# Never inherit an ambient MISE_DATA_DIR. _strip_showcase_seed below migrates and
# then DELETEs rows in whatever it points at, and the suite churns writes into it
# for the rest of the run — so a developer who exports MISE_DATA_DIR to run the
# app locally would lose real data by typing `pytest`. Overriding unconditionally
# keeps the documented `MISE_DATA_DIR=$(mktemp -d) pytest -m smoke` gate working
# (it asks for a throwaway dir and still gets one). MISE_TEST_DATA_DIR is the
# explicit opt-in for pointing the suite somewhere specific — a large volume, say,
# when /tmp is a small tmpfs.
os.environ["MISE_DATA_DIR"] = os.environ.get("MISE_TEST_DATA_DIR") or tempfile.mkdtemp(
    prefix="mise-test-"
)


@pytest.fixture(scope="session", autouse=True)
def _strip_showcase_seed():
    """Remove migration 058's retired prototype rows before any test runs.

    Migration 068 leaves them unpublished for production reversibility; tests
    remove them entirely so public-site assertions start from an empty showcase.
    """
    from app import db

    db.migrate()
    db.run("DELETE FROM testimonials")
    db.run("UPDATE assets SET portfolio=0, portfolio_tag=NULL")
    db.run(
        "UPDATE galleries SET cs_published=0, cs_tagline=NULL, cs_brief=NULL, "
        "cs_credits=NULL, cs_location=NULL"
    )
    yield


@pytest.fixture(autouse=True)
def _reset_attempt_buckets(request):
    """Clear the DB-backed lockout/throttle ledger before each test.

    ``pin_attempts`` carries both PIN-lockout rows and the public inquiry
    buckets (``security.INQUIRY_BUCKET_*``, 3 posts/hour/IP). The whole session
    shares one ``MISE_DATA_DIR`` DB and every TestClient request presents as
    ``ip=testclient``, so ``/contact`` posts accumulated across test FILES and a
    plain ``pytest tests/`` run 429'd where the marker-partitioned CI steps —
    three separate processes — did not. The existing autouse resets only clear
    the in-memory ``ratelimit._hits``, never these rows.

    This is test isolation only: the throttle itself is untouched, and the reset
    is the same ``DELETE FROM pin_attempts`` the per-test helpers already use.

    Not applied to tests/smoke: that suite is deliberately order-coupled on
    accumulated DB state (see tests/smoke/conftest.py) and keeps owning its own
    targeted cleanups.
    """
    if not request.node.get_closest_marker("smoke"):
        from app import db

        db.run("DELETE FROM pin_attempts")
    yield
