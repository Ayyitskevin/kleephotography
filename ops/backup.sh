#!/usr/bin/env bash
# Stage 1 (runs as the mise user, nightly): consistent, SELF-VERIFIED SQLite snapshot.
# sqlite3 .backup is WAL-safe. The snapshot is integrity-checked before it is kept,
# so a corrupt snapshot fails loud instead of silently replacing a good one.
# This stage is LOCAL ONLY — off-machine durability is stage 2 (offsite-sync.sh).
set -euo pipefail
DATA=/opt/mise/data
OUT="$DATA/backups"
mkdir -p "$OUT"
STAMP=$(date +%F-%H%M)
TMP="$OUT/.mise-$STAMP.db.tmp"

sqlite3 "$DATA/mise.db" ".backup '$TMP'"

# HARD CHECK: a snapshot we cannot verify is not a backup (R21).
res=$(sqlite3 "$TMP" "PRAGMA integrity_check;")
if [ "$res" != "ok" ]; then
  echo "BACKUP FAILED: integrity_check on fresh snapshot = $res" >&2
  rm -f "$TMP"
  exit 1
fi

mv "$TMP" "$OUT/mise-$STAMP.db"
gzip -f "$OUT/mise-$STAMP.db"
find "$OUT" -name 'mise-*.db.gz' -mtime +14 -delete
echo "stage1 ok: mise-$STAMP.db.gz ($(du -h "$OUT/mise-$STAMP.db.gz" | cut -f1)) verified=ok"
