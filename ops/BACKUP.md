# Mise backup & restore

Production runs from `/opt/mise` on a single always-on host (`<prod-host>` — see
[`DEPLOY.md`](DEPLOY.md)). This file is the runbook for what protects the database
and how to get it back.

## What runs today

A systemd timer (`mise-backup.timer` → `mise-backup.service` → [`backup.sh`](backup.sh))
takes a nightly snapshot of `/opt/mise/data/mise.db`:

- `sqlite3 .backup`, so it is consistent and WAL-safe against a live database;
- **integrity-checked before it is kept** — a corrupt snapshot fails loud instead of
  silently replacing a good one;
- gzipped into `/opt/mise/data/backups/`, with older snapshots pruned on a rolling
  window.

Check the last run with `journalctl -u mise-backup.service`. The app also watches for
staleness (`app/ops_monitor.py`, threshold `MISE_BACKUP_STALE_HOURS`) and fires a
throttled ops alert, so a silently dead timer announces itself instead of being
discovered during a restore.

## Off-host DR — a known open gap

**This chain is host-local.** Snapshots protect against the failure modes that
actually happen most — a bad migration, a wrong delete, a corrupted table — and they
do not constitute disaster recovery. A replacement off-host sink (second machine or
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

Machine-local helper scripts and any pre-move archive copies live on the host under
the operator's own home directory — useful archaeology, not a live off-host chain.
