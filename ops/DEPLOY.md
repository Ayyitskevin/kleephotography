# Deploy Mise to mickey

Canonical production tree: **mickey `/opt/mise`** · `kleephotography.com` · `:8400`
(loopback; public traffic via Cloudflare tunnel).

Host: **mickey** (`Strix-Halo-A9-Mega`, Tailscale `100.125.80.91`). Chosen as the
always-on node — flow is no longer the live origin.

Working clones (laptops, CI, agent workspaces) are **not** production. Never
scratch-edit files under `/opt/mise` outside a deliberate deploy.

## Full-site deploy (normal path)

After `main` has the commit you want:

1. Make sure mickey’s bare mirror is current (`/home/kevin-lee/git/mise.git` —
   that is `/opt/mise`’s `origin`), **or** add/use a `github` remote on the
   prod tree if you prefer fetching GitHub directly.
2. Pull + restart on mickey:

```sh
ssh mickey 'set -euo pipefail
  cd /opt/mise
  git fetch origin
  git merge --ff-only origin/main
  sudo systemctl restart mise
  systemctl is-active mise
'
```

If `/opt/mise` has a `github` remote instead (or in addition), prefer:

```sh
ssh mickey 'set -euo pipefail
  cd /opt/mise
  git fetch github
  git merge --ff-only github/main
  sudo systemctl restart mise
  systemctl is-active mise
'
```

`mise` runs as systemd unit `mise.service` (user `mise`, binds `127.0.0.1:8400`).
Restart needs sudo.

### Post-deploy spot checks

- `curl -fsS https://kleephotography.com/healthz`
- Home, one specialty spoke, `/portfolio`, `/admin` → login
- One gallery PIN page; browser console clear of CSP `Refused to execute…`
- Rollback look only: `MISE_SCREENING_ROOM=false` in mickey `/opt/mise/.env` +
  restart (no git revert)

Data rollback is the nightly backup chain — see [`BACKUP.md`](BACKUP.md).

## Specialty slice script (not full deploy)

[`scripts/deploy-flow.sh`](../scripts/deploy-flow.sh) is a **legacy-named** rsync
slice (Plutus/Argus/Platekit modules, one admin gallery template, `mise.css`,
migrations). It still defaults its SSH target via `MISE_FLOW_HOST` (set to
`mickey` when using it). Use it only for that surgical patch — **not** as “how
main reaches prod.” Ordinary template/app/static changes use the git pull path
above.

Touching `scripts/deploy-flow.sh`, `mise.service`, `ops/harden-flow.sh`, or the
backup chain is **red-light** per [`AGENTS.md`](../AGENTS.md) (PR + human merge).

## Migrations on deploy

`db.migrate()` runs on app startup. Migrations are forward-only against the live
DB. Schema changes are red-light; never renumber already-applied migration files.
See [`MIGRATIONS.md`](MIGRATIONS.md).
