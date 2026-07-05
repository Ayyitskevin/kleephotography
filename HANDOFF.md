# HANDOFF — Klee Photography / Mise refactor (retirement notes)

**For the successor agent (Opus 4.8 / Sonnet 5 / any):** the previous agent (Fable 5)
retired mid-effort. This file is the single source of truth. Read it top-to-bottom,
then continue from [§7 What remains](#7-what-remains-ordered-work-queue). Update this
file as you work. **All prior context you need is here — do not assume chat history.**

---

## 1. Mission & ground rules

Refactor/improve the whole codebase and leave kleephotography.com ready to ship —
without breaking galleries, admin, contracts, invoices, Stripe, uploads, or SEO.

- **Read `AGENTS.md` first. It overrides everything**, including this file.
- Green-light (fix autonomously): templates/CSS/HTMX, public copy, non-money admin
  features, tests, docs, surgical refactors, dep bumps that pass the suite.
- Red-light (document; PR for Kevin; NEVER self-merge): `app/public/pay.py`/Stripe/
  invoice-payment math+state; `migrations/`+schema; deploy files (`scripts/deploy-flow.sh`,
  `mise.service`, `ops/backup.sh`, flow tree); `app/security.py`, `app/admin/auth.py`,
  CSRF/session/cookie/rate-limit/lockout/secrets; contracts/e-sign. Unsure ⇒ red.
- **Session override:** all work goes to branch `claude/klee-photography-refactor-y9tr5g`
  (platform constraint; do not push to main or any other branch). Delivered via
  **draft PR #2** (https://github.com/Ayyitskevin/kleephotography/pull/2) — Kevin merges.
  Keep red-light *changes* out of the diff; they are documented in §6 and the PR body.
- SQL: bound `?` placeholders only. Studio date logic: `studio._today()`. Conform to
  existing style. One logical change per commit (`area: what — why`). No JS build.
- Never commit secrets/.env/client media/dumps. Never touch `/opt/mise` (unreachable
  here anyway). No model IDs in commits/PR bodies.

## 2. Environment setup (fresh container)

```sh
cd /home/user/kleephotography && git checkout claude/klee-photography-refactor-y9tr5g
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest ruff httpx
apt-get update -q; apt-get install -y ffmpeg     # REQUIRED for video smoke tests
```

## 3. Gates — ALL must pass before EVERY commit

```sh
source .venv/bin/activate
python -m pytest tests/ --ignore=tests/test_smoke.py -q -m unit
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/test_smoke.py -q
ruff check . && ruff format --check .
```

Current state (2026-07-05, commit 5ffee23): **37 unit / 158 smoke / ruff clean.**
CI (`.github/workflows/ci.yml`) runs the same on the PR. If smoke fails fresh with
FileNotFoundError on video tests ⇒ ffmpeg missing, not a code bug.

Manual verification harness (already used, reuse it): scratchpad dir has a Playwright
setup — `cd <scratchpad>/ && npm install playwright --no-save`, launch chromium with
`executablePath: '/opt/pw-browsers/chromium'`; run uvicorn with
`MISE_DATA_DIR=<tmp> MISE_SECRET_KEY=devtest MISE_ADMIN_PASSWORD=pw` (showcase
auto-seeds demo data on first boot).

## 4. Codebase map

- `app/main.py` wiring/CSP/error pages · `app/db.py` SQLite helpers (`one`,`all_`,`run`,
  `get_or_404`,`tx`) · `app/render.py` Jinja env+filters (`portfolio_alt`,`localtime`,`usd`).
- `app/public/`: `site.py` marketing (+robots/sitemap/favicon), `gallery.py` PIN'd
  galleries+proofing+video comments, `portal.py`, `media.py`, `downloads.py` (ZIPs),
  `docs.py` (public invoice/contract/proposal), `pay.py` (RED Stripe), `scheduling.py`
  booking, `workspace.py`, `forms.py`, `sms_webhook.py`.
- `app/admin/`: one router per feature; `common.py` shared. Biggest: `studio.py` 1499,
  `galleries.py` 889, `activity.py` 829 lines.
- `templates/site/` marketing (base: `site/base_site.html` — has `meta_description`
  and `og` blocks; pages override them) · `templates/public/` client-facing (base:
  `base.html`/`base_cream.html`, noindex by default) · `templates/admin/`.
- `static/mise.css` (single 305KB sheet — includes admin), `site.js`, `lightbox.js`.
- Jinja gotcha that already bit once: `{% set %}` inside a block is INVISIBLE to
  sibling blocks — set page-level vars at template top level.

## 5. Work completed (all gates green at each commit)

**Round 1 — PR #2 (MERGED to main):**
| Commit | What |
|---|---|
| c12cfa0 | **Bug fix:** `/work/{slug}` overrode whole head block → lost fonts/site.js (dead mobile menu)/dark-mode/Plausible/JSON-LD **and** hero photo never rendered (block-scoped `{% set %}`). Restructured `base_site.html` head: new `meta_description` + `og` blocks, canonical link, og:title/desc now mirror each page. |
| b1e2098 | Per-page meta descriptions on all indexable pages. |
| bd73d1d | Favicon: SVG+ICO+apple-touch-icon, links in `base.html`, `/favicon.ico` route, unit test. |
| 5ffee23 | A11y: skip-link → `#main` wrapper, hamburger aria-expanded/controls, Escape closes+refocuses. |

**Round 2 — PR #3 (MERGED to main):** contact-form value echo on error; lightbox keyboard
access + dialog semantics + focus mgmt + alt; reels demo-fallback removal (was leaking
client-private video IDs as broken players); portal/workspace expired-gallery unlinking.

**Round 3+ — PR #4 (OPEN, draft):** ZIP-wait failed-build UX; fav/note silent-failure on
expired session; booking confirmation in client TZ (`localtime` filter gained `tz` arg);
expiry enforced on fav-toggle + video-poster routes; `/static` immutable caching;
portfolio CLS width/height; press-marquee aria-hidden + about h1→h2; expired-gallery
contact link + booking-copy contradiction fix; **drop-gallery favorites/section infinite
redirect-loop fix**. All gate-green; smoke suite now 164 passed.

**IMPORTANT for successor:** after each PR merges, `git fetch origin main` and
`git checkout -B claude/klee-photography-refactor-y9tr5g origin/main` before continuing —
follow-up work is a fresh change on a fresh base, delivered via a NEW draft PR (the
merged PR is finished and must not be reused). Commit-msg tip: avoid backticks in
`git commit -m` (shell eats them); use `-F -` heredoc for messages with code.

An 8-dimension multi-agent audit ran (security, public-UI/SEO, client-flows, admin-UX,
code-quality complete; **performance, features, tests audits DID NOT RUN** — usage
limits). Findings below. Verification status: CONFIRMED = adversarially verified;
UNVERIFIED = single-auditor claim, re-verify the code before fixing.

## 6. Audit findings

### 6a. RED-LIGHT — document in PR only; Kevin decides (NO code changes)

1. **[CONFIRMED·med] Portal PIN-lockout buckets collide with inquiry-throttle sentinels**
   `app/public/portal.py:151` uses bucket `-p["id"]`; portals 2/3/4 collide with
   `security.py:94-96` sentinels −2/−3/−4 (contact/book/forms throttles). 3 portal-PIN
   typos ⇒ /contact 429s for that IP; successful portal login wipes contact throttle;
   spurious Telegram alerts. Fix pattern exists: `workspace.py:24` PIN_OFFSET=2_000_000.
2. **[CONFIRMED·med] Admin session = irrevocable signed constant, 90 days**
   `security.py:191`; logout deletes only the browser cookie; revocation requires
   rotating MISE_SECRET_KEY (kills all client cookies too). Fix: server-side session
   token table (also needs migration ⇒ doubly red).
3. **[UNVERIFIED·med] COOKIE_SECURE defaults false** `app/config.py:243` + `.env.example`
   ships false; live site is HTTPS. Fix: default true or derive from BASE_URL. **Also:
   Kevin should check flow's `.env` has `MISE_COOKIE_SECURE=true` today.**
4. **[UNVERIFIED·med] No per-target (cross-IP) PIN attempt cap** `security.py:54` —
   distributed guessing of 4-digit PINs is unbounded; alerting is per-IP only.
5. **[UNVERIFIED·high] Stripe success return ignored** `app/public/pay.py:123` sets
   `success_url=/i/{slug}?thanks=1` but `view_invoice` never reads `thanks`; client
   returns from Checkout to a stale invoice with a live Pay button until the webhook
   lands (days for ACH) — can double-open Checkout sessions.
6. **[minor·red] Rate limiter exempts `/c/`, `/w/`, `/t/`** `app/ratelimit.py:34`.
7. **[minor·red] `check_admin_password` TypeError→500 on non-ASCII password**
   `security.py:204` — compare `.encode()` bytes; also skips lockout bookkeeping.

### 6b. GREEN — ✅ DONE (rounds 2–4, PR #3 merged / PR #4 open)

Contact-form value echo · lightbox keyboard+dialog+focus+alt · reels fallback removal ·
booking confirmation client-TZ · portal/workspace expired-gallery unlink · ZIP-wait
failed-build UX · fav/note silent-failure on expired session · poster+fav expiry 410
gate · drop-gallery favorites/section redirect-loop · `/static` immutable caching ·
portfolio CLS width/height · press-marquee aria-hidden · about h1→h2 · expired.html
contact link · book_index copy contradiction. Each has a smoke/unit test.

### 6b-remaining. GREEN — still open (verify code, fix, test, gate, commit)

- **[minor·green]** portal crop links 404 while the crop is still processing
  `portal.py:205` — check crop file existence per asset, render unready ratios as a
  muted "processing…" span (or return a friendlier "still preparing" from the route).
- **[minor·green]** lightbox arrows page through filtered-out portfolio tiles + filter
  chips lack `aria-pressed` `portfolio.html` — constrain the lightbox `tiles` array to
  non-`.pf-hidden`, toggle aria-pressed in the chips' apply().
- **[HELD — red-adjacent, do NOT auto-edit]** invoice/proposal client-document footer
  copy ("no extra portal friction") + raw-UTC `paid_at`/`accepted_at` display
  (`invoice.html:66,92`, `proposal.html:58,79`). Display-only and audit-classified
  green, but they render on financial/legal client documents — recommend but leave to
  Kevin (contract.html signed_at is firmly red-light). Documented in PR body.
- **Admin (all UNVERIFIED, verify first):** financials CSV "Include Paid" checkbox
  can't uncheck (`financials.py:210` — likely missing unchecked-checkbox handling) ·
  scheduling date-override backend has no UI (`admin/scheduling.html:75`) · gallery
  section "remove" has no confirm (`admin/gallery.html:557`) · upload UI says success
  when all files rejected (`admin/gallery.html:635`) · activity page ghost-renders
  missing gallery (`activity.py:535` — add get_or_404) · inbox 100-thread cap,
  ?sel deep-link mismatch (`inbox.py:208`) · gallery delete lands on Home not
  library (`galleries.py:757`) · studio Archived column always 0 (`studio.html:51`) ·
  galleries.py:81 computes context the template never renders (dead code) ·
  /admin/emails unpaginated (`activity.py:427`) · "Photos" tile counts all assets
  (`admin/gallery.html:53`).
- **Code quality (UNVERIFIED):** financial/report date boundaries bypass `_today()`
  (`financials.py:60`, reports) · `admin/common.today()` dead — remove after grep ·
  inquiry→client find-or-create duplicated (`studio.py:675` ×2) · video-comment
  thread query duplicated (`galleries.py:308` vs `gallery.video_comment_thread`) ·
  dead feature flags (`features.py:38`) · chunked upload-save loop ×4
  (`uploads.py:58`) · hand-rolled one()+404 sites → `db.get_or_404`
  (`admin/scheduling.py:77` etc).

### 6c. Not audited (agents never ran)

Performance, features-completeness, and tests dimensions. If capacity allows, sweep:
imaging/jobs hot paths, N+1s in admin lists/studio dashboard, `test_pin_lockout`-style
ordering brittleness in test_smoke.py (it reads `ORDER BY id DESC LIMIT 1` galleries
created by earlier tests), TODO/FIXME grep, mailer/gcal/notion failure modes.

## 7. What remains (ordered work queue)

1. **Green fixes from §6b, top-down** (confirmed first, then verify-and-fix the
   unverified ones). Small commits; add/extend a test per meaningful fix; gates every time.
2. Push after every 2-3 commits: `git push -u origin claude/klee-photography-refactor-y9tr5g`.
3. **Keep PR #2 body current**: red-light table (§6a), change list, tests run.
4. §6c sweeps if capacity remains.
5. Finalize: gates → push → PR body final (summary, risk, tests, manual verification,
   rollback, red-light list) → flip PR from draft only when Kevin asks.
6. **Deploy is BLOCKED from this environment** (no access to flow). Kevin deploys
   after merge via existing `scripts/deploy-flow.sh` (do not modify). Post-deploy
   checks: `curl https://kleephotography.com/healthz` · spot / , /work , /work/{slug}
   (fonts+menu+hero now load there — verify!), /services, /contact, /book · /admin
   303→login · one gallery PIN page. Rollback: revert merge on main, redeploy;
   nightly DB snapshots per ops/BACKUP.md (untouched by this work).

## 8. Operational notes

- PR #2 has an activity subscription: CI failures/review comments arrive as webhook
  events — investigate, fix if small+clear, ask Kevin if ambiguous. A `send_later`
  self check-in re-arms hourly; re-arm silently if nothing changed; stop when merged/closed.
- Usage limits were hitting at retirement (subagent fan-outs failed). Prefer inline
  work over multi-agent workflows until limits reset.
- Progress: PR #2 merged (SEO/favicon/nav-a11y/work_detail bug). PR #3 merged
  (contact echo, lightbox a11y, reels, portal/workspace expiry). PR #4 open with 9
  commits (client-flow correctness + perf/a11y/copy). Full smoke suite: 164 passed.
- Remaining green work: §6b-remaining (portal-crop-processing, portfolio filter+lightbox
  aria-pressed), §6c sweeps (perf N+1s, test-ordering brittleness, features), and the
  admin/code-quality UNVERIFIED lists (verify each before touching). The two
  ordering-brittle tests (`test_expired_gallery`, `test_gallery_notion_writeback`) fail
  under `-k` subsets because they read the newest gallery/project from earlier tests —
  a good §6c test-hardening target (make them self-sufficient).
- Deploy remains BLOCKED from this env (no flow access) — Kevin deploys merged main via
  `scripts/deploy-flow.sh`; post-deploy spot-check `/work/{slug}` (fonts+menu+hero).
