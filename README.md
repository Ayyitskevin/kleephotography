# Mise

**Self-hosted delivery + business platform for a Food & Beverage photographer/videographer.**
A single-operator "Pixieset + HoneyBook hybrid" — client galleries, content delivery,
proposals/contracts/invoices, recurring social retainers, and a public marketing site —
built as one FastAPI app with no JS build chain.

Live: <https://kleephotography.com> · Runs on a single always-on node behind a Cloudflare Tunnel.

---

## What it does

- **Client galleries** — PIN-gated delivery, favorites/proofing, video comments, single-asset
  and full-gallery ZIP downloads, iOS-friendly Range-streamed media.
- **F&B content portal** — per-client hub with auto social crops (1:1, 4:5, 9:16), brand kits,
  caption packs, usage/licensing rights, and a content calendar.
- **Studio (the money side)** — proposals → contracts (typed-name e-sign) → Stripe invoices,
  plus recurring-retainer plans that auto-*draft* monthly deliverables (never auto-send/charge).
- **Public marketing site** — home, portfolio, services, work case studies, testimonials,
  press, about, contact, and an inquiry/booking form.

## Stack

FastAPI · Jinja2 · HTMX (no front-end build) · SQLite (WAL) · Pillow + pillow-heif (imaging) ·
ffmpeg (video transcode/poster) · Stripe (payments) · itsdangerous (signed cookies).
Python deps pinned in `requirements.txt`. ~12.5K LOC Python across 70 modules
(~19.5K with tests); one 141-case end-to-end smoke suite. No ORM, no JS framework,
no message broker — the platform is the standard library plus four well-chosen packages.

## Architecture

Three surfaces, one process:

| Surface | Code | Audience | Auth |
|---|---|---|---|
| Marketing site | `app/public/site.py` | Public / indexable | none |
| Client delivery | `app/public/{gallery,portal,downloads,media,pay}.py` | Clients | 14-char slug + 4-digit PIN, per-IP lockout |
| Admin back office | `app/admin/*` | The photographer | password + signed cookie |
| Machine API | `app/service_api.py` | Internal automation | bearer token (`/api/shots`) |

**Spine:** `main.py` (app factory + middleware), `config.py` (env-driven), `db.py`
(SQLite, short-lived connections, 53 forward-only migrations in `migrations/`),
`security.py` (slugs/PINs/lockout/cookies), `jobs.py` (in-process queue for image
derivatives + video transcodes), `scheduler.py` (retainer thread — drafts only).

**Integration doctrine — one-way, by design.** Mise *owns* money and media truth. It pushes
status **outward only**: to Notion (`notion_sync.py`) and an external Odysseus CRM
(`caption_ai.py`, `reopen_notify.py`). There is **no bidirectional sync anywhere** — that is a
deliberate constraint, not a missing feature.

## Notable engineering decisions

The deliberate constraints — each is a choice, not an omission, and most trade scale I
don't need for operability I do:

- **SQLite over Postgres.** One operator, one node, low write concurrency. WAL mode handles
  the read-heavy gallery traffic; the whole DB backs up with a file copy. A network DB would
  add an ops surface with no payoff at this scale.
- **In-process job queue over Celery/Redis.** Derivatives and transcodes run on a small
  executor pool with a startup reconcile for jobs orphaned by a crash (`jobs.py`). No broker
  to run, monitor, or secure — uploads still return fast.
- **HTMX over a SPA.** Server-rendered Jinja with HTMX for partial swaps. No build step, no
  bundle, no client-state duplication. Four hand-written vanilla JS files cover the rest.
- **Forward-only migrations.** Plain numbered `.sql` applied on startup, idempotent, with
  hand-written down-scripts in `rollback/` for the riskier schema changes. Schema history is
  readable in `git log`, not hidden behind a tool.
- **Money/media truth is local; integrations are one-way.** Avoids the split-brain class of
  bug entirely — nothing external can write back and disagree with what Stripe charged.
- **Defense in depth at the edge.** App-level PIN lockout + unguessable slugs *and*
  Cloudflare Access on `/admin/*`, so a single layer failing is not a breach.

## Testing

`tests/test_smoke.py` is one end-to-end suite (141 cases) that exercises real routes against
a real SQLite DB via FastAPI's `TestClient` — auth gating, PIN lockout, the proposal →
contract → invoice → receipt flow, Stripe webhook signature verification, and template
rendering. Tests assert *intent* (e.g. a webhook with a bad signature is rejected), not just
status codes.

```bash
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  MISE_ENV_FILE=/nonexistent .venv/bin/python -m pytest tests/test_smoke.py -q
```

## Repo layout

```
app/
  main.py config.py db.py security.py render.py jobs.py scheduler.py audit.py
  admin/      back-office routers (galleries, studio, invoices, contracts,
              proposals, licenses, presets, press, recurring, shotlist, uploads, activity)
  public/     client + marketing routers (gallery, portal, downloads, media, pay, docs, site)
  service_api.py            bearer-gated /api/shots
  imaging.py video.py       media pipeline
  notion_sync.py caption_ai.py reopen_notify.py   one-way outbound integrations
migrations/   001..053 forward-only (+ rollback/ down-scripts for risky changes)
templates/    admin/ · public/ · site/   (93 Jinja + HTMX templates)
static/       mise.css + htmx.min.js + 4 vanilla JS (lightbox, copy-link, details-persist, site)
ops/          systemd units + nightly backup
tests/        test_smoke.py (141 end-to-end cases, real DB + TestClient)
```

## Running it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in real values (mode 600, never committed)
uvicorn app.main:app --host 127.0.0.1 --port 8400
```

Migrations run automatically on startup (`db.migrate()`). Production runs under systemd
(`mise.service`) with a nightly backup timer (`ops/mise-backup.timer`).

### Configuration

All config is env-driven via `app/config.py` (loads `/opt/mise/.env`). Keys cover the secret
key, admin password, Stripe keys, Gmail app password (manual-send only), Notion token, the
Odysseus caption/reopen URLs, and the `/api/shots` bearer token. **No secrets live in the
repo** — `.env` is git-ignored; `.env.example` holds placeholders only.

## Design system

The visual layer is a single hand-written `static/mise.css` plus Jinja partials under
`templates/site/` (marketing) and `templates/public/` (client-facing). Brand colors, spacing,
and type scale are defined as CSS custom properties at the top of `mise.css`.

## Security posture

- Tiered auth (see table); client PINs have per-IP brute-force lockout (5 fails → 15 min).
- Gallery slugs are 14-char base62 (unguessable); all non-marketing routes send
  `X-Robots-Tag: noindex`; `X-Frame-Options: DENY` everywhere.
- `CF-Connecting-IP` is trusted only when the peer is localhost (tunnel-correct rate limiting).
- Query values always use `?` placeholders; the few interpolated table/column names pass
  through `db.ident()`, an allowlist gate that raises rather than letting a stray identifier
  reach the query.
- Secrets in `.env` (mode 600), never in code, logs, or history. Stripe webhooks are
  signature-verified.

## Status & scope

Single-operator (not multi-tenant). Self-hosted on one node; designed for later VPS
lift-out. Money and media truth live here; everything else reads from Mise one-way.
