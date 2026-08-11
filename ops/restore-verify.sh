#!/usr/bin/env bash
# Prove the off-host backup restores. Monthly.
#
# `restic check` verifies the repository's structure; it does not prove the data
# comes back. The failure this catches is the one that matters: a backup chain
# that has been running "successfully" for months and produces nothing usable
# when you finally need it. An unverified backup is a belief, not a control.
#
# So this actually restores — into a temp directory, never over live data — and
# then asserts two things the operator would otherwise only discover mid-crisis:
# the restored database opens and passes integrity_check, and a real client file
# comes back byte-identical.
set -euo pipefail
DATA="${MISE_DATA_DIR:-/opt/mise/data}"

if [ -z "${RESTIC_REPOSITORY:-}" ]; then
  echo "restore-verify SKIPPED: RESTIC_REPOSITORY unset (ops/DR.md)" >&2
  exit 0
fi
command -v restic >/dev/null 2>&1 || { echo "restore-verify FAILED: restic not installed" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "restore-verify FAILED: sqlite3 not installed" >&2; exit 1; }

WORK="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK now, not at trap time
trap "rm -rf '$WORK'" EXIT

fail() { echo "restore-verify FAILED: $*" >&2; exit 1; }

restic restore latest --tag mise --target "$WORK" >/dev/null \
  || fail "restic restore did not complete"

# ── The database comes back and is sound ────────────────────────────────────
newest_gz="$(find "$WORK" -name 'mise-*.db.gz' -print0 | xargs -0 ls -1t 2>/dev/null | head -1 || true)"
[ -n "$newest_gz" ] || fail "no database snapshot in the restored tree"
gzip -t "$newest_gz" || fail "restored archive is corrupt: $newest_gz"
gunzip -c "$newest_gz" > "$WORK/verify.db" || fail "could not decompress $newest_gz"
res="$(sqlite3 "$WORK/verify.db" 'PRAGMA integrity_check;')"
[ "$res" = "ok" ] || fail "restored database integrity_check = $res"
# A structurally valid but EMPTY database passes integrity_check, and that is
# exactly what a misconfigured backup produces — so assert it has real content.
tables="$(sqlite3 "$WORK/verify.db" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")"
[ "${tables:-0}" -ge 10 ] || fail "restored database has only $tables tables — near-empty, not a real backup"

# ── A real client file comes back byte-identical ────────────────────────────
# Sampling one file and comparing hashes is what distinguishes "files were
# uploaded" from "files can be recovered".
#
# Only files whose live mtime still MATCHES the restored copy are compared. A
# file legitimately re-uploaded since the backup differs for an innocent reason,
# and failing on that would make a monthly job cry wolf — which is how a real
# alarm gets ignored later. Equal mtime with unequal bytes has no innocent
# explanation: that is corruption on one side or the other, and it fails loud.
sampled=""
while IFS= read -r sample; do
  [ -n "$sample" ] || continue
  rel="${sample#"$DATA"/}"
  restored="$(find "$WORK" -path "*/$rel" -type f 2>/dev/null | head -1 || true)"
  [ -n "$restored" ] || fail "media file missing from the restore: $rel"
  if [ "$(stat -c %Y "$sample")" != "$(stat -c %Y "$restored")" ]; then
    continue  # changed since the snapshot — not a fair comparison
  fi
  a="$(sha256sum "$sample" | cut -d' ' -f1)"
  b="$(sha256sum "$restored" | cut -d' ' -f1)"
  [ "$a" = "$b" ] || fail "restored media differs from live at identical mtime: $rel"
  sampled="$rel"
  break
done <<EOF
$(find "$DATA/media" -type f -size +0 2>/dev/null | head -25)
EOF

if [ -n "$sampled" ]; then
  echo "restore-verify: media sample OK ($sampled)"
elif [ -d "$DATA/media" ] && [ -n "$(find "$DATA/media" -type f -size +0 2>/dev/null | head -1)" ]; then
  # Every candidate was newer than the snapshot. Not a failure, but not a proof
  # either — say so rather than reporting a verification that did not happen.
  echo "restore-verify: media present but all sampled files postdate the snapshot — not verified" >&2
else
  echo "restore-verify: no media on this host to sample" >&2
fi

echo "restore-verify ok: db integrity=ok, $tables tables, restored from $RESTIC_REPOSITORY"
[ -n "${MISE_RESTORE_VERIFY_PING_URL:-}" ] && curl -fsS --max-time 10 --retry 3 -o /dev/null \
  "$MISE_RESTORE_VERIFY_PING_URL" || true
