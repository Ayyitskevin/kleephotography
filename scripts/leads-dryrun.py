#!/usr/bin/env python3
"""Dry-run the lead-intake wire end-to-end with ZERO live writes.

Simulates a kleephotography.com contact submission through the REAL app code
path (POST /contact → inquiries row → notion_sync_inquiry job) against a
throwaway temp database, with no .env, mailer unconfigured, and the two Notion
network functions stubbed to raise — nothing can reach api.notion.com even by
accident. Then prints: the stored inquiries row, the enqueued job, and the
exact Notion payload sync_inquiry would send once armed.

Run from the repo root:  python scripts/leads-dryrun.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["MISE_DATA_DIR"] = tempfile.mkdtemp(prefix="mise-dryrun-")
os.environ["MISE_SECRET_KEY"] = "dry-run"
os.environ["MISE_ADMIN_PASSWORD"] = "dry-run"
os.environ["MISE_ENV_FILE"] = "/nonexistent"
# Notion deliberately UNCONFIGURED: the enqueued job must take the dormant
# (skip + log) path, proving a live deploy without MISE_NOTION_LEADS_DB set
# changes nothing about today's behavior.

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, notion_sync  # noqa: E402
from app.main import app  # noqa: E402


def _no_network(*_a, **_k):
    raise RuntimeError("dry run: Notion network calls are disabled")


notion_sync._create_page = _no_network
notion_sync._patch_page = _no_network

SUBMISSION = {
    "name": "Dry Run Lead",
    "email": "dryrun@example.com",
    "business": "Example Bistro",
    "message": "Hi Kevin — we need menu photos for our relaunch.",
    "service": "Food & Beverage",
    "shoot_date": "2026-08-01",
    "budget": "$1-2k",
}

print("=" * 72)
print(f"DRY RUN — simulated submission (temp db: {os.environ['MISE_DATA_DIR']})")
print("=" * 72)

with TestClient(app) as client:
    r = client.post("/contact", data=SUBMISSION)
    print(f"\n1. POST /contact → HTTP {r.status_code}")

    inq = db.one("SELECT * FROM inquiries WHERE email=?", (SUBMISSION["email"],))
    print("\n2. inquiries row Mise stored:")
    print(json.dumps(dict(inq), indent=2))

    job = db.one("SELECT id, kind, payload, status FROM jobs WHERE kind='notion_sync_inquiry'")
    print("\n3. job enqueued (dormant no-op today — MISE_NOTION_LEADS_DB unset):")
    print(json.dumps(dict(job), indent=2))

    # Show what WOULD go to Notion once armed. dry_run=True builds the payload
    # with zero network and zero db writes; placeholder ids only affect display.
    config.NOTION_TOKEN = "<MISE_NOTION_TOKEN>"
    config.NOTION_LEADS_DB = "<MISE_NOTION_LEADS_DB>"
    plan = notion_sync.sync_inquiry(inq["id"], dry_run=True)
    print("\n4. exact Notion write once armed (sync_inquiry dry-run plan):")
    print(json.dumps(plan, indent=2))

    row_after = db.one("SELECT notion_page_id FROM inquiries WHERE id=?", (inq["id"],))
    print(
        f"\n5. zero-write proof: notion_page_id after dry run = "
        f"{row_after['notion_page_id']!r} (unstamped)"
    )

print("\nDone. Temp data dir can be deleted; nothing outside it was touched.")
