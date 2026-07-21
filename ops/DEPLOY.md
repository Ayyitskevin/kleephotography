# Deploy Mise to flow

Canonical production tree: **flow `/opt/mise`** · `kleephotography.com` · `:8400`.

Working clones (laptops, CI, agent workspaces) are **not** production. Never
scratch-edit files under `/opt/mise` outside a deliberate deploy.

## Full-site deploy (normal path)

After `main` has the commit you want (pushed to GitHub):

```sh
ssh flow 'set -euo pipefail
  cd /opt/mise
  git fetch github
  git merge --ff-only github/main
  if systemctl is-active mise >/dev/null 2>&1; then
    sudo systemctl restart mise
  elif systemctl --user is-active mise >/dev/null 2>&1; then
    systemctl --user restart mise
  else
    echo "restart mise manually"; exit 1
  fi
  systemctl is-active mise || systemctl --user is-active mise
'
```

Flow’s remotes include `github` (GitHub) and `all` / `mickey` (bare mirror). Prefer
`github/main` when you just pushed to origin.

### Post-deploy spot checks

- `curl -fsS https://kleephotography.com/healthz`
- Home, one specialty spoke, `/portfolio`, `/admin` → login
- One gallery PIN page; browser console clear of CSP `Refused to execute…`
- Rollback look only: `MISE_SCREENING_ROOM=false` in flow `.env` + restart (no git revert)

Data rollback is the nightly backup chain — see [`BACKUP.md`](BACKUP.md).

## Specialty slice script (not full deploy)

[`scripts/deploy-flow.sh`](../scripts/deploy-flow.sh) rsyncs a **narrow** set of
files (Plutus/Argus/Platekit modules, one admin gallery template, `mise.css`,
migrations) then restarts mise. Use it only when you intentionally want that
surgical patch. **Do not** treat it as “how main reaches flow” for ordinary
template/app/static changes — those need the git pull path above.

Touching `scripts/deploy-flow.sh`, `mise.service`, or the backup chain is
**red-light** per [`AGENTS.md`](../AGENTS.md) (PR + human merge).

## Migrations on deploy

`db.migrate()` runs on app startup. Migrations are forward-only against the live
DB. Schema changes are red-light; never renumber already-applied migration files.
See [`MIGRATIONS.md`](MIGRATIONS.md).
