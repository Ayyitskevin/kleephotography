"""Disposable loopback server for browser tests; never load operator data or workers."""

import argparse
import datetime as dt
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DAY = dt.date(2037, 4, 15)
PASSWORD = "test-pw"  # pragma: allowlist secret (synthetic fixture only)


def deny_connection(*args, **kwargs):
    raise RuntimeError("Browser fixture server must not make outbound connections")


def run(*, check=False):
    scratch = ROOT / ".browser-tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as data:
        # Do this before importing app/config, including for a developer with a live .env.
        for key in list(os.environ):
            if key.startswith("MISE_"):
                del os.environ[key]
        os.environ.update(
            MISE_ENV_FILE="/nonexistent",
            MISE_DATA_DIR=data,
            MISE_SECRET_KEY=secrets.token_hex(32),
            MISE_ADMIN_PASSWORD=PASSWORD,
            MISE_BASE_URL="http://127.0.0.1:8765",
            MISE_PORT="8765",
            MISE_TIMEZONE="America/New_York",
            MISE_RL_ADMIN="10000",
        )
        sys.path.insert(0, str(ROOT))
        # Browser routes are real; only the studio day and infrastructure are fixtures.
        from app import config, db
        from app.admin import studio
        from app.main import _require_secret_key, app

        studio._today = lambda: DAY
        _require_secret_key()
        assert config.DB_PATH.parent == Path(data)
        db.migrate()
        for viewport in ("desktop", "mobile"):
            for name in ("Café & Bar A", "Café & Bar B", "Stale Bistro"):
                business = f"{viewport} {name}"
                pid = db.run(
                    "INSERT INTO prospects (business,source,qualification,business_key,contact_key) "
                    "VALUES (?, 'Synthetic browser fixture', 'potential_fit', ?, '')",
                    (business, business.casefold()),
                )
                db.run(
                    "INSERT INTO tasks (title,due_date,prospect_id) VALUES (?, ?, ?)",
                    (f"Review {business}", DAY.isoformat(), pid),
                )

        with (
            patch.object(socket.socket, "connect", deny_connection),
            patch.object(socket.socket, "connect_ex", deny_connection),
        ):
            if check:
                from fastapi.testclient import TestClient

                # No context manager: application lifespan workers stay off, as in browser mode.
                with_client = TestClient(app)
                try:
                    assert with_client.get("/admin/login").status_code == 200
                    assert (
                        with_client.post("/admin/login", data={"password": PASSWORD}).status_code
                        == 200
                    )
                    page = with_client.get("/admin/studio/prospects/follow-ups")
                    assert page.status_code == 200 and "desktop Café &amp; Bar A" in page.text
                    assert db.one("SELECT COUNT(*) n FROM prospects")["n"] == 6
                    print("Browser fixtures and real login/queue routes passed; workers off.")
                finally:
                    with_client.close()
            else:
                import uvicorn

                # Migration/fixtures ran above. Disable jobs and scheduled provider work.
                uvicorn.run(app, host="127.0.0.1", port=8765, lifespan="off")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate fixtures without a socket")
    run(check=parser.parse_args().check)
