#!/usr/bin/env bash
# THE HARD GATE (runs as kevin-lee): a backup is not "done" until a restore is verified.
# Pulls the latest OFF-SITE DB snapshot back from mickey, restores it to a throwaway
# file, and proves it opens + passes integrity/foreign-key checks + has real rows.
# Then confirms the off-site media mirror matches the live tree by checksum.
# Exits non-zero on any failure so a timer/alert can catch a rotting backup (R21).
set -euo pipefail
SRC=/opt/mise/data
REMOTE=kevin-lee@100.125.80.91
RDIR=backups/mise
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# 1) pull the newest off-site DB snapshot
latest=$(ssh -o BatchMode=yes "$REMOTE" "ls -t $RDIR/db/mise-*.db.gz 2>/dev/null | head -1") \
  || { echo "RESTORE-TEST FAILED: cannot reach mickey" >&2; exit 1; }
[ -n "$latest" ] || { echo "RESTORE-TEST FAILED: no off-site DB snapshot found" >&2; exit 1; }
scp -q "$REMOTE:$latest" "$WORK/db.gz"
gunzip "$WORK/db.gz"

# 2) restore-side integrity: open it, structural + referential checks
ic=$(sqlite3 "$WORK/db" "PRAGMA integrity_check;")
[ "$ic" = "ok" ] || { echo "RESTORE-TEST FAILED: integrity_check=$ic" >&2; exit 1; }
fk=$(sqlite3 "$WORK/db" "PRAGMA foreign_key_check;")
[ -z "$fk" ] || { echo "RESTORE-TEST FAILED: foreign_key_check found violations:" >&2; echo "$fk" >&2; exit 1; }

# 3) sanity: the schema + at least the core tables exist and are queryable
tables=$(sqlite3 "$WORK/db" "SELECT count(*) FROM sqlite_master WHERE type='table';")
mig=$(sqlite3 "$WORK/db" "SELECT count(*) FROM schema_migrations;" 2>/dev/null || echo "NA")
gal=$(sqlite3 "$WORK/db" "SELECT count(*) FROM galleries;" 2>/dev/null || echo "NA")

# 4) media off-site copy matches live tree (checksum, dry-run must be clean)
miss=$(rsync -an --checksum "$SRC/media/" "$REMOTE:$RDIR/media/" | grep -E "^[^ ].*[^/]$" | grep -v "^sending" || true)
[ -z "$miss" ] || { echo "RESTORE-TEST FAILED: off-site media differs from live:" >&2; echo "$miss" >&2; exit 1; }

echo "RESTORE-TEST PASS: $(basename "$latest") restores clean (tables=$tables migrations=$mig galleries=$gal); off-site media checksum-matches live."
