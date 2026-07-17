#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' \
  "RETIRED: seed-real-showcase-flow.sh no longer publishes public showcase content." \
  "The former workflow could publish prototype proof without verified provenance." \
  "Use the live admin only after confirming ownership, release, attribution, and results." \
  "Follow ops/TRUTHFUL-HTTPS.md for the human-gated inventory and rollout." >&2
exit 1
