#!/usr/bin/env bash
# Specialty rsync slice → prod /opt/mise (restart). Legacy filename; it deploys to
# whatever host MISE_FLOW_HOST names. Prefer ops/DEPLOY.md (git pull) for ordinary
# deploys. Set MISE_FLOW_HOST (required, no default so a typo can't ship to the
# wrong box) and optionally MISE_FLOW_ROOT.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
FLOW_HOST="${MISE_FLOW_HOST:?set MISE_FLOW_HOST to the prod SSH host}"
FLOW_ROOT="${MISE_FLOW_ROOT:-/opt/mise}"

echo "==> Rsync studio modules to ${FLOW_HOST}:${FLOW_ROOT}"
rsync -avz \
  "$SRC/app/plutus_recommend.py" \
  "$SRC/app/argus_analyze.py" \
  "$SRC/app/platekit.py" \
  "${FLOW_HOST}:${FLOW_ROOT}/app/"
rsync -avz "$SRC/templates/admin/gallery.html" "${FLOW_HOST}:${FLOW_ROOT}/templates/admin/"
rsync -avz "$SRC/static/mise.css" "${FLOW_HOST}:${FLOW_ROOT}/static/"
rsync -avz "$SRC/migrations/" "${FLOW_HOST}:${FLOW_ROOT}/migrations/"

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