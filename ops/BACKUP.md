# Mise backup & restore

Production runs from `/opt/mise` on a single always-on host (`<prod-host>` — see
[`DEPLOY.md`](DEPLOY.md)). This file is the runbook for what protects the database
and how to get it back.

## What runs today

A systemd timer (`mise-backup.timer` → `mise-backup.service` → [`backup.sh`](backup.sh))
runs nightly in two stages.

**Stage 1 — the database.** A snapshot of `/opt/mise/data/mise.db`:

- `sqlite3 .backup`, so it is consistent and WAL-safe against a live database;
- **integrity-checked before it is kept** — a corrupt snapshot fails loud instead of
  silently replacing a good one, and the compressed archive is `gzip -t`'d too,
  because an intact database inside a truncated `.gz` restores nothing;
- gzipped into `/opt/mise/data/backups/`, pruned on a 14-night window.

**Stage 2 — the durable files.** `media/`, `brand/` and `receipts/` — the client
originals and the financial scans. The database is the smaller half of the estate;
for a photography business the originals *are* the product, and nothing copied them
anywhere until this stage existed.

- Requires `MISE_FILE_BACKUP_DIR` (see [`.env.example`](../.env.example)) and `rsync`
  on the host. Unset = the stage **skips loudly** and only the database is copied —
  stage 1 has already succeeded by then, so a missing destination never costs you the
  database snapshot as well.
- Snapshots are hardlinked against the previous night (`rsync --link-dest`), so a
  mostly-immutable media tree costs one copy plus each night's delta, not N copies.
  That is not only about disk: **a plain mirror would replay a wrong delete onto the
  only other copy**, which is the failure this is meant to survive. Fourteen nights
  are kept, and `files/latest` symlinks the newest.
- `mise.db` is excluded on purpose (a raw copy of a live WAL database is not a valid
  backup); so are `zips/` and `tmp/`, which are rebuildable derivatives.
- Refuses to run below `MISE_FILE_BACKUP_MIN_FREE_GB` (default 5) free.
- Warns every night if the destination shares a filesystem with `/opt/mise/data` —
  that arrangement survives a wrong delete but **not** the disk dying.

The run also pings a dead-man's switch on success when `MISE_BACKUP_PING_URL` is
set, so a timer that silently stops announces itself through a channel that does
not share this host's fate — see [`MONITORING.md`](MONITORING.md).

Check the last run with `journalctl -u mise-backup.service`. The app also watches for
staleness (`app/ops_monitor.py`, threshold `MISE_BACKUP_STALE_HOURS`) and fires a
throttled ops alert, so a silently dead timer announces itself instead of being
discovered during a restore.

## Off-host DR — a known open gap

**This chain is host-local, both stages.** Snapshots protect against the failure modes
that actually happen most — a bad migration, a wrong delete, a corrupted table, a dead
disk if the file destination is a genuinely separate one — and they do not constitute
disaster recovery. Fire, theft, or the host being lost still takes everything. A replacement off-host sink (second machine or
object store) plus an automated restore-verify is **pending and tracked**; until it
lands, treat "we have backups" as "we can undo a mistake," not "we can survive losing
the host."

An earlier two-machine pull-and-verify chain existed and was retired when production
moved hosts (verifying a frozen copy protects nothing). The lesson worth keeping: the
off-host sink has to be a *different* box from the origin of truth, and the copy is
only real once a scripted restore has proven it.

Because the gap is open, the red-light rules in [`AGENTS.md`](../AGENTS.md) do real
work: a nightly snapshot is not a license to run an unreviewed migration or a live
money change.

## Restore (manual)

```sh
# on the prod host — newest local snapshot (needs a mise-readable path / sudo)
sudo ls /opt/mise/data/backups/
sudo gunzip -k /opt/mise/data/backups/mise-YYYY-MM-DD-HHMM.db.gz
sudo sqlite3 /opt/mise/data/backups/mise-YYYY-MM-DD-HHMM.db "PRAGMA integrity_check;"
# then: sudo systemctl stop mise
# swap /opt/mise/data/mise.db as user mise (keep ownership and permissions)
# sudo systemctl start mise
```

Always run the `integrity_check` before swapping anything in, and never restore over
the live file without stopping the service first.

## Restore — a deleted or damaged client file

Stage 2's snapshots are plain directories, so recovery is a copy. Each night is a
full-looking tree (unchanged files are hardlinks, not duplicates):

```sh
ls "$MISE_FILE_BACKUP_DIR/files/"                 # the nights available
ls "$MISE_FILE_BACKUP_DIR/files/latest/media/"    # newest night
# copy one gallery's originals back, preserving ownership/timestamps
sudo rsync -a "$MISE_FILE_BACKUP_DIR/files/2026-08-10-0230/media/42/" \
              /opt/mise/data/media/42/
```

Pick a night from **before** the loss: `latest` mirrors the live tree, so if a file
was deleted yesterday `latest` no longer has it either. Restoring media needs no
downtime — the app reads these files per request and does not cache them.

Machine-local helper scripts and any pre-move archive copies live on the host under
the operator's own home directory — useful archaeology, not a live off-host chain.
