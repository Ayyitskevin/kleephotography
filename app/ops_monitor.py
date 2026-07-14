"""Operational heartbeat — pushes a Telegram alert when the box is in a state
Kevin would want to know about WITHOUT opening the admin: free disk under the
upload floor, the nightly backup gone stale/missing, or background jobs waiting
in a failed state. It runs off the same recurring sweep as the reminders (no
cron, no second process).

This is the active-push counterpart to the Settings storage panel, which only
shows the same facts when someone happens to look. Silence is not evidence
(R21): a backup that simply stopped running produces no error anywhere, so we
assert the positive — "newest snapshot is N hours old" — and alert when that
crosses the threshold or there's no snapshot at all.

alerts.ops_alert throttles per signature, so a condition that persists across
many hourly sweeps re-pings at most twice a day rather than every hour. Dormant
unless Telegram is configured.
"""

import datetime as dt
import logging
import shutil

from . import alerts, config, db

log = logging.getLogger("mise.ops_monitor")


def storage_status() -> dict:
    """Read-only disk/backup facts shared by the heartbeat and /healthz."""
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1e9
    bdir = config.DATA_DIR / "backups"
    snaps = sorted(bdir.glob("*.db.gz")) if bdir.exists() else []
    newest = max(snaps, key=lambda p: p.stat().st_mtime) if snaps else None
    age_h = (dt.datetime.now().timestamp() - newest.stat().st_mtime) / 3600 if newest else None
    return {
        "disk_free_gb": round(free_gb, 2),
        "disk_low": free_gb < config.MIN_FREE_GB,
        "backup_present": newest is not None,
        "backup_age_hours": round(age_h, 2) if age_h is not None else None,
        "backup_stale": age_h is None or age_h > config.BACKUP_STALE_HOURS,
    }


def _check_disk(status: dict | None = None) -> None:
    status = status or storage_status()
    free_gb = status["disk_free_gb"]
    if status["disk_low"]:
        alerts.ops_alert(
            "disk_low",
            f"Low disk — {free_gb:.1f} GB free, below the {config.MIN_FREE_GB} GB "
            f"upload floor. New uploads are being refused until space is freed.",
        )


def _check_backup(status: dict | None = None) -> None:
    status = status or storage_status()
    if not status["backup_present"]:
        alerts.ops_alert(
            "backup_missing",
            "No database backup found at all — the nightly backup "
            "may have stopped. Check mise-backup.timer.",
        )
        return
    age_h = status["backup_age_hours"]
    if status["backup_stale"]:
        alerts.ops_alert(
            "backup_stale",
            f"Latest database backup is {int(age_h)}h old (over the "
            f"{config.BACKUP_STALE_HOURS}h threshold) — the nightly backup may "
            f"have stopped. Check mise-backup.timer.",
        )


def _check_failed_jobs() -> None:
    failed = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed'")["n"]
    if failed:
        alerts.ops_alert(
            "jobs_failed",
            f"{failed} background job{'s have' if failed != 1 else ' has'} failed. "
            "Open Admin → Jobs to review and retry the failures.",
        )


def sweep() -> None:
    """Check storage + failed jobs. Independent failures never block the loop."""
    if not alerts.is_enabled():
        return
    for check in (_check_disk, _check_backup, _check_failed_jobs):
        try:
            check()
        except Exception:
            log.exception("ops_monitor check failed")
