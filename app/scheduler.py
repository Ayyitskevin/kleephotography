"""Mise's first scheduler — an in-process daemon thread for recurring retainers.

It wakes on an interval and runs the due recurring plans, generating DRAFT
invoices only — it never sends or charges (the manual-send doctrine is intact:
Kevin still clicks Send, Stripe still collects). It is deliberately the simplest
thing that works: no cron, no run_at column, no second process. The sweep is
idempotent (the period claim in recurring.generate_for_plan dedupes per month),
so the loop can fire as often as it likes and a plan still gets exactly one draft
per period.

The thread WAITS one interval before its first sweep — there is no sweep-on-boot.
That keeps test lifespan cycles from generating anything, and in production it
just means a due monthly draft is caught up within one interval of a restart,
which is plenty for a monthly event.
"""

import logging
import threading

from . import booking_reminders, config, gallery_reminders
from .admin import recurring

log = logging.getLogger("mise.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    while not _stop.wait(config.RECURRING_TICK_SECONDS):
        try:
            recurring.run_due_plans()
        except Exception:
            log.exception("recurring sweep failed")
        try:
            booking_reminders.sweep()
        except Exception:
            log.exception("booking reminder sweep failed")
        try:
            gallery_reminders.sweep()
        except Exception:
            log.exception("gallery reminder sweep failed")


def start() -> None:
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="mise-recurring", daemon=True)
    _thread.start()
    log.info("recurring scheduler up (every %ss, drafts only)",
             config.RECURRING_TICK_SECONDS)


def stop() -> None:
    global _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
        _thread = None
