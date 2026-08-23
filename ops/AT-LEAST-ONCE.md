# Delivery contract — at least once, never at most once

**The rule.** Every background send in Mise — a job handler, a reminder sweep, a
one-way mirror — does the outside-world side effect **first** and records that it
happened **second**. A crash in that gap re-sends on the next attempt. That is
deliberate: **a duplicate is the acceptable failure, a silent drop is not.**

Do not "fix" this by stamping the flag before the send. Flag-then-send buys
at-most-once, which trades a visible duplicate for an invisible loss — for a
solo studio, a client who never got their gallery-expiry warning or a lead Kevin
never heard about costs real money, while a second copy of the same email costs
an apology. If you need to tighten a specific path, add idempotency at the
receiver (a claim lock, a stamped remote id) rather than inverting the order.

## Why the gap exists at all

SQLite is the only durable store and the send is a network call. There is no
transaction that spans both, so one of them has to go first. Nothing here is
worth a two-phase commit or an outbox table; the queue plus a couple of
idempotency stamps is the whole reliability budget.

## What relies on it

| Module | Side effect → record | Re-send trigger |
|--------|----------------------|-----------------|
| `app/jobs.py` | `_execute` runs the handler, then marks `done` | startup re-queues rows left `running` by a crash; a failed attempt is retried up to `MAX_ATTEMPTS` after a backoff (`next_attempt_at`), re-offered by the queue's sweeper thread (below) |
| `app/booking_reminders.py` | `mailer.send` → `reminded_48h` / `reminded_24h` | next scheduler tick, while the send window is still open |
| `app/gallery_reminders.py` | `mailer.send` → `reminded_expiry` / `reminded_proofing` | next scheduler tick, while still due |
| `app/contract_reminders.py` | `alerts.notify` → `nudged_unsigned` | next scheduler tick (internal Telegram nudge, never the client) |
| `app/postshoot_reminders.py` | `hermes_arm.arm` → `armed_postshoot` | next scheduler tick; Hermes dedups by key, so a re-arm is a no-op |

Handlers registered in `jobs.HANDLERS` must therefore be **idempotent or
cheap to repeat**. The shipped ones are: derivative/rendition/crop/zip builds
re-render to the same path, and the Notion + owner-email paths carry their own
guards (below).

## The retry clock (and how to override it)

A failed attempt does not re-run immediately — it parks the row as `queued` with
a future `next_attempt_at`, and `jobs._claim` refuses to run it until that
instant. What un-parks it is the queue's **own** sweeper thread
(`MISE_JOB_SWEEP_TICK_SECONDS`, default 60s), started by `jobs.start()`. It is
deliberately not the hourly recurring scheduler: on an hourly clock a "60 second"
backoff would really mean "sometime within the hour" and the numbers in
`RETRY_BACKOFF_SECONDS` would be fiction. Real gap = backoff, plus at most one
tick, minus up to a second (SQLite's `datetime()` is whole-second): **59–120s**
then **299–360s** at the defaults.

Operator overrides, both of which clear `next_attempt_at`:

- **Admin → Jobs → run now** on a row in *Waiting to retry* (`jobs.retry`, which
  matches a parked row as well as a `failed` one). This is the hands-on control
  over a stuck queue; it must keep working *during* a backoff window.
- **Admin → Jobs → retry** on a `failed` row — same call, resets the attempts.

## When the queue stops moving

`jobs.queue_health()` is the single reader for this, surfaced two ways:

- `/healthz` → `jobs_failed`, `jobs_waiting_retry`, `jobs_stuck` — **bearer-gated**
  (`Authorization: Bearer $MISE_HEALTHZ_TOKEN`); the public body is `{"ok": …}`
  only. See [`MONITORING.md`](MONITORING.md).
- `ops_monitor._check_failed_jobs` → Telegram `jobs_failed` and `jobs_stuck`.

`stuck` means a **retry** is more than `MISE_JOB_STUCK_AFTER_SECONDS` (default
15 min) past its `next_attempt_at` and still unclaimed — the sweeper thread or
the pool is down, or the timestamp is bad. `failed` alone cannot see that: a
parked row keeps `failed` at 0 while nothing drains, which is exactly the silent
stall this counter exists to break.

Never-attempted work is deliberately excluded from `stuck`: two workers grinding
through a batch of video transcodes leave fresh jobs queued for an hour as
normal, and that is what `jobs_pending` is for.

## Where a stronger guarantee was bought

Two paths were not safe to simply repeat, so they add receiver-side idempotency
on top of the same at-least-once queue:

- `app/inquiry_notify.py` — owner-email delivery takes an `owner_email_status`
  `in_flight` claim and stamps `owner_email_delivered_at`, so a retry after a
  crashed attempt does not mail Kevin twice about one lead.
- `app/notion_sync.py` — stamps `inquiries.notion_page_id` on create and patches
  that page afterwards; a create-race loser is recorded as
  `notion_orphan_page_id` for the operator instead of leaving a duplicate lead.
  See [`LEADS-NOTION.md`](LEADS-NOTION.md).

Copy that shape when a new send genuinely cannot tolerate a duplicate. Do not
copy it by default — the flags above are enough for a reminder.
