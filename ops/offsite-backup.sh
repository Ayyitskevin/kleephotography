#!/usr/bin/env bash
# Stage 3 — the copy that survives losing the host.
#
# Stages 1 and 2 (ops/backup.sh) are both LOCAL. They protect against the
# failures that actually happen most — a bad migration, a wrong delete, a dead
# disk — and none of them survive fire, theft, or the box simply being gone.
# This pushes an encrypted copy somewhere else.
#
# restic, deliberately: it encrypts CLIENT-SIDE, so the destination provider
# never holds readable client photos, financial scans, or a database full of
# personal data. It also dedups and snapshots, so a mostly-immutable media tree
# costs one upload plus each run's delta.
#
# The backend is whatever RESTIC_REPOSITORY names — B2, S3, SFTP to another
# machine, or a mounted disk. Nothing here is provider-specific. See ops/DR.md.
set -euo pipefail
DATA="${MISE_DATA_DIR:-/opt/mise/data}"

ping_deadman() {
  [ -n "${MISE_OFFSITE_PING_URL:-}" ] || return 0
  curl -fsS --max-time 10 --retry 3 --retry-delay 2 -o /dev/null \
    "$MISE_OFFSITE_PING_URL" \
    || echo "ping FAILED (the offsite backup itself succeeded): $MISE_OFFSITE_PING_URL" >&2
}

if [ -z "${RESTIC_REPOSITORY:-}" ]; then
  echo "offsite SKIPPED: RESTIC_REPOSITORY unset — this host has NO off-host copy (ops/DR.md)" >&2
  exit 0
fi
if [ -z "${RESTIC_PASSWORD_FILE:-}" ] && [ -z "${RESTIC_PASSWORD:-}" ]; then
  echo "offsite FAILED: no RESTIC_PASSWORD_FILE — refusing to guess a repository password" >&2
  exit 1
fi
command -v restic >/dev/null 2>&1 || { echo "offsite FAILED: restic not installed" >&2; exit 1; }

# The live database is excluded on purpose: a raw copy of an open WAL database
# is not a valid backup. What ships instead is backups/, which stage 1 already
# wrote and integrity-checked — so the off-host copy inherits that verification
# rather than inventing its own. zips/ and tmp/ are rebuildable derivatives.
restic backup \
  --exclude "$DATA/mise.db" \
  --exclude "$DATA/mise.db-wal" \
  --exclude "$DATA/mise.db-shm" \
  --exclude "$DATA/zips" \
  --exclude "$DATA/tmp" \
  --tag mise \
  "$DATA"

# Keep enough history to outlive a problem noticed late — a corrupted file or a
# wrong delete discovered months later is exactly what daily-only retention
# loses. Prune reclaims the space the forgotten snapshots held.
restic forget \
  --tag mise \
  --keep-daily "${MISE_OFFSITE_KEEP_DAILY:-14}" \
  --keep-weekly "${MISE_OFFSITE_KEEP_WEEKLY:-8}" \
  --keep-monthly "${MISE_OFFSITE_KEEP_MONTHLY:-12}" \
  --prune

# Structural check every run. Cheap, and it catches repository damage while
# there is still a local copy to re-upload from — restore-verify.sh is the
# heavier proof that the data actually comes back.
restic check

echo "offsite ok: $(restic snapshots --tag mise --json | grep -o '"short_id"' | wc -l) snapshots in $RESTIC_REPOSITORY"
ping_deadman
