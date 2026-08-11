#!/usr/bin/env bash
# Specialty rsync slice → prod /opt/mise (restart). Legacy filename; it deploys to
# whatever host MISE_FLOW_HOST names. Prefer ops/DEPLOY.md (git pull) for ordinary
# deploys. Set MISE_FLOW_HOST (required, no default so a typo can't ship to the
# wrong box) and optionally MISE_FLOW_ROOT.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
FLOW_HOST="${MISE_FLOW_HOST:?set MISE_FLOW_HOST to the prod SSH host}"
FLOW_ROOT="${MISE_FLOW_ROOT:-/opt/mise}"

# ── Ship only what has been reviewed ────────────────────────────────────────
# rsync overwrites files on the live box with whatever is in THIS clone, so a
# stale branch, a half-finished experiment, or a forgotten local edit lands in
# production with no review, no PR, and no record of what changed. Refuse unless
# the tree is clean and HEAD is already merged into origin/main.
#
# MISE_FLOW_ALLOW_UNMERGED=yes overrides it. That exists because this script is
# the incident tool — a guard with no way past it during an outage is its own
# kind of hazard — but it announces itself so it cannot be the habit.
if [ "${MISE_FLOW_ALLOW_UNMERGED:-}" = "yes" ]; then
  echo "!! MISE_FLOW_ALLOW_UNMERGED=yes — shipping an UNREVIEWED tree to ${FLOW_HOST}" >&2
else
  if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
    echo "refusing: working tree is not clean — these would ship as-is:" >&2
    git -C "$SRC" status --short >&2
    echo "  Commit and merge them, or set MISE_FLOW_ALLOW_UNMERGED=yes for an incident." >&2
    exit 1
  fi
  if ! git -C "$SRC" fetch -q origin main; then
    echo "refusing: could not fetch origin/main, so 'is this merged?' cannot be answered" >&2
    exit 1
  fi
  if ! git -C "$SRC" merge-base --is-ancestor HEAD origin/main; then
    echo "refusing: HEAD ($(git -C "$SRC" rev-parse --short HEAD)) is not merged into origin/main." >&2
    echo "  Merge it first, or set MISE_FLOW_ALLOW_UNMERGED=yes for an incident." >&2
    exit 1
  fi
fi

echo "==> Rsync studio modules to ${FLOW_HOST}:${FLOW_ROOT}"
rsync -avz \
  "$SRC/app/plutus_recommend.py" \
  "$SRC/app/argus_analyze.py" \
  "$SRC/app/platekit.py" \
  "${FLOW_HOST}:${FLOW_ROOT}/app/"
rsync -avz "$SRC/templates/admin/gallery.html" "${FLOW_HOST}:${FLOW_ROOT}/templates/admin/"
rsync -avz "$SRC/static/mise.css" "${FLOW_HOST}:${FLOW_ROOT}/static/"

# migrations/ is deliberately NOT shipped here.
#
# The restart below runs db.migrate(), so any .sql landing in that directory is
# applied to the LIVE database immediately — irreversibly, and recorded in
# schema_migrations. There are two hazards and the guard above only fixes one:
#
#   1. A stale clone applies an un-merged, un-reviewed schema change.
#   2. Even from a clean, fully merged tree, this script ships three Python
#      files — so the SCHEMA moves without the code that expects it. Migration
#      071 renames admin_sessions.token to token_hash; applied this way it would
#      break admin login instantly on a box still running the old security.py.
#
# (2) cannot be guarded away, because the decoupling is what this script IS.
# Schema changes reach production through git pull + restart (ops/DEPLOY.md),
# where code and schema move together.

ssh -o ConnectTimeout=15 "$FLOW_HOST" bash -s <<REMOTE
set -euo pipefail
if systemctl is-active mise >/dev/null 2>&1; then
  sudo systemctl restart mise
elif systemctl --user is-active mise >/dev/null 2>&1; then
  systemctl --user restart mise
else
  echo "restart mise manually on ${FLOW_HOST}"
fi
REMOTE

echo "==> ${FLOW_HOST} Mise restarted — verify gallery admin Plutus tile"