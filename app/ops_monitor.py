"""Operational heartbeat — pushes a Telegram alert when the box is in a state
Kevin would want to know about WITHOUT opening the admin: free disk under the
upload floor, or the nightly backup gone stale/missing. It runs off the same
recurring sweep as the reminders (no cron, no second process).

This is the active-push counterpart to the Settings storage panel, which only
shows the same facts when someone happens to look. Silence is not evidence
(R21): a backup that simply stopped running produces no error anywhere, so we
assert the positive — "newest snapshot is N hours old" — and alert when that
crosses the threshold or there's no snapshot at all.

alerts.ops_alert throttles per signature, so a condition that persists across
many hourly sweeps re-pings at most twice a day rather than every hour. Dormant
unless Telegram is configured.
"""

import logging

from . import alerts, config

log = logging.getLogger("mise.ops_monitor")


def _check_disk() -> None:
    import shutil
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    if free_gb < config.MIN_FREE_GB:
        alerts.ops_alert(
            "disk_low",
            f"Low disk — {free_gb:.1f} GB free, below the {config.MIN_FREE_GB} GB "
            f"upload floor. New uploads are being refused until space is freed.")


def _check_backup() -> None:
    import datetime as dt
    bdir = config.DATA_DIR / "backups"
    snaps = sorted(bdir.glob("*.db.gz")) if bdir.exists() else []
    if not snaps:
        alerts.ops_alert("backup_missing",
                         "No database backup found at all — the nightly backup "
                         "may have stopped. Check mise-backup.timer.")
        return
    newest = max(snaps, key=lambda p: p.stat().st_mtime)
    age_h = (dt.datetime.now().timestamp() - newest.stat().st_mtime) / 3600
    if age_h > config.BACKUP_STALE_HOURS:
        alerts.ops_alert(
            "backup_stale",
            f"Latest database backup is {int(age_h)}h old (over the "
            f"{config.BACKUP_STALE_HOURS}h threshold) — the nightly backup may "
            f"have stopped. Check mise-backup.timer.")


def sweep() -> None:
    """Check disk + backup, alert on trouble. Each check is independent and
    best-effort — one failing must not stop the other or block the loop."""
    if not alerts.is_enabled():
        return
    for check in (_check_disk, _check_backup):
        try:
            check()
        except Exception:
            log.exception("ops_monitor check failed")
