#!/usr/bin/env bash
# Relabel flow showcase gallery 1 for the public site (no DB shell access).
set -euo pipefail
HOST="${MISE_HOST:-flow}"
BASE="http://127.0.0.1:8400"
GALLERY_ID=1

run_on_flow() {
  ssh "$HOST" "cd /opt/mise && $*"
}

PASS="$(run_on_flow 'grep -m1 "^MISE_ADMIN_PASSWORD=" .env | cut -d= -f2-')"

echo "==> admin login"
CODE="$(run_on_flow "curl -sS -c /tmp/mise-real-cookie -b /tmp/mise-real-cookie -o /dev/null -w '%{http_code}' -X POST '$BASE/admin/login' -d 'password=${PASS}'")"
if [[ "$CODE" != "303" && "$CODE" != "302" ]]; then
  echo "login failed (HTTP $CODE)" >&2
  exit 1
fi

echo "==> relabel gallery $GALLERY_ID (Cúrate case study)"
run_on_flow "curl -sS -c /tmp/mise-real-cookie -b /tmp/mise-real-cookie -o /dev/null -X POST ${BASE}/admin/galleries/${GALLERY_ID}/settings \
  --data-urlencode title='Seasonal Tasting Menu' \
  --data-urlencode client_name='Cúrate' \
  --data-urlencode published=on \
  --data-urlencode cs_published=on \
  --data-urlencode cs_tagline='A tasting menu, shot at its peak.' \
  --data-urlencode cs_brief='A full menu refresh and brand library in a single service window — plating, pours, and the dining room, delivered as a same-week gallery with social crops baked in.' \
  --data-urlencode cs_credits='Client: Cúrate
Scope: Menu refresh · brand library
Deliverables: 6 finals · social crop pack
Turnaround: Same-week gallery' \
  --data-urlencode cs_location='Asheville, NC'"

run_on_flow "rm -f /tmp/mise-real-cookie"

echo "==> verify public site"
DEMO_COUNT="$(curl -sS 'https://kleephotography.com/' | grep -c 'Mise Demo' || true)"
CURATE_COUNT="$(curl -sS 'https://kleephotography.com/' | grep -c 'Cúrate' || true)"
echo "  kleephotography.com: Mise Demo=$DEMO_COUNT Cúrate=$CURATE_COUNT"
if [[ "$DEMO_COUNT" -gt 0 ]]; then
  echo "  WARN: still showing Mise Demo — restart mise on flow if needed" >&2
fi

echo "==> done"