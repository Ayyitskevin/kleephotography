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

## Durability: what a power cut can cost

SQLite runs with `synchronous=NORMAL` (`app/db.py`), the standard WAL pairing:
commits are not fsynced individually, they are flushed at checkpoint. A crashed
or restarted **process** loses nothing — the WAL lives in the OS page cache and
outlives it. An **OS crash or power cut** can lose the most recent commits,
typically seconds of work.

That trade buys a large reduction in write latency on a single-writer database
where the busiest path is serving images. If it is ever the wrong trade — a
worsening power situation, no UPS — `MISE_SQLITE_SYNCHRONOUS=FULL` restores
fsync-per-commit with a restart and no deploy. Worth knowing which writes are
exposed: Stripe redelivers unacknowledged webhooks, so payment state re-converges
on its own; a contract signature or a form submission in that window would need
redoing.

## Off-host DR — stage 3

**Stages 1 and 2 are host-local.** They protect against the failure modes that
actually happen most — a bad migration, a wrong delete, a corrupted table, a dead
disk if the file destination is a genuinely separate one — and on their own they do
not constitute disaster recovery: fire, theft, or the host being lost takes
everything.

**Stage 3 closes that**: a nightly client-side-encrypted push to an off-host
repository ([`offsite-backup.sh`](offsite-backup.sh)), plus a monthly
restore-verify that proves the data actually comes back
([`restore-verify.sh`](restore-verify.sh)). Runbook: [`DR.md`](DR.md).

**The chain exists in code; whether a given host runs it is a separate question.**
Stage 2 and stage 3 are both opt-in, and an unarmed stage does not fail — it skips
and the run still exits 0. Worse for a casual reader: the backup ping fires from
the stage-2-skipped exit path too, so a **green dead-man's switch does not prove
the files were copied** ([`MONITORING.md`](MONITORING.md)). Never infer the armed
state; check it:

```sh
sudo grep -c '^MISE_FILE_BACKUP_DIR=' /opt/mise/.env   # 0 = stage 2 skips: db only
sudo grep -c '^RESTIC_REPOSITORY='    /opt/mise/.env   # 0 = stage 3 skips: no off-host copy
command -v restic || echo 'restic NOT installed — stage 3 cannot run here'
# the units ship in ops/ but are inert until installed and enabled (DR.md):
systemctl is-enabled mise-offsite.timer mise-restore-verify.timer   # not-found = never installed
# what the last run actually did, in its own words:
journalctl -u mise-backup.service -n 30 --no-pager | grep -E 'stage[12]'
```

Read that last one carefully. Every run prints a stage-2 line — `stage2 ok:` or
`stage2 SKIPPED:`. **No stage-2 line at all** means neither: the host is running a
`backup.sh` that predates the stage, i.e. the deploy tree is behind `main`. Check
with `git -C /opt/mise log -1` and see [`DEPLOY.md`](DEPLOY.md); a unit file also
only picks up `EnvironmentFile` after it is reinstalled and `daemon-reload`ed.

Until those checks come back armed on *this* host, treat "we have backups" as "we
can undo a mistake," not "we can survive losing the host" — and treat the monthly
restore-verify as unproven, because skipping is not passing.

An earlier two-machine pull-and-verify chain existed and was retired when production
moved hosts (verifying a frozen copy protects nothing). The lesson worth keeping — the
off-host sink has to be a *different* box from the origin of truth, and the copy is
only real once a scripted restore has proven it — is why stage 3 ships with
`restore-verify.sh` rather than trusting `restic check` alone.

Whenever that gap is open — and the checks above are the only way to know it is not
— the red-light rules in [`AGENTS.md`](../AGENTS.md) do real work. Even fully armed,
a nightly snapshot is not a license to run an unreviewed migration or a live money
change.

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
