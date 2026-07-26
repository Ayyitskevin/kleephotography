# Mise backup & restore — deployed topology

**Production host is mickey** (`/opt/mise`). As of **2026-07-25**, Mise was moved
off flow onto mickey (always-on). The old flow→mickey pull topology is retired.

## What runs today

| When  | Where  | Mechanism | What |
|-------|--------|-----------|------|
| 02:30 | **mickey** | `mise-backup.timer` → `mise-backup.service` (systemd, **enabled**) → `ops/backup.sh` | Consistent WAL-safe `sqlite3 .backup` of `/opt/mise/data/mise.db`, **integrity-checked before it is kept**, gzipped to `/opt/mise/data/backups/`, 14-day local retention. |

Local stage-1 is live and healthy (see `journalctl -u mise-backup.service`).

## Off-host DR — pending

When prod lived on flow, mickey also ran:

- `03:30` cron → `~/.local/bin/mise-backup-pull` (rsync flow backups/media/brand → `~/backups/mise/`)
- `04:00` cron → `~/.local/bin/mise-backup-verify` (restore-prove + Telegram on failure)

Those cron lines were **retired 2026-07-25** on mickey (commented in crontab): pulling
from flow would mirror a frozen origin, and verifying that copy no longer protects
live data. Scripts remain on disk under `~/.local/bin/` for reference; do not
re-enable them against flow.

**Gap (honest):** nightly snapshots currently share a disk with live data on
mickey. Replacement off-host DR (second machine or object store + restore-verify)
is **pending**. Until that lands, a mickey disk loss is not covered by the old
two-machine story.

Uptime still polls every 2 minutes via `~/.local/bin/mise-uptime-check` (cron).

## Why the old mickey-pull existed

The off-site copy lived on mickey so verification survived **flow** being down.
With prod on mickey, that inversion no longer holds — the always-on box is now
the origin of truth, so off-host durability must target a *different* sink.

## History

An earlier **flow-push** design (`mise-offsite.service`/`.timer`, `offsite-sync.sh`,
`restore-test.sh`) was never installed and was **pruned 2026-06-25** in favour of
mickey-pull. Prod then moved flow→mickey on **2026-07-25**; pull/verify cron was
retired the same day. `ops/` holds the deployed `backup.sh` + unit templates;
machine-local pull/verify/uptime scripts stay on mickey under `~/.local/bin/`.

## Restore (manual)

```sh
# on mickey — newest local snapshot (needs mise-readable path / sudo)
sudo ls /opt/mise/data/backups/
sudo gunzip -k /opt/mise/data/backups/mise-YYYY-MM-DD-HHMM.db.gz
sudo sqlite3 /opt/mise/data/backups/mise-YYYY-MM-DD-HHMM.db "PRAGMA integrity_check;"
# then: sudo systemctl stop mise
# swap /opt/mise/data/mise.db as user mise (keep permissions)
# sudo systemctl start mise
```

Historical copies under `~/backups/mise/db/` on mickey are from the pre-move
flow pull — useful archaeology, not a live off-host chain.
