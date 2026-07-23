#!/usr/bin/env bash
# Visual-baseline screenshots for UI work: boot a throwaway Mise (mktemp
# MISE_DATA_DIR, 127.0.0.1:8499 — never prod :8400), wait for it, then shoot
# the page manifest at desktop (1440x900) + mobile (390x844) widths via
# scripts/ui-shots.mjs. Server is killed and the data dir removed on exit.
# MISE_ENV_FILE=/dev/null keeps the throwaway process from setdefault-ing
# prod keys out of /opt/mise/.env (app/config.py loads it when present).
# Usage: scripts/ui-shots.sh [out-dir]        (default /tmp/mise-shots/before)
# Env:   UI_SHOTS_PORT     (default 8499)  UI_SHOTS_NPM_DIR (default /tmp/ui-shots-npm)
#        MISE_ADMIN_PASSWORD (default pw)  GALLERY_SLUG (optional, also shoot /g/<slug>)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-/tmp/mise-shots/before}"
PORT="${UI_SHOTS_PORT:-8499}"
NPM_DIR="${UI_SHOTS_NPM_DIR:-/tmp/ui-shots-npm}"
BASE="http://127.0.0.1:${PORT}"
ADMIN_PASSWORD="${MISE_ADMIN_PASSWORD:-pw}"

if [[ "$PORT" == "8400" ]]; then
  echo "refusing to boot on prod port 8400" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
DATA_DIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

echo "==> boot throwaway mise on ${BASE} (data ${DATA_DIR})"
cd "$ROOT"
source .venv/bin/activate
MISE_ENV_FILE=/dev/null MISE_DATA_DIR="$DATA_DIR" MISE_SECRET_KEY=test \
  MISE_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  >"$OUT_DIR/server.log" 2>&1 &
SERVER_PID=$!

echo "==> wait for ${BASE}/ (30s)"
for _ in $(seq 1 60); do
  curl -sf -o /dev/null "$BASE/" && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server died on boot — last log lines:" >&2
    tail -20 "$OUT_DIR/server.log" >&2
    exit 1
  fi
  sleep 0.5
done
if ! curl -sf -o /dev/null "$BASE/"; then
  echo "server not answering after 30s — last log lines:" >&2
  tail -20 "$OUT_DIR/server.log" >&2
  exit 1
fi

echo "==> playwright env (${NPM_DIR})"
if [[ ! -d "$NPM_DIR/node_modules/playwright" ]]; then
  mkdir -p "$NPM_DIR"
  (cd "$NPM_DIR" && npm init -y >/dev/null && npm i playwright@1.61.1)
fi
# Browsers live in ~/.cache/ms-playwright (chromium-1228 matches 1.61.x);
# install only if the expected binary is missing.
if ! (cd "$NPM_DIR" && node -e "const fs=require('fs');process.exit(fs.existsSync(require('playwright').chromium.executablePath())?0:1)"); then
  (cd "$NPM_DIR" && npx playwright install chromium)
fi

echo "==> shoot manifest -> ${OUT_DIR}"
(cd "$NPM_DIR" && BASE_URL="$BASE" OUT_DIR="$OUT_DIR" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  UI_SHOTS_NPM_DIR="$NPM_DIR" node "$ROOT/scripts/ui-shots.mjs")

echo "==> done (${OUT_DIR})"
