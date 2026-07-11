# AGENTS.md — scope contract for autonomous work on Mise

Mise is **live production**: flow `100.90.17.20:8400` · `/opt/mise` · `kleephotography.com`.
It handles **real Stripe money for real clients**. This file is the contract that lets an
agent work continuously without a human watching every commit. Read it before you touch
anything. When code and this file disagree, stop and ask — don't reconcile silently.

This repo (`~/ai-workspace/mise-claude`, or your own clone) is a **working copy, NOT
production**. flow `/opt/mise` is git + deploy canonical. Never scratch-edit the flow tree.

Scope derivation: the red/green split below narrows the fleet-wide **Permission
Boundaries** block (canonical in each machine's CLAUDE.md / GROK.md /
`~/.codex/AGENTS.md`). This file may be stricter than that block for Mise,
never looser — if the two ever disagree, stop and flag; don't reconcile silently.

## Green-light — work freely, commit straight to `main`, push `all`

You don't need a human for any of this. Make the change, prove it green (gates below),
`git commit` + `git push all main`.

- Refactors, readability, dead-code removal, comments — within one logical change.
- Tests: new coverage, fixing flaky/ordering issues, fixtures.
- UI / templates / CSS / HTMX, public-site copy and layout.
- Non-money admin features (galleries, proofing, shotlist, presets, press, licenses).
- Tooling: ruff config, CI tweaks, dev scripts, docs.
- Dependency bumps that pass the full suite.

## Red-light — STOP and open a PR, leave the merge to Kevin

These are irreversible, money/legal, or schema-shaped. Do the work on a
`claude/<topic>` branch, push it, open a PR with the risk spelled out, and **do not
self-merge**. A human presses the button (R1).

- **Money path** — anything under `app/public/pay.py`, Stripe Checkout/webhook logic,
  invoice/payment/deposit/balance math, `payments`/`invoices` state transitions.
- **Schema / migrations** — any new file in `migrations/`, any `ALTER`/`CREATE`/`DROP`,
  any change to table shape or a UNIQUE/CHECK constraint. (Migrations are forward-only
  and run against the live DB.)
- **Deploy** — touching the flow tree, `scripts/deploy-flow.sh`, `mise.service`,
  `ops/backup.sh`, the systemd units, or anything in the backup/restore chain.
- **Security** — `app/security.py`, `app/admin/auth.py`, CSRF/session/cookie logic,
  rate-limit/lockout, secrets handling.
- **Contracts / legal** — proposal/contract generation, e-sign, anything a client signs.

When unsure which side a change sits on, treat it as red. The cost of a needless PR is
minutes; the cost of an unattended money/schema mistake is an incident.

## Gates — a commit is not "done" until all pass (R4)

```sh
source .venv/bin/activate
# 1. unit (fast, pure logic — no DB/network)
python -m pytest tests/ --ignore=tests/test_smoke.py -q -m unit
# 2. full smoke (e2e against a throwaway DB)
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/test_smoke.py -q
# 3. lint + format (CI enforces both, strict)
ruff check . && ruff format --check .
```

"Tests pass" is false if any were skipped or mocked without saying so. Run all three
locally before pushing — CI runs the same on `main` and on PRs.

## Conventions that bite if you miss them

- **Conform, don't reinvent** (R11). Match the surrounding style even where you'd choose
  differently. Surface a harmful convention; never silently fork it.
- **Surgical changes** (R10). Touch only what the task needs. No drive-by cleanup of
  adjacent code in the same commit.
- **No abstraction for single-use code** (R9). Extract a helper only when there's real
  shared use or drift risk, not on spec.
- **SQL safety.** All dynamic SQL uses bound `?` placeholders or `db.ident(value,
  whitelist)` for identifiers. Never f-string a user value into SQL.
- **Secrets.** Never write tokens/credentials into the repo, logs, or test fixtures.
  They live in untracked `.env` per machine (`.env.example` documents the keys).
- **One financial clock.** Studio date boundaries read `studio._today()` (monkeypatchable
  wall-clock), not `datetime.date.today()` directly.

## Operating the live admin (if your task does, not just edits code)

After any **write** in Mise admin (client/project/shot create or update, status change),
append one row to the Notion **Mise Activity Log** (Command Center · db
`14ed3722-8165-48a2-82e0-cecbaf4c5daa`) — one-way, display-only, one row per write. Never
read that log back into any Mise flow.

## Safety net (so you know what's catching you)

Nightly, proven end-to-end: flow takes an integrity-checked SQLite snapshot (02:30),
mickey pulls it off-disk (03:30) and **proves it restores** (04:00), alerting to Telegram
on any failure. See `ops/BACKUP.md`. This means a data mistake is recoverable to the last
nightly — it does **not** make a bad migration or a wrong Stripe charge a non-event. Red-light
rules still hold.

## Done means

Gates green · change is one logical unit · red-light items went through a PR · commit
message says what and why · `git push all main` (green-light) or PR pushed (red-light).
