# Mise — Kevin Lee Photography

FastAPI + Jinja + HTMX + SQLite studio app for [kleephotography.com](https://kleephotography.com).
Live production runs on **flow** at `/opt/mise` (port 8400). This clone is a
working copy — never scratch-edit the flow tree.

Agent scope contract: **[AGENTS.md](AGENTS.md)** (green/red lights, gates, money rules).
Ops runbooks live under [`ops/`](ops/).

## Architecture

| Area | Path | Role |
|------|------|------|
| Entry | `app/main.py` | Middleware (rate limit, CSRF, CSP nonce), routers, `/healthz` |
| Data | `app/db.py` | WAL SQLite, forward-only migrations |
| Config / flags | `app/config.py`, `app/features.py` | Env-driven; integrations dormant until keyed |
| Admin | `app/admin/*` | CRM: galleries, studio, invoices, contracts, scheduling… |
| Public / client | `app/public/*` | Marketing site, galleries, portal, workspace, pay, booking |
| Money | `app/public/pay.py` + `app/admin/invoices.py` | Checkout + webhook vs draft/send |
| Templates | `templates/{site,public,admin}/` | Marketing / client docs / admin |
| Static | `static/` | `mise.css` (legacy layer) + Screening Room CSS/JS |

## Local setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest ruff httpx
# ffmpeg required for video smoke tests
cp .env.example .env   # set MISE_SECRET_KEY + MISE_ADMIN_PASSWORD at minimum
uvicorn app.main:app --reload --port 8400
```

Secrets and integration keys are documented in [`.env.example`](.env.example).
CI and local installs use **pip + `requirements.txt`** — do not commit `uv.lock`.

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
python -m pytest tests/ --ignore=tests/smoke --ignore=tests/test_smoke.py -q -m unit
# 2. integration — SQLite + TestClient seams
python -m pytest tests/ --ignore=tests/smoke --ignore=tests/test_smoke.py -q -m integration
# 3. full smoke — e2e against a throwaway DB (ffmpeg required for video tests)
# Domain slices live under tests/smoke/ (ordered test_01_… → test_07_…).
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/ -q -m smoke
# 4. lint + format
ruff check . && ruff format --check .
```

## Deploy

Canonical full-site deploy is **git pull on flow + restart mise**.
See [`ops/DEPLOY.md`](ops/DEPLOY.md).

`scripts/deploy-flow.sh` is a **specialty rsync slice** (Plutus/Argus/Platekit bits),
not the full-tree deploy path.

## Related docs

- [`AGENTS.md`](AGENTS.md) — agent permission boundaries
- [`ops/BACKUP.md`](ops/BACKUP.md) — nightly snapshot → mickey pull → restore-verify
- [`ops/DEPLOY.md`](ops/DEPLOY.md) — production deploy
- [`ops/SPECIALTY-LAUNCH.md`](ops/SPECIALTY-LAUNCH.md) — specialty / aerials launch
- [`HANDOFF.md`](HANDOFF.md) — historical refactor notes (prefer AGENTS.md + this README)
