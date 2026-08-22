# Monitoring — the alarms that survive the host

Every alarm this system had before this document originated **on the box being
watched**. `app/alerts.py` needs the process alive to send a Telegram message;
`app/ops_monitor.py` needs the scheduler thread alive to run a check. That is
fine for the conditions they were built for — low disk, a stale backup, a wedged
job queue — and useless for the ones that matter most: a host that is powered
off, a container that wedged, a tunnel that dropped, a process that died at 3am.

In all of those, **nothing sends anything, and silence reads exactly like
health**. This is the R21 problem applied to the monitoring itself.

The fix is to invert the direction. The host emits a signal on a schedule and
something *off* the host alerts when the signal stops.

## Three legs, and what each one alone would miss

| Leg | Cadence | Catches | Blind to |
|---|---|---|---|
| 1. External HTTP monitor on `/healthz` | 1–5 min | host down, tunnel down, TLS expired, web server wedged | scheduler thread dead while the web server still answers |
| 2. App-liveness ping (`MISE_HEARTBEAT_PING_URL`) | hourly | scheduler loop dead or never started | a fast outage — an hourly ping can be up to ~3h late |
| 3. Backup ping (`MISE_BACKUP_PING_URL`) | nightly | dead `mise-backup.timer`, failing backup | anything between nightly runs |

They overlap on purpose. Leg 1 sees a dead box within minutes but cannot see a
dead background loop, because `/healthz` is served by the web worker and answers
happily while nothing is sweeping. Leg 2 sees exactly that, and is too coarse to
be your outage alarm. **Neither replaces the other; run both.**

## Leg 1 — external HTTP monitor (no code)

Point any external uptime service at `https://kleephotography.com/healthz` every
1–5 minutes.

**Assert on HTTP 200 and on the body being exactly `{"ok": true}`.** That is the
whole public contract now and it is stable — the endpoint answers 503 with
`{"ok": false}` when the database probe fails or times out. Asserting the body
too is worth the extra line: it catches DNS or proxy misrouting to some *other*
service that also happens to answer 200 on `/healthz`.

Do not build a monitor around `disk_free_gb`, `backup_age_hours` or the queue
counters. Those moved behind a bearer (`MISE_HEALTHZ_TOKEN`) — they told any
anonymous reader how close the box was to falling over, which is reconnaissance
and, during a resource-exhaustion attempt, a live progress meter. Read them with:

```sh
curl -fsS -H "Authorization: Bearer $MISE_HEALTHZ_TOKEN" \
  https://kleephotography.com/healthz | python3 -m json.tool
```

Leave `MISE_HEALTHZ_TOKEN` unset and the detail is simply unreachable over HTTP,
which is the safe default; the same facts stay visible in Admin → Settings.

Send its notifications somewhere that is *not* this host and not the same
Telegram bot the app uses — email or SMS is fine. A monitor that alerts through
the infrastructure it is monitoring is decoration.

## Legs 2 and 3 — dead-man's switches

Both work the same way: the host pings a URL, and the service alerts when a ping
does not arrive within a grace period. Any dead-man's-switch service works; the
URL is opaque to us.

Set in `/opt/mise/.env`:

```sh
MISE_HEARTBEAT_PING_URL=https://<service>/<uuid-for-app-heartbeat>
MISE_BACKUP_PING_URL=https://<service>/<uuid-for-backup>
```

Then `sudo systemctl restart mise` (leg 2 is read at startup). Leg 3 is read per
run by `backup.sh` and needs no restart, but the unit does read `.env`, so a
`daemon-reload` after editing is harmless and good habit.

**Use two separate checks, not one.** A single shared URL would let the hourly
app ping keep the check green while the nightly backup silently stopped — the
loud failure hidden by the quiet success.

Suggested grace periods:

- **app heartbeat** — period 1h, grace 2h. The scheduler tick is
  `MISE_RECURRING_TICK_SECONDS` (default 3600), so a tighter grace will
  false-alarm on a slow sweep.
- **backup** — period 1 day, grace 4h. The timer runs at 02:30 with up to 5
  minutes of randomized delay, and a large first-run file sync takes a while.

### What they deliberately do not do

**A ping means "the loop ran", not "everything is fine."** Liveness is not
correctness. A failing check still routes through `ops_monitor` → Telegram, and
keeping those two channels separate is what stops a low-disk warning from also
tripping the "monitoring is dead" alarm — which would make both meaningless.

The app ping is **not** gated on Telegram being configured. An alarm that
depended on the host's own alerting to work is the exact failure this exists to
compensate for.

The backup ping fires only on a **successful** run, from both exit paths —
including the one where stage 2 is skipped because no file destination is
configured, since stage 1 still really did back up the database. A failed backup
pings nothing and the missing ping is the alarm. A *failed ping* never fails the
backup: a monitoring outage must not turn a good backup into a failed unit, and
it errs toward alerting you about a backup that actually worked.

## Verifying it

```sh
# leg 2: restart and watch one tick arrive at the service (up to an hour)
sudo systemctl restart mise
# leg 3: force a run now — the service should show a ping within seconds
sudo -u mise /opt/mise/ops/backup.sh
```

Then **break it on purpose once**, because an untested alarm is a belief, not a
control: pause the check at the service (or set a 1-minute grace) and confirm the
notification actually reaches you. The failure mode of a dead-man's switch is
that it was never wired to a channel anyone reads.

## Off-host DR — check, don't assume

Monitoring tells you the host died; it does not give you a second copy to restore
from. That second copy is stage 3 — nightly encrypted restic plus a monthly
restore-verify — and it **exists in code** ([`DR.md`](DR.md),
[`BACKUP.md`](BACKUP.md)). It is opt-in, so whether *this* host runs it is a
question with an answer, not an assumption:

```sh
sudo grep -c '^RESTIC_REPOSITORY=' /opt/mise/.env   # 0 = stage 3 skips: no off-host copy
command -v restic || echo 'restic NOT installed — stage 3 cannot run here'
systemctl is-enabled mise-offsite.timer mise-restore-verify.timer   # not-found = never installed
```

Two dead-man's switches cover the armed case (`MISE_OFFSITE_PING_URL` nightly,
`MISE_RESTORE_VERIFY_PING_URL` on a ~5-week period). Neither covers the unarmed
one, and neither does leg 3: the backup ping fires from the stage-2-skipped exit
path as well, so a green switch proves the nightly *ran*, never that it copied the
files or shipped them off the host. Run the checks.
