# Disaster recovery

`BACKUP.md` covers the failures that happen most — a bad migration, a wrong
delete, a dead disk. This file covers the one it cannot: **the host is gone.**
Fire, theft, a dead machine, a provider closing the account.

## The three copies

| Stage | Where | Survives | Runbook |
|---|---|---|---|
| 1 — database snapshot | `/opt/mise/data/backups` | wrong delete, bad migration | [`BACKUP.md`](BACKUP.md) |
| 2 — durable files | second local disk | wrong delete, single-disk failure | [`BACKUP.md`](BACKUP.md) |
| 3 — off-host, encrypted | `RESTIC_REPOSITORY` | **losing the host entirely** | this file |

Stage 3 runs nightly at 03:30 (`mise-offsite.timer`), an hour after the local
backup, so what it ships is a snapshot stage 1 has already integrity-checked.

**restic encrypts client-side.** The destination provider never holds readable
client photos, financial scans, or a database full of personal data — it stores
opaque blobs. That is what makes an off-host copy of other people's photographs
defensible in the first place.

## Arming it

Any restic backend works; nothing in the scripts is provider-specific.

```sh
# 1. A strong repository password, readable only by mise. Losing it loses the
#    backup — restic has no recovery path, by design.
sudo -u mise install -m 600 /dev/null /opt/mise/.restic-password
sudo -u mise sh -c 'openssl rand -base64 32 > /opt/mise/.restic-password'

# 2. Point at a repository. Pick one:
#    Backblaze B2:  b2:bucket-name:mise
#    S3-compatible: s3:s3.amazonaws.com/bucket-name
#    Another box:   sftp:user@host:/srv/mise-backup
# In /opt/mise/.env:
#   RESTIC_REPOSITORY=b2:kleephoto-backup:mise
#   RESTIC_PASSWORD_FILE=/opt/mise/.restic-password
#   B2_ACCOUNT_ID=...
#   B2_ACCOUNT_KEY=...
#   MISE_OFFSITE_PING_URL=https://<service>/<uuid>
#   MISE_RESTORE_VERIFY_PING_URL=https://<service>/<uuid>

# 3. Initialise once, then prove it end to end.
sudo -u mise restic init
sudo -u mise /opt/mise/ops/offsite-backup.sh
sudo -u mise /opt/mise/ops/restore-verify.sh

sudo systemctl daemon-reload
sudo systemctl enable --now mise-offsite.timer mise-restore-verify.timer
```

Until `RESTIC_REPOSITORY` is set both jobs **skip loudly** and say this host has
no off-host copy. Skipping is not passing.

## The credentials must live somewhere else

This is the step DR plans usually miss, and it is the one that turns a recoverable
outage into a permanent loss:

> If the repository password and the provider credentials exist **only** on the
> host, then losing the host loses the backup too. The encrypted copy is intact
> and permanently unreadable. There is no support ticket for this — restic cannot
> decrypt without the password, which is the property that makes it safe to store
> client photos with a third party.

So keep, **off this machine** — a password manager, a printed copy in a drawer,
anywhere that does not burn down with the box:

- the restic repository password,
- the provider credentials (B2 key, S3 key, or the SSH key for an SFTP target),
- the repository URL,
- a copy of `/opt/mise/.env` and the cloudflared credential (see
  [`TRUTHFUL-HTTPS.md`](TRUTHFUL-HTTPS.md)), so the host is rebuildable at all.

## Monthly restore-verify

`restic check` verifies the repository's structure. It does **not** prove the
data comes back, and the classic disaster-recovery failure is a chain that has
been reporting success for months and produces nothing usable when it is finally
needed. `restore-verify.sh` (1st of the month) restores into a temp directory and
asserts:

- the newest database archive decompresses, opens, and passes `integrity_check`;
- it has real content — an **empty** database passes `integrity_check` too, which
  is exactly what a misconfigured backup produces;
- a real client file comes back byte-identical, comparing only files whose mtime
  still matches the snapshot, so a legitimately re-uploaded file is not mistaken
  for corruption.

Failure exits non-zero and the systemd unit records it. Point
`MISE_RESTORE_VERIFY_PING_URL` at a dead-man's switch with a ~5-week period so a
verify that stops running announces itself ([`MONITORING.md`](MONITORING.md)).

## Rebuilding the host from nothing

```sh
# On the new machine, with the credentials retrieved from off-host storage:
export RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=...
restic snapshots --tag mise                  # pick a point in time
restic restore <id> --target /restore

# 1. Application tree from git; /opt/mise/.env from your off-host copy.
# 2. Media, brand and receipts back into place:
sudo -u mise rsync -a /restore/opt/mise/data/media/    /opt/mise/data/media/
sudo -u mise rsync -a /restore/opt/mise/data/brand/    /opt/mise/data/brand/
sudo -u mise rsync -a /restore/opt/mise/data/receipts/ /opt/mise/data/receipts/
# 3. Database — the newest verified snapshot, NOT a live copy (there isn't one):
gunzip -c /restore/opt/mise/data/backups/mise-<stamp>.db.gz > /tmp/mise.db
sqlite3 /tmp/mise.db 'PRAGMA integrity_check;'      # must print ok
sudo -u mise cp /tmp/mise.db /opt/mise/data/mise.db
# 4. Start, then re-point DNS / the tunnel at the new host.
```

Restore the **files and the database from the same snapshot**. A database newer
than the media references originals that are not there; older, and recent
uploads exist on disk with no rows pointing at them.

### What a restore does not bring back

- **Anything after the snapshot.** Up to ~24h of inquiries, bookings and uploads.
- **Stripe state.** Payments captured after the snapshot are real at Stripe and
  absent from the restored database, and Stripe will not redeliver those webhooks
  on request — reconcile from the dashboard (see
  [`MIGRATIONS.md`](MIGRATIONS.md), "Rolling back a money migration").
- **The host itself.** DNS, the Cloudflare tunnel, TLS and systemd units are
  rebuilt from `DEPLOY.md` and `TRUTHFUL-HTTPS.md`, not from this backup.

## Retention

14 daily, 8 weekly, 12 monthly (`MISE_OFFSITE_KEEP_*`). The monthlies are the
point: a file corrupted or wrongly deleted in March and noticed in June is
recoverable only if something older than the daily window survived.
