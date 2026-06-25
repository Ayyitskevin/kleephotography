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
# The demo-showcase backfill (bootstrap.ensure_public_showcase, gated by this flag,
# + migration 058's one-time SQL seed) exists so a fresh prototype site isn't blank.
# In tests it would pollute the empty-baseline assertions in the public-site suite,
# so we turn the startup seed off here and wipe migration 058's rows once below —
# letting those tests exercise the real empty→populated path on a pristine DB.
os.environ.setdefault("MISE_SHOWCASE_SEED", "false")


@pytest.fixture(scope="session", autouse=True)
def _strip_showcase_seed():
    """Remove migration 058's one-time public-showcase seed before any test runs.

    058 runs unconditionally inside db.migrate(); with MISE_SHOWCASE_SEED off the
    startup backfill won't re-add these rows, so a single wipe here sticks for the
    whole session and the public-site tests start from a genuinely empty showcase.
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
