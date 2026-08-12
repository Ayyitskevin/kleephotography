# Deploy Mise to production

Canonical production tree: **`<prod-host>` `/opt/mise`** · `kleephotography.com`
(uvicorn on loopback; public traffic via Cloudflare tunnel).

Host: **`<prod-host>`** — the always-on node, reachable over the operator's private
network only. Its real name, address, and SSH alias live in the operator's private
ops notes, not in this repo; substitute your own everywhere `<prod-host>` appears.

Working clones (laptops, CI, agent workspaces) are **not** production. Never
scratch-edit files under `/opt/mise` outside a deliberate deploy.

## Full-site deploy (normal path)

After `main` has the commit you want:

1. Make sure the remote the prod tree pulls from is current — either the operator's
   bare mirror (the prod tree's `origin`), **or** add/use a `github` remote on the
   prod tree if you prefer fetching GitHub directly.
2. Pull + restart on the prod host:

```sh
ssh <prod-host> 'set -euo pipefail
  cd /opt/mise
  git fetch origin
  git merge --ff-only origin/main
  sudo systemctl restart mise
  systemctl is-active mise
'
```

If `/opt/mise` has a `github` remote instead (or in addition), prefer:

```sh
ssh <prod-host> 'set -euo pipefail
  cd /opt/mise
  git fetch github
  git merge --ff-only github/main
  sudo systemctl restart mise
  systemctl is-active mise
'
```

`mise` runs as systemd unit `mise.service` (user `mise`, binds loopback — see
[`../mise.service`](../mise.service)). Restart needs sudo.

### Post-deploy spot checks

- `curl -fsS https://kleephotography.com/healthz` → `{"ok": true}`. For the
  detail (disk, backup age, queue depth) add
  `-H "Authorization: Bearer $MISE_HEALTHZ_TOKEN"` — see
  [`MONITORING.md`](MONITORING.md).
- Home, one specialty spoke, `/portfolio`, `/admin` → login
- One gallery PIN page; browser console clear of CSP `Refused to execute…`
- Rollback look only: `MISE_SCREENING_ROOM=false` in the prod `/opt/mise/.env` +
  restart (no git revert)

Data rollback is the nightly backup chain — see [`BACKUP.md`](BACKUP.md).

## Specialty slice script (not full deploy)

[`scripts/deploy-flow.sh`](../scripts/deploy-flow.sh) is a **legacy-named** rsync
slice (Plutus/Argus/Platekit modules, one admin gallery template, `mise.css`). It
takes its SSH target from `MISE_FLOW_HOST` — set that explicitly before running it,
there is no default, so a typo cannot ship to the wrong box. Use it only for that
surgical patch — **not** as “how main reaches prod.” Ordinary template/app/static
changes use the git pull path above.

It **refuses to run** from a dirty tree, or from a HEAD not yet merged into
`origin/main`: rsync overwrites live files with whatever is in your clone, so a stale
branch or a forgotten local edit would otherwise reach production with no review and
no record. `MISE_FLOW_ALLOW_UNMERGED=yes` overrides it, loudly, for an incident.

**It no longer ships `migrations/`, and must not be made to.** The restart runs
`db.migrate()`, so any `.sql` landing there is applied to the live database
immediately and irreversibly. The staleness guard above does not make that safe,
because the danger is not only staleness: this script ships three Python files, so a
migration sent this way moves the **schema without the code that expects it** — 071
renames `admin_sessions.token`, which would break admin login on the spot. Schema
changes reach production through git pull + restart, where code and schema move
together. See [`MIGRATIONS.md`](MIGRATIONS.md).

Touching `scripts/deploy-flow.sh`, `mise.service`, host hardening, or the backup chain
is **red-light** per [`AGENTS.md`](../AGENTS.md) (PR + human merge).

## Open design question

[`DB-CONNECTIONS.md`](DB-CONNECTIONS.md) measures what `db.one`/`all_`/`run`
cost by opening a connection per statement (~150 ms of a 60-photo gallery load,
growing with every migration) and lays out the options. Nothing is implemented —
it is a decision waiting on a measurement from the prod host.

## Migrations on deploy

`db.migrate()` runs on app startup. Migrations are forward-only against the live
DB. Schema changes are red-light; never renumber already-applied migration files.
See [`MIGRATIONS.md`](MIGRATIONS.md).
