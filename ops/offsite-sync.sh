#!/usr/bin/env bash
# Stage 2 (runs as kevin-lee, who already has keyed ssh to mickey): push the latest
# verified DB snapshot AND the irreplaceable client media off-machine to mickey.
# This is the actual disk-failure protection — stage 1 alone shares a disk with the
# live data. Fails LOUD if the remote is unreachable (a silent backup is not a backup).
#
# Off-site layout on mickey:  ~/backups/mise/{db,media,brand}/
# Media/brand sync is append-only (NO --delete): asset files are immutable and keyed
# by id, so the off-site copy is a safety net that retains delivered work even if it
# is later cleaned up locally. DB keeps a rolling 30-day set of dated snapshots.
set -euo pipefail
SRC=/opt/mise/data
REMOTE=kevin-lee@100.125.80.91
RDIR=backups/mise

ssh -o BatchMode=yes "$REMOTE" "mkdir -p $RDIR/db $RDIR/media $RDIR/brand" \
  || { echo "OFFSITE FAILED: mickey unreachable" >&2; exit 1; }

latest_db=$(ls -t "$SRC"/backups/mise-*.db.gz 2>/dev/null | head -1)
[ -n "$latest_db" ] || { echo "OFFSITE FAILED: no local DB snapshot to push (run backup.sh first)" >&2; exit 1; }
rsync -a "$latest_db" "$REMOTE:$RDIR/db/"

rsync -a "$SRC/media/"  "$REMOTE:$RDIR/media/"
rsync -a "$SRC/brand/"  "$REMOTE:$RDIR/brand/"

pending=$(rsync -an --checksum "$SRC/media/" "$REMOTE:$RDIR/media/" | grep -E "^[^ ].*[^/]$" | grep -v "^sending" || true)
if [ -n "$pending" ]; then
  echo "OFFSITE WARNING: media checksum mismatch after sync:" >&2
  echo "$pending" >&2
  exit 1
fi

# off-site DB retention: keep 30 days of dated snapshots
ssh -o BatchMode=yes "$REMOTE" "find $RDIR/db -name 'mise-*.db.gz' -mtime +30 -delete"
echo "stage2 ok: db=$(basename "$latest_db") media+brand mirrored to $REMOTE:$RDIR (checksum-verified, 30d retention)"
