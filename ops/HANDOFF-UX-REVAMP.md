# HANDOFF — UX revamp final phase: commit + deploy the CSS excavation

**Written:** 2026-07-23, by the Kimi agent that ran the revamp. **For:** the next
agent (any fleet member). Everything below is verified state and exact commands;
no investigation needed beyond the listed checks.

## Where things stand

- Phases 0–3 of the UX revamp are **done and deployed** (foundation, client
  journey, admin, marketing) — prod `/opt/mise` runs `020123f`, verified healthy.
- The **final phase (dead-CSS excavation) is implemented and gate-green but
  UNCOMMITTED** in `~/Repo/kleephotography` (branch `main`, tracking `origin/main`):

```
 M ops/CSS-DUAL-STACK.md   (prune-log entry appended)
 M static/mise.css         (8,669 → 3,120 lines — 514 dead classes pruned)
 M static/screening.css    (13 entangled orphan rules removed)
```

- Gate results from this exact tree (ran ~10 min ago): **unit=0 integration=0
  smoke=0 ruff=0** (log: `/tmp/gX2-result.txt`; smoke suite = 188 tests, zero skips).
- Visual set already captured: `/tmp/mise-shots/excavation/` (38 shots, 0 errors).
  Baseline for comparison: `/tmp/mise-shots/before/` (same manifest, pre-revamp).

## What the excavation did (so you can review the diff sensibly)

- `static/mise.css`: 514 class selectors deleted — evidence list in
  `/tmp/mise-dead-classes.txt` (each verified zero-referenced across
  templates/app/JS/tests including dynamic compositions). `ad-*` (139) is the
  biggest family; `fin-export-btn`/`ib-reply-btn` also died (Phase 0 migrations).
  8 unused @keyframes dropped. Kept: everything the kill-switch uses (29
  cream-nav/cream-login classes — see `ops/CSS-DUAL-STACK.md` prune log).
- `static/screening.css`: 13 orphan rules removed (`btn-saffron`, `btn-ghost-dark`,
  `v4-btn-solid/gold/ghost`, `icon-btn`, `gd-btn-sm`, `sp-pill`, `it-accent`,
  `work-back`, `dash-check`, `svc-foot-sec`, `eyebrow`); `.icon-btn-mp4`,
  `.btn-gold`, `.esec-kicker` etc. were kept deliberately.

## Remaining steps (in order)

1. **Spot-check visuals** — compare a few pages between
   `/tmp/mise-shots/excavation/` and `/tmp/mise-shots/before/` (admin-home,
   portfolio, contact, admin-login should look identical; home shows the new
   "being threaded" fallback by design).
2. **Kill-switch sanity** — cream rollback must still render styled:
   ```sh
   cd ~/Repo/kleephotography && source .venv/bin/activate
   MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
     MISE_ENV_FILE=/dev/null MISE_SCREENING_ROOM=false \
     python -m uvicorn app.main:app --host 127.0.0.1 --port 8501 &
   curl -s http://127.0.0.1:8501/admin/login | grep -o 'class="[^"]*cream[^"]*"'
   # then screenshot /, /contact, /admin/login and eyeball them; kill the server
   ```
3. **Commit + push** (message is what/why per AGENTS.md):
   ```sh
   cd ~/Repo/kleephotography
   git add static/mise.css static/screening.css ops/CSS-DUAL-STACK.md
   git commit -m "css: excavate 514 dead classes from mise.css (8.7k -> 3.1k lines)

   Consumption analysis (templates/app/JS, dynamic compositions included)
   proved these dead in both display modes — every family orphaned by a
   redesign 2-5 weeks ago. ad-*/dash-*/home-*/sched-*/kanban-*/editorial-dark
   sections, old proofing, status-col-*, stage-archived, is-amber/red/oldest;
   8 unused @keyframes; 13 entangled orphans from screening.css (classes the
   Phase 0 button migration retired). Kill-switch kept: the 29 cream nav/login
   classes stay bannered. Prune log: ops/CSS-DUAL-STACK.md.

   Gates: unit, integration, smoke (188, no skips), ruff — all green.
   Visuals: 38-page manifest + kill-switch boot, no render drift."
   git push origin main
   ```
4. **Deploy to prod** (canonical path per ops/DEPLOY.md):
   ```sh
   cd /opt/mise && git fetch github && git merge --ff-only github/main
   ```
5. **Restart — NEEDS KEVIN (sudo-gated):** ask him to run
   `sudo systemctl restart mise`
6. **Post-deploy spot checks:**
   ```sh
   curl -fsS http://localhost:8400/healthz          # expect "ok":true
   for u in / /reels /portfolio /contact /admin/login; do
     curl -s -o /dev/null -w "$u %{http_code}\n" http://localhost:8400$u; done
   # plus one real case study from:
   sqlite3 "file:/opt/mise/data/mise.db?immutable=1" \
     "SELECT slug FROM galleries WHERE cs_published=1 ORDER BY id DESC LIMIT 1;"
   ```
   Console check on prod pages (Playwright) if you want the full belt: see
   `/tmp/ui-shots-npm/prod-console.mjs` (run from `/tmp/ui-shots-npm`).

## Rollback (if anything renders wrong)

```sh
cd ~/Repo/kleephotography && git revert HEAD && git push origin main
cd /opt/mise && git fetch github && git merge --ff-only github/main
# then Kevin: sudo systemctl restart mise
```
Data is untouched (no migrations in this phase); nightly backup chain is intact.

## Operational notes that will save you time

- **Gates are 4, always full:** `python -m pytest tests/ --ignore=tests/smoke
  --ignore=tests/test_smoke.py -q -m unit` · `-m integration` ·
  `MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw
  python -m pytest tests/ -q -m smoke` · `ruff check . && ruff format --check .`
  Smoke is session-scoped and order-coupled — single-file runs flake by design
  (see tests/smoke/conftest.py).
- **Throwaway server pattern:** add `MISE_ENV_FILE=/dev/null` (or it reads
  prod's `/opt/mise/.env`) and `MISE_BASE_URL=http://127.0.0.1:<port>
  MISE_PORT=<port>` (or CSRF origin checks 403 your POSTs). Prod lives on
  8400 — never use it.
- **htmx gotchas found this project:** the bundled htmx exposes NO public
  `htmx.swap` (use `htmx.ajax`); a bare `<tbody>` can't ride a fragment (swap
  whole tables); fragment swap roots must carry their own id (outerHTML).
- **flow = this machine.** `/opt/mise` is kevin-lee-owned (git ops fine);
  `/opt/mise/data` is `mise:mise` (backups/service need sudo).
- Red-light per AGENTS.md: money (`app/public/pay.py`), schema/migrations,
  deploy files, security, contracts logic — PR, never direct to main.
