# HANDOFF — Klee Photography / Mise refactor session

**Purpose of this file:** if the current agent session is cut off, a successor agent
(Opus/Sonnet/any) must be able to resume from here with zero other context. Read this
top-to-bottom, then continue from the first unchecked item in [Progress log](#progress-log).
Update this file as you complete work — it is the single source of truth for this effort.

---

## 0. Mission

Refactor and improve the whole codebase, then leave the repo ready to ship a safer,
cleaner, faster, more polished kleephotography.com — **without breaking** client
galleries, admin workflows, contracts, invoices, Stripe payments, uploads, or SEO.

## 1. Ground rules (read AGENTS.md first — it overrides everything)

- This is **live production** with real clients and real Stripe money.
- `AGENTS.md` defines **green-light** (fix autonomously) vs **red-light** (branch + PR,
  human merges). Digest:
  - **Green:** refactors, dead code, tests, UI/templates/CSS/HTMX, public copy,
    non-money admin features (galleries, proofing, shotlist, presets, press, licenses),
    tooling/docs, dep bumps that pass the suite.
  - **Red (DO NOT change without PR approval):** `app/public/pay.py` + Stripe/webhook/
    invoice/payment math & state; `migrations/` + any schema change; deploy
    (`scripts/deploy-flow.sh`, `mise.service`, `ops/backup.sh`, flow tree, systemd);
    `app/security.py`, `app/admin/auth.py`, CSRF/session/cookie, rate-limit/lockout,
    secrets; proposal/contract generation & e-sign.
  - When unsure, treat as red.
- **Session-specific override:** this session's platform contract designates branch
  `claude/klee-photography-refactor-y9tr5g` and forbids pushing to any other branch.
  So (deviating from AGENTS.md's "push green to main"): **all work — green and red — is
  committed to `claude/klee-photography-refactor-y9tr5g`**, pushed there, and delivered
  via ONE draft PR that Kevin reviews and merges. Keep red-light changes OUT of the
  diff entirely: red-light findings are *documented* (below + PR body), not implemented,
  unless they are tests/docs-only.
- Never commit secrets, .env, client media, DB dumps. Never scratch-edit `/opt/mise`
  (production host `flow` — not reachable from this environment anyway).
- SQL: bound `?` placeholders only. Date logic: `studio._today()`, not `date.today()`.
- Style: conform to the existing codebase; surgical one-logical-change commits;
  no speculative abstractions; keep FastAPI + Jinja + HTMX; **no JS build**.

## 2. Environment setup (fresh container)

```sh
cd /home/user/kleephotography          # or wherever the repo is cloned
git checkout claude/klee-photography-refactor-y9tr5g
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest ruff httpx
# smoke video tests need ffmpeg:
apt-get update -q; apt-get install -y ffmpeg
```

## 3. Gates — run ALL before every commit; never push red

```sh
source .venv/bin/activate
python -m pytest tests/ --ignore=tests/test_smoke.py -q -m unit          # gate 1
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/test_smoke.py -q                                # gate 2
ruff check . && ruff format --check .                                    # gate 3
```

Baseline recorded 2026-07-05 (commit 0b72e6b): **36 unit passed · 158 smoke passed ·
ruff clean**. If gates fail before you changed anything, it's the environment
(usually missing ffmpeg), not the code.

## 4. Codebase map (30-second orientation)

- `app/main.py` — app wiring, CSP + security-header middleware, branded error pages.
- `app/db.py` — SQLite helpers (`one`, `all_`, `run`, `get_or_404`, `migrate`).
- `app/security.py` (RED) — cookies, PIN lockout, admin session. `app/csrf.py` (RED) —
  same-origin check middleware. `app/ratelimit.py` (RED).
- `app/public/` — public routes: `site.py` (marketing pages), `gallery.py` (PIN'd client
  galleries), `portal.py`, `media.py` (image serving), `downloads.py` (ZIPs), `pay.py`
  (RED — Stripe), `docs.py` (public invoice/contract/proposal views), `forms.py`,
  `scheduling.py` (booking), `workspace.py`, `sms_webhook.py`.
- `app/admin/` — back office, one router per feature; `common.py` shared helpers.
  Biggest: `studio.py` (1499), `galleries.py` (889), `activity.py` (829).
- `templates/site/` marketing · `templates/public/` client-facing · `templates/admin/`
  back office. Base layouts: `site/base_site.html`, `base_cream.html`,
  `admin/base_admin.html`.
- `static/` — `mise.css` (single stylesheet), `site.js`, `lightbox.js`, `htmx.min.js`,
  self-hosted fonts.
- `tests/` — `-m unit` fast tests + `test_smoke.py` full e2e (needs env vars + ffmpeg).
- `migrations/` (RED) — forward-only SQL, run against live DB.

## 5. The plan, beginning to end

Work in **passes**; each pass = several small commits; gates before every commit;
push after each pass so nothing is lost. Update the [Progress log](#progress-log)
as you go.

### Pass 0 — setup & audit  ✅ done except where noted
1. Read AGENTS.md, map repo, set up venv, record gate baseline. ✅
2. Run a comprehensive audit (security / public UI+SEO / client flows / admin UX /
   code quality / performance / features / tests). ✅ — **verified findings are
   recorded in section 6 below**; implement from that list.
3. Commit this HANDOFF.md, push branch, open draft PR early (durability). ✅

### Pass 1 — safety & correctness (green-light only)
Fix verified correctness bugs from the audit findings (section 6) that sit in
green-light territory: template escaping bugs, broken routes/templates, unhandled
None/404 paths, HTMX endpoints that fail silently, upload-validation gaps *outside*
`app/security.py`, error-state gaps. Each fix gets/adjusts a test where meaningful.

### Pass 2 — UI/UX + accessibility (green-light)
Public marketing site + client-facing templates: mobile nav, form labels +
validation feedback, focus states, alt text, heading hierarchy, skip links,
keyboard paths (lightbox, menus), empty states, 404/500 polish, favicon/icons.

### Pass 3 — feature polish + SEO/social + performance (green-light)
Titles/meta descriptions/canonical/OpenGraph/Twitter cards, robots + sitemap,
structured data, `loading=lazy` + width/height on images, font preload, cache
headers on static/media where safe (code-level, not schema), copy polish,
perceived-speed (HTMX indicators).

### Pass 4 — code cleanup + tests (green-light)
Dead code removal, duplication collapse *with real payoff only*, import hygiene,
DB-helper adoption (`get_or_404` etc.) where clearly intended, flaky/ordering test
fixes (e.g. `test_pin_lockout` depends on newest-gallery state from earlier tests),
new coverage for anything touched above.

### Pass 5 — red-light documentation (no code changes)
For every red-light finding in section 6: write it up in the PR body with severity,
evidence (file:line), proposed fix, risk, and rollback. Tests/docs-only additions
for red-light areas are allowed (they don't change behavior).

### Pass 6 — finalize
1. Re-run all gates on the final tree.
2. `git push -u origin claude/klee-photography-refactor-y9tr5g` (retry w/ backoff on
   network errors).
3. Ensure the ONE draft PR exists and its body contains: summary; green-light change
   list; red-light findings needing Kevin's decision; tests run + output summary;
   manual verification notes; rollback plan (`git revert` of the merge, plus nightly
   DB snapshot chain per `ops/BACKUP.md`); deploy checklist (below).
4. Final report to Kevin in chat (executive summary, what changed, findings, deploy
   status, rollback, red-light items).

### Deploy (BLOCKED from this environment — leave instructions only)
This container has no access to the production host (`flow:/opt/mise`). Deploy is
Kevin's step after merging the PR, using the repo's established process
(`scripts/deploy-flow.sh` — DO NOT modify it). Post-deploy verification for Kevin:
```sh
curl -s https://kleephotography.com/healthz          # {"ok": true, ...}
# spot-check: / , /work , /services , /about , /contact , /book
# admin: /admin -> redirects to /admin/login
# one gallery PIN page loads (no client data changes)
```
Rollback: `git revert` the merge commit on main, redeploy via the same script;
data is recoverable to the last nightly snapshot (see ops/BACKUP.md) — but nothing
in this effort should touch data or schema.

## 6. Audit findings (fill in / consume as work proceeds)

> Populated from the verified multi-agent audit. Status: ☐ open · ☑ fixed (commit) ·
> ✗ rejected (why) · ⚠ red-light (document only).

*(pending — audit workflow was still running when this file was first committed;
successor: if this section is still empty, re-run the audit or proceed with manual
inspection using the pass structure above)*

## 7. Progress log

- [x] 2026-07-05 — AGENTS.md read; repo mapped; venv built; ffmpeg installed.
- [x] 2026-07-05 — Baseline gates green: 36 unit / 158 smoke / ruff clean @ 0b72e6b.
- [x] 2026-07-05 — 8-dimension audit workflow launched (results → section 6).
- [ ] HANDOFF.md committed; branch pushed; draft PR opened.
- [ ] Pass 1 — safety & correctness.
- [ ] Pass 2 — UI/UX + accessibility.
- [ ] Pass 3 — SEO/social/meta + performance polish.
- [ ] Pass 4 — code cleanup + tests.
- [ ] Pass 5 — red-light write-ups in PR body.
- [ ] Pass 6 — final gates, push, PR finalized, report delivered.

## 8. Commit conventions

- One logical change per commit; message = `area: what — why` (match existing log,
  e.g. `admin: make multi-write delete/reorder/mark-sent routes atomic`).
- No model IDs in commit messages/PR bodies. Do not add Generated-by trailers beyond
  what the platform requires.
- Never `git push --force`. Never push to any branch other than
  `claude/klee-photography-refactor-y9tr5g`.
