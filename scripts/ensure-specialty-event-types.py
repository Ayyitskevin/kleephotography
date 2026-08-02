#!/usr/bin/env python3
"""Ensure re-shoot / pl-session / fb-shoot booking event types exist.

Green-light tooling — run on the production host (as the mise user) after pull:

  cd /opt/mise && sudo -u mise .venv/bin/python scripts/ensure-specialty-event-types.py

Idempotent. Does not touch money, schema migrations, or contracts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

# Matches ops/SPECIALTY-LAUNCH.md §2 starting points.
SPECS = (
    {
        "slug": "re-shoot",
        "name": "Real Estate Shoot",
        "description": "MLS-ready stills, twilight exteriors, and walkthrough film for a listing.",
        "duration_min": 120,
        "buffer_before_min": 30,
        "buffer_after_min": 30,
        "min_notice_hours": 24,
        "booking_window_days": 45,
        "creates_notion_session": 1,
        "position": 10,
        # Mon–Fri daytime + evening twilight block (minutes from midnight).
        "windows": [(wd, 540, 1020) for wd in range(5)] + [(wd, 1020, 1200) for wd in range(5)],
    },
    {
        "slug": "pl-session",
        "name": "Portrait Session",
        "description": "Directed headshots, personal branding, or family sessions.",
        "duration_min": 90,
        "buffer_before_min": 15,
        "buffer_after_min": 30,
        "min_notice_hours": 24,
        "booking_window_days": 60,
        "creates_notion_session": 1,
        "position": 20,
        "windows": [(wd, 540, 1020) for wd in range(5)] + [(wd, 1020, 1200) for wd in range(5)],
    },
    {
        "slug": "fb-shoot",
        "name": "F&B Shoot",
        "description": "Menus, pours, and the rooms they live in — photo and motion.",
        "duration_min": 240,
        "buffer_before_min": 30,
        "buffer_after_min": 30,
        "min_notice_hours": 48,
        "booking_window_days": 60,
        "creates_notion_session": 1,
        "position": 30,
        "windows": [(wd, 540, 1020) for wd in range(5)],
    },
)


def _ensure(spec: dict) -> str:
    row = db.one("SELECT id, active FROM event_types WHERE slug=?", (spec["slug"],))
    if row:
        db.run(
            """UPDATE event_types SET name=?, description=?, duration_min=?,
                  buffer_before_min=?, buffer_after_min=?, min_notice_hours=?,
                  booking_window_days=?, creates_notion_session=?, active=1,
                  position=? WHERE id=?""",
            (
                spec["name"],
                spec["description"],
                spec["duration_min"],
                spec["buffer_before_min"],
                spec["buffer_after_min"],
                spec["min_notice_hours"],
                spec["booking_window_days"],
                spec["creates_notion_session"],
                spec["position"],
                row["id"],
            ),
        )
        eid = row["id"]
        action = "updated"
    else:
        eid = db.run(
            """INSERT INTO event_types (
                  slug, name, description, duration_min, buffer_before_min,
                  buffer_after_min, min_notice_hours, booking_window_days,
                  creates_notion_session, active, position
                ) VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
            (
                spec["slug"],
                spec["name"],
                spec["description"],
                spec["duration_min"],
                spec["buffer_before_min"],
                spec["buffer_after_min"],
                spec["min_notice_hours"],
                spec["booking_window_days"],
                spec["creates_notion_session"],
                spec["position"],
            ),
        )
        action = "created"

    for weekday, start_min, end_min in spec["windows"]:
        exists = db.one(
            """SELECT 1 AS x FROM availability_rules
               WHERE event_type_id=? AND weekday=? AND start_min=? AND end_min=?""",
            (eid, weekday, start_min, end_min),
        )
        if not exists:
            db.run(
                """INSERT INTO availability_rules (event_type_id, weekday, start_min, end_min)
                   VALUES (?,?,?,?)""",
                (eid, weekday, start_min, end_min),
            )
    return f"{action} {spec['slug']} (id={eid})"


def main() -> int:
    db.migrate()
    for spec in SPECS:
        print(_ensure(spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
