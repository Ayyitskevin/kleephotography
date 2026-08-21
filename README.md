# Mise

**A self-hosted Pixieset + HoneyBook hybrid for one photographer/videographer.**
PIN-gated client galleries, content delivery, proposals → contracts → Stripe invoices,
and the public marketing site — one FastAPI + HTMX app on SQLite/WAL, with no ORM and no
JavaScript build step.

Live: **<https://kleephotography.com>** · Python 3.12+ (CI on 3.12 and 3.14) · no bundler, no broker, no ORM

---

## Why it exists

A working studio needs two things off the shelf: a gallery-delivery product (Pixieset and
friends) and a client-management product (HoneyBook and friends). That's two subscriptions,
two places client data lives, and two vendors between the photographer and their own
business records.

Mise is those two products collapsed into one application the owner runs, owns, and can
read the source of. Client galleries, the money pipeline, and the marketing site share one
database, one deploy, and one set of truths — so a paid invoice can move a project's stage
without an integration in between.

## What it does

- **Client galleries** — unguessable 14-character slug plus a 4-digit PIN, with per-IP
  lockout after 5 failed attempts. Favorites and proofing, threaded review notes on stills
  and films alike, single-asset and full-gallery ZIP downloads, Range-served media so iOS
  video scrubs properly, and AVIF/WebP served to browsers that ask for it.
- **Content delivery portal** — a per-client hub with social crops, brand kits, and usage
  and licensing rights.
- **Studio (the money side)** — proposals become contracts become Stripe invoices, with
  deposit/balance math and recurring-retainer plans that *draft* monthly work rather than
  auto-charging for it.
- **Public marketing site** — home, portfolio, services, case studies, press, contact, and
  the inquiry/booking form that starts the pipeline above.

## Screenshots

> **TODO (owner):** drop screenshots in `docs/screenshots/` and link them here — the
> gallery PIN gate, the admin studio board, and an invoice are the three that tell the
> story fastest. Deliberately left blank rather than filled with stale or invented images.

## Architecture

Three audiences, one process, one SQLite file:

| Surface | Code | Audience | Auth |
|---|---|---|---|
| Marketing site | `app/public/site.py` | Public, indexable | none |
| Client delivery | `app/public/{gallery,portal,downloads,media,pay}.py` | Clients | slug + PIN, per-IP lockout |
| Admin back office | `app/admin/*` | The photographer | password + signed cookie |
| Machine API | `app/service_api.py` | Internal automation | bearer token |

| Area | Path | Role |
|------|------|------|
| Entry | `app/main.py` | Middleware (rate limit, CSRF, canonical origin, CSP nonce), routers, `/healthz` |
| Data | `app/db.py` | WAL SQLite, forward-only migrations |
| Config / flags | `app/config.py`, `app/features.py` | Env-driven; integrations dormant until keyed |
| Money | `app/public/pay.py` + `app/admin/invoices.py` | Checkout + webhook vs draft/send |
| Jobs | `app/jobs.py` | Durable SQLite queue + thread pool (image derivatives, transcodes) |
| Templates | `templates/{site,public,admin}/` | 120 Jinja templates, HTMX fragments included |
| Static | `static/` | `mise.css` (legacy layer) + Screening Room CSS/JS |

## Engineering decisions worth a look

Each of these is a deliberate constraint, and each is one file away if you want to check it.

**No ORM.** `app/db.py` is ~270 lines of `sqlite3` wrapped in `one()` / `all_()` / `run()` /
`tx()`, over half of it the migration runner. Reads reuse a per-thread connection
(schema parse was 99% of `db.one`); writes still open and close per call so job
threads and `tx()` stay isolated. Dynamic identifiers go through
`db.ident(name, allowed)` — an allowlist that raises rather than interpolating — and values
always go through `?` placeholders. Decision: [`ops/DB-CONNECTIONS.md`](ops/DB-CONNECTIONS.md).

**Jobs are staged inside the caller's transaction.** `jobs.stage(con, kind, payload)`
inserts a queued row on the *caller's* connection; `jobs.dispatch(ids)` is only called after
that transaction commits. So a multi-file upload — a row per asset, a derivative or transcode
job per asset — either lands entirely or not at all. There is no window where a worker picks
up a job for a row that was rolled back, and none where a committed asset has no job for it
because the process died first. The rows are durable
in SQLite, so a crash mid-queue is recovered at startup rather than lost in memory.
(`app/jobs.py`, used by `app/admin/uploads.py`.)

**The Stripe webhook does not trust the webhook.** It verifies the signature, then
re-derives what is actually owed from the invoice in the database (`next_payment()`) and
compares against `amount_total` and the payment kind from Checkout. A mismatch is a 409 plus
a security alert, not a recorded payment. Idempotency is enforced at the database level —
`payments.stripe_event_id` is `UNIQUE`, the insert runs first inside the transaction, and a
Stripe retry surfaces as an `IntegrityError` that rolls the whole unit back and answers
`{"ok": true, "duplicate": true}`. (`app/public/pay.py`, `migrations/002_studio.sql`.)

**HTMX fragments carry their swap contract in the template.** Most partials (22 of 36) open
with a comment saying what swaps where, so a change to one end doesn't silently break the
other:

```jinja
{# HX fragment for the inbox POSTs (reply / retry-owner-email / notion-orphan
   relink / notion-orphan dismiss): the whole conversation pane (swapped
   outerHTML onto #ib-convo) plus the context pane and the active thread row
   riding out-of-band, so one POST re-trues all three regions. #}
```

**No JavaScript build step — and the JavaScript is still tested.** htmx plus six
hand-written vanilla files in `static/`. There is no `package.json`, no bundler, no
`node_modules`. The behavioral bits are covered by `node --test` suites in `tests/js/`, driven
from pytest (`tests/test_lightbox_node.py`) so they run in the same gate as everything else.

**A per-request CSP nonce, and `script-src` without `unsafe-inline`.** Every `on*` handler
was moved to data-attributes handled by `static/behaviors.js` — `grep -r 'onclick=' templates/`
returns nothing — so injected markup cannot execute script even if it slips past
autoescaping. `style-src` deliberately keeps `unsafe-inline`, and `app/main.py` says why.

**A design-token layer with an honest kill switch.** `static/screening-room-tokens.css`
holds the tokens; components live under `body.sr` in `static/screening.css`; `mise.css`
loads inside a CSS cascade layer so the redesign wins without specificity wars. Setting
`MISE_SCREENING_ROOM=false` drops the whole layer and the legacy look is still there
underneath. The inventory of what each layer owns is in
[`ops/CSS-DUAL-STACK.md`](ops/CSS-DUAL-STACK.md).

**Forward-only migrations, applied at startup.** 72 numbered `.sql` files in `migrations/`,
recorded in `schema_migrations`, readable in `git log` instead of hidden behind a tool.
`db.MIGRATION_ALIASES` is the scar tissue: the same migration once got applied under two
different filenames, so both names are treated as equivalent and a clean deploy doesn't
re-run its `ALTER`s against a database that already has them. Policy:
[`ops/MIGRATIONS.md`](ops/MIGRATIONS.md).

**Tests in a unit / integration / smoke split**, so feedback is tiered rather than
one slow suite: unit (pure logic, no DB), integration (SQLite + `TestClient` seams),
smoke (end-to-end against a throwaway DB, ffmpeg required for the video path). CI
runs all three plus ruff on every push to `main` and every pull request, and a
second job re-runs unit + integration on 3.12 so the supported floor above is
tested rather than asserted. Coverage is measured against `app/` with a floor CI
enforces. Counts drift; the gates in CI are the contract.

## Local setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
# ffmpeg required for video smoke tests
cp .env.example .env   # set MISE_SECRET_KEY + MISE_ADMIN_PASSWORD at minimum
uvicorn app.main:app --reload --port 8400
```

Migrations run automatically on startup. Secrets and integration keys are documented in
[`.env.example`](.env.example); integrations stay dormant until their keys are set. CI and
CI and local installs use **pip + the committed hash locks** (`requirements.lock`
for runtime, `requirements-dev.lock` for tests and tooling). Regenerate them from
the short exact-pinned input files when dependencies change; do not commit `uv.lock`.

### Feature flags (kill switches)

| Env | Default | Effect |
|-----|---------|--------|
| `MISE_SCREENING_ROOM` | on | Cinema look (`body.sr` / `sr-admin`). `=false` restores legacy cream. |
| `MISE_AERIALS_LIVE` | on | Aerial Pass band, booking add-on, ticker/credits. `=false` hides it. |

## Gates (must pass before a change is done)

Matches CI (`.github/workflows/ci.yml`):

```sh
source .venv/bin/activate
# 1. unit — fast, pure logic
python -m pytest tests/ --ignore=tests/smoke -q -m unit
# 2. integration — SQLite + TestClient seams
python -m pytest tests/ --ignore=tests/smoke -q -m integration
# 3. full smoke — e2e against a throwaway DB (ffmpeg required for video tests)
# Domain slices live under tests/smoke/ (ordered test_01_… → test_07_…).
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/ -q -m smoke
# 4. lint + format
ruff check . && ruff format --check .
```

## Deploy

Canonical full-site deploy is **git pull on the production host + restart mise**.
See [`ops/DEPLOY.md`](ops/DEPLOY.md).

`scripts/deploy-flow.sh` is a **specialty rsync slice** (legacy name; it ships only the
Plutus/Argus/Platekit bits to the host you point it at), not the full-tree deploy path.

## Security

Reporting contact and scope: [`SECURITY.md`](SECURITY.md). The live site also serves an
RFC 9116 [`/.well-known/security.txt`](https://kleephotography.com/.well-known/security.txt),
rendered from live config so it can't go stale.

## Working on this

Most of the day-to-day work here is done by AI agents under a written scope contract, with
a human on the irreversible parts.

- [`AGENTS.md`](AGENTS.md) — the agent scope contract: green-light work, red-light work that
  needs a human (money, schema, deploy, security, contracts), the gates above, and the
  conventions that bite.
- [`ops/`](ops/) — operator runbooks: [`DEPLOY.md`](ops/DEPLOY.md),
  [`BACKUP.md`](ops/BACKUP.md) (nightly local snapshot + durable files),
  [`DR.md`](ops/DR.md) (off-host restic; opt-in until `RESTIC_REPOSITORY` is set),
  [`MIGRATIONS.md`](ops/MIGRATIONS.md), [`CSS-DUAL-STACK.md`](ops/CSS-DUAL-STACK.md),
  [`SPECIALTY-LAUNCH.md`](ops/SPECIALTY-LAUNCH.md),
  [`AT-LEAST-ONCE.md`](ops/AT-LEAST-ONCE.md) — jobs/reminders send-then-record contract.
- [`ENHANCEMENT-BRIEF.md`](ENHANCEMENT-BRIEF.md) — where Mise sits against the products it
  replaces (Pixieset, Pic-Time, ShootProof, HoneyBook, Dubsado, …), what that benchmark
  changed, and the ranked red/green queue it left behind.
- [`HANDOFF.md`](HANDOFF.md) — historical refactor notes. Prefer AGENTS.md and this README.
