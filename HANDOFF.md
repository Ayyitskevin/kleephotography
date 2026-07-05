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

| Commit | What |
|---|---|
| d9793d2 | HANDOFF.md v1 |
| c12cfa0 | **Bug fix:** `/work/{slug}` overrode whole head block → lost fonts/site.js (dead mobile menu)/dark-mode/Plausible/JSON-LD **and** hero photo never rendered (block-scoped `{% set %}`). Restructured `base_site.html` head: new `meta_description` + `og` blocks, canonical link, og:title/desc now mirror each page. Smoke test pins regression. |
| b1e2098 | Per-page meta descriptions on all indexable pages (home, portfolio, work, services, about, contact, reels, press, book, book_event). |
| bd73d1d | Favicon: `static/favicon.svg`+`.ico`+`apple-touch-icon.png` (serif K + terracotta dot on cream), links in `base.html`, `/favicon.ico` route in site.py, unit test. |
| 5ffee23 | A11y: skip-link → `#main` wrapper (flex-preserving), hamburger `aria-expanded`/`aria-controls`, Escape closes + refocuses. Chromium-verified. |

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

### 6b. GREEN — open, high value first (verify code, fix, test, gate, commit)

- **[CONFIRMED·med] Contact form wipes all input on validation/throttle error**
  `app/public/site.py` error branches (~495-519) don't echo values; template only
  restores prefill.business/message/service. Reachable: `me@gmail` passes browser
  check, fails server dot-check. Echo submitted values back + template `value=` attrs.
- **[CONFIRMED·med] Lightbox not keyboard-openable** `static/lightbox.js:174-176`
  binds click on `<img>` in non-focusable `<figure>` (portfolio/home/work_detail/
  public gallery). Add tabindex/role=button/Enter+Space, or real `<button>` wrapper.
- **[CONFIRMED·med] Lightbox missing dialog semantics/focus mgmt/alt**
  `templates/site/_lightbox.html:1` no role=dialog/aria-modal; `open()` doesn't move
  focus, `close()` doesn't restore; `render()` sets no alt (copy from tile img).
- **[UNVERIFIED·med] Reels fallback leaks client-private video IDs as broken players**
  `app/public/site.py:279` `_portfolio_reels()` falls back to non-portfolio videos
  whose /site/vid+poster routes 404 ⇒ black players on / and /reels. Delete fallback,
  return [] (templates already have empty states). *Caution: check smoke tests that
  may rely on the fallback for the reels layout.*
- **[UNVERIFIED·med] Booking confirmation shows studio TZ, funnel sold visitor TZ**
  `booking_manage.html:19` + `public/scheduling.py:209` — render in `b.tz` when set.
- **[UNVERIFIED·med] Portals/workspaces link expired galleries → 410**
  `portal.py:62`, `workspace.py:74` — pass expired flag; unlink + "expired — get in touch".
- **[UNVERIFIED·med] ZIP wait page spins forever on failed build**
  `downloads.py:209` status only reports ready; report `failed` from jobs table;
  zip_wait.html show retry/contact message.
- **[UNVERIFIED·med] Fav toggle/video notes fail silently on expired cookie**
  add `htmx:responseError` 403→reload in gallery.html; error branch in lightbox.js post.
- **[minor·green·quick]** poster route missing expiry+ready gate `media.py:44-59`
  (mirror `_resolve`) · drop-gallery favorites/section download redirect loop
  `downloads.py:128,157` (use `_email_required(g) and not visitor["email"]`) ·
  `toggle_fav` works on expired galleries `gallery.py:142` · portal crop 404 while
  processing `portal.py:205` · masonry imgs lack width/height (CLS) `portfolio.html:32`
  (assets table has width/height cols) · lightbox arrows page through filtered-out
  tiles + chips lack aria-pressed `portfolio.html:75` · static files lack
  Cache-Control despite ?v= busting (`main.py` middleware: `/static/` →
  `public, max-age=31536000, immutable`) · press marquee 2nd loop needs
  aria-hidden `home.html:43` · about h1→h3 skip `about.html:44` · raw UTC timestamps
  on invoice/proposal (`invoice.html:66` use `|localtime`; contract.html is red-adjacent,
  leave) · internal pitch copy in client doc footers (`invoice.html:92`, proposal) ·
  book_index copy contradiction (instant vs follow-up) `book_index.html:8` ·
  expired.html says "get in touch" with no link.
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
- Task list state: #1 audit done · #2 pass-1 partially done (work_detail fix) ·
  #3 partially (nav a11y done; lightbox a11y open) · #4 partially (meta/canonical/
  favicon done; caching/CLS open) · #5 open · #6 open.
