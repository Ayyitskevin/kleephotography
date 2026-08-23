#!/usr/bin/env python3
"""Fail if two migrations share an ``NNN_`` prefix.

Green-light tooling — CI runs it on every push; run it the same way locally:

  python scripts/check-migration-numbering.py [migrations-dir]

Apply order is lexicographic filename order (``app/db.py``), so a shared prefix
still applies deterministically — the cost is elsewhere. Two branches each taking
"the next number" is exactly how 054 and 055 collided, and ``MIGRATION_ALIASES``
is the scar that collision left. ``ops/MIGRATIONS.md`` rule 2 forbids renumbering
anything production already recorded, so the two historical pairs are pinned
below by full filename rather than by prefix: a *third* file landing on 054 is
still a failure, and so is a new pair anywhere else.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ops/MIGRATIONS.md, "Known duplicate prefixes (do not 'fix' by renaming)".
KNOWN_DUPLICATES = {
    "054": {"054_argus_vision.sql", "054_gallery_reminders.sql"},
    "055": {"055_contract_unsigned_nudge.sql", "055_plutus_upsell.sql"},
}

PREFIX_RE = re.compile(r"^(\d{3})_")


def duplicates(migrations_dir: Path) -> list[str]:
    """Return one message per prefix carried by more than one unexpected file."""
    by_prefix: dict[str, set[str]] = defaultdict(set)
    for path in sorted(migrations_dir.glob("*.sql")):
        m = PREFIX_RE.match(path.name)
        if m:
            by_prefix[m.group(1)].add(path.name)

    return [
        f"{prefix}_ is shared by {len(names)} files: {', '.join(sorted(names))}"
        for prefix, names in sorted(by_prefix.items())
        if len(names) > 1 and names != KNOWN_DUPLICATES.get(prefix)
    ]


def main(argv: list[str]) -> int:
    migrations_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "migrations"
    found = duplicates(migrations_dir)
    if found:
        print(f"Duplicate migration prefixes in {migrations_dir}:", file=sys.stderr)
        for problem in found:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nEach migration takes the next unused NNN_ prefix (ops/MIGRATIONS.md).\n"
            "Renumber the NEW file — never one production has already recorded.",
            file=sys.stderr,
        )
        return 1
    print(f"migration numbering OK — {len(list(migrations_dir.glob('*.sql')))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
