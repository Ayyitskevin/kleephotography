#!/usr/bin/env bash
# Nightly backup, two stages, both LOCAL to the prod host.
#
# Stage 1 — the database: consistent, SELF-VERIFIED SQLite snapshot. sqlite3
# .backup is WAL-safe. The snapshot is integrity-checked before it is kept, so a
# corrupt snapshot fails loud instead of silently replacing a good one.
#
# Stage 2 — the durable files: media/, brand/ and receipts/. The database is the
# smaller half of the estate; the client originals ARE the product, and until
# this stage existed nothing copied them anywhere. Snapshots are hardlinked
# against the previous night (rsync --link-dest), so a mostly-immutable media
# tree costs one copy plus each night's delta rather than N copies. That matters
# for more than disk: a plain mirror would REPLAY a wrong delete onto the only
# other copy, which is the failure this is meant to survive.
#
# Off-host DR is still pending — see ops/BACKUP.md.
set -euo pipefail
DATA="${MISE_DATA_DIR:-/opt/mise/data}"
OUT="$DATA/backups"
mkdir -p "$OUT"
STAMP=$(date +%F-%H%M)
TMP="$OUT/.mise-$STAMP.db.tmp"

# ── Stage 1: the database ──────────────────────────────────────────────────
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
# The archive is what actually gets restored, so verify the compressed artifact
# too — an integrity-checked database inside a truncated .gz restores nothing.
if ! gzip -t "$OUT/mise-$STAMP.db.gz" 2>/dev/null; then
  echo "BACKUP FAILED: gzip -t on fresh archive mise-$STAMP.db.gz" >&2
  rm -f "$OUT/mise-$STAMP.db.gz"
  exit 1
fi
find "$OUT" -name 'mise-*.db.gz' -mtime +14 -delete
echo "stage1 ok: mise-$STAMP.db.gz ($(du -h "$OUT/mise-$STAMP.db.gz" | cut -f1)) verified=ok"

# ── Stage 2: the durable files ─────────────────────────────────────────────
# Unset means this host has not been provisioned with a second location yet.
# Skip loudly rather than failing: stage 1 has already succeeded and a missing
# file destination must not cost the operator their database snapshot too.
FILE_DEST="${MISE_FILE_BACKUP_DIR:-}"
if [ -z "$FILE_DEST" ]; then
  echo "stage2 SKIPPED: MISE_FILE_BACKUP_DIR unset — media/, brand/ and receipts/ are NOT backed up (ops/BACKUP.md)" >&2
  exit 0
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "stage2 FAILED: rsync not installed" >&2
  exit 1
fi
mkdir -p "$FILE_DEST"

# A "second disk" on the same filesystem survives a wrong delete but not the
# disk dying. Say so every night rather than letting it read as full coverage.
if [ "$(stat -c %d "$DATA")" = "$(stat -c %d "$FILE_DEST")" ]; then
  echo "stage2 WARNING: $FILE_DEST shares a filesystem with $DATA — protects against a wrong delete, NOT a disk failure" >&2
fi

MIN_FREE_GB="${MISE_FILE_BACKUP_MIN_FREE_GB:-5}"
free_gb=$(df -BG --output=avail "$FILE_DEST" | tail -1 | tr -dc '0-9')
if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
  echo "stage2 FAILED: only ${free_gb}G free at $FILE_DEST, need ${MIN_FREE_GB}G" >&2
  exit 1
fi

SNAP_ROOT="$FILE_DEST/files"
LATEST="$SNAP_ROOT/latest"
TARGET="$SNAP_ROOT/$STAMP"
mkdir -p "$SNAP_ROOT"

# --link-dest wants a real directory, not the symlink that points at one.
link=()
if [ -d "$LATEST" ]; then
  link=(--link-dest "$(readlink -f "$LATEST")")
fi

# mise.db is stage 1's job — a raw copy of a live WAL database is not a valid
# backup and shipping one would invite a restore that silently loses writes.
# zips/ and tmp/ are rebuildable derivatives; backups/ is stage 1's own output.
rm -rf "$TARGET.partial"
rsync -a --delete "${link[@]}" \
  --exclude='mise.db' --exclude='mise.db-wal' --exclude='mise.db-shm' \
  --exclude='/backups/' --exclude='/zips/' --exclude='/tmp/' \
  "$DATA/" "$TARGET.partial/"
# A same-minute rerun (operator runs this by hand right after the timer) would
# otherwise nest the new snapshot inside the old one, because `mv a b` moves a
# INTO b when b exists. Replacing is right: same stamp means same night.
rm -rf "$TARGET"
mv "$TARGET.partial" "$TARGET"
ln -sfn "$TARGET" "$LATEST"

# Same 14-night window as the database. Snapshot names sort chronologically, so
# lexical order is chronological order; `latest` is a symlink and never matches.
find "$SNAP_ROOT" -maxdepth 1 -type d -name '20*' -printf '%f\n' \
  | sort | head -n -14 | while read -r old; do rm -rf "${SNAP_ROOT:?}/$old"; done

echo "stage2 ok: files/$STAMP ($(du -sh "$TARGET" | cut -f1), $(du -sh --exclude=latest "$SNAP_ROOT" | cut -f1) for all snapshots)"
