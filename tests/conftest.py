"""Test-environment defaults.

Pytest imports this before any test module — and therefore before ``app.config``,
which reads these at import time. Setting them here gives the unit suite a
self-contained, writable environment instead of depending on the CI workflow to
export them (the unit step did not, so app-dependent unit tests failed with
``MISE_SECRET_KEY is not set``).

We only ``setdefault``: any value the caller already exported (e.g. the smoke
step's explicit ``MISE_DATA_DIR=$(mktemp -d)``) still wins.
"""

import os
import tempfile

import pytest

os.environ.setdefault("MISE_SECRET_KEY", "test-secret")
os.environ.setdefault("MISE_ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("MISE_ENV_FILE", "/nonexistent")
os.environ.setdefault("MISE_DATA_DIR", tempfile.mkdtemp(prefix="mise-test-"))


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
