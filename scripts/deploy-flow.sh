#!/usr/bin/env bash
# Deploy Mise app code to flow:/opt/mise (rsync + restart).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
FLOW_HOST="${MISE_FLOW_HOST:-flow}"
FLOW_ROOT="${MISE_FLOW_ROOT:-/opt/mise}"

echo "==> Rsync studio modules to ${FLOW_HOST}:${FLOW_ROOT}"
rsync -avz \
  "$SRC/app/plutus_recommend.py" \
  "$SRC/app/argus_analyze.py" \
  "${FLOW_HOST}:${FLOW_ROOT}/app/"
rsync -avz "$SRC/templates/admin/gallery.html" "${FLOW_HOST}:${FLOW_ROOT}/templates/admin/"

ssh -o ConnectTimeout=15 "$FLOW_HOST" bash -s <<REMOTE
set -euo pipefail
if systemctl is-active mise >/dev/null 2>&1; then
  sudo systemctl restart mise
elif systemctl --user is-active mise >/dev/null 2>&1; then
  systemctl --user restart mise
else
  echo "restart mise manually on flow"
fi
REMOTE

echo "==> flow Mise restarted — verify gallery admin Plutus tile"