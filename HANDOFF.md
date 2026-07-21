# HANDOFF — Klee Photography / Mise refactor (historical notes)

**Prefer [`AGENTS.md`](AGENTS.md) + [`README.md`](README.md) + [`ops/`](ops/) over this
file.** HANDOFF archives the mid-2026 refactor / Screening Room / truthful-HTTPS work.
Do not treat [§7](#7-what-remains-ordered-work-queue) as an active autonomous queue.

**Standing truths:**

- §6a security red-lights are **fixed** on main.
- **Full-site deploy** = git pull on flow `/opt/mise` + restart — [`ops/DEPLOY.md`](ops/DEPLOY.md).
  `scripts/deploy-flow.sh` is a specialty rsync slice only.
- Screening Room / Aerials kill switches: `.env.example`. Migrations policy: [`ops/MIGRATIONS.md`](ops/MIGRATIONS.md).
- Do not start leftover §6b / §6c items without Kevin's explicit ask.

---

## CURRENT STATUS 2026-07-17 — Truthful HTTPS baseline (RED, verified candidate)

The current work supersedes older statements below that a fresh boot should
auto-publish showcase proof. Branch `claude/truthful-https-baseline` is based on
GitHub `main` at `522ef6c` and is intentionally human-gated because it contains
migration 068 and changes canonical transport behavior.

- Removes startup code that could publish invented prototype testimonials,
  case-study copy, and portfolio flags; migration 068 unpublishes exact invented
  content even after attribution edits or CRLF normalization while preserving
  rows for audit. Legacy live-publish scripts now fail loud.
- Removes unsupported Review JSON-LD and prevents general food-and-beverage
  quotes from being reused as real-estate or portrait proof.
- Adds an optional canonical-origin guard: GET/HEAD receive rollback-safe 308s,
  unsafe noncanonical methods receive 421 without replay, and opaque/malformed
  browser Origins are rejected. Private `/healthz` and bearer `/api/*` access
  remain available.
- Activation and rollback are in `ops/TRUTHFUL-HTTPS.md`. No Cloudflare, Flow,
  production database, deploy, or live content mutation belongs in this branch.
- Human gates still open: live proof/release inventory, fresh verified backup,
  Kevin review/merge, Cloudflare rules, Flow deploy, and post-deploy smoke.

Local acceptance: 61 unit, 87 integration, 185 smoke, Ruff check/format, and
`git diff --check` pass; exact-SHA GitHub Actions remains the publication gate. Preserve payment and
draft-contract operational holds from the 2026-07-15 Codex review.

## 0. STATUS 2026-07-11 — SCREENING ROOM full-platform redesign (this branch)

Kevin approved the **Screening Room** redesign (design handoff:
`design_handoff_screening_room/` — cinema front-of-house, command-deck
back-of-house). Implemented end-to-end on branch `claude/new-session-693cw5`
(platform session constraint — the handoff's 5-PR plan landed as 5 phase
groups of commits on ONE draft PR; Kevin merges):

- **Foundation:** `static/screening-room-tokens.css` (verbatim from the
  handoff) + `static/screening.css` component layer, loaded after mise.css.
  mise.css itself now loads inside a CSS cascade layer (`layer(mise)`) so the
  body.sr-scoped redesign wins without specificity wars — non-sr surfaces are
  untouched. IBM Plex Mono self-hosted (400/500/700, latin+ext) in fonts.css.
  Flags: `MISE_SCREENING_ROOM` (default ON — the kill switch back to the
  legacy look) and `MISE_AERIALS_LIVE` (default ON since 2026-07-12 —
  Kevin launched aerials at $150; =false is the kill switch) in
  config/features.
- **Marketing:** home = the Lobby (house reel + live mono timecode via
  data-sr-player in site.js, three feature title cards, ticker, credits
  footer); spokes = 3b/3k/3c+3l (RE premiere hero with data-seek chapter
  chips at fractions of the real film's duration; Aerial Pass band, spec-line
  segment, ticker line, credits "flown" all gated on aerials_live); archive
  chips (250D/800T/500T + ▶ films first), reels, services/about/contact/
  press/work/booking restyled. Aerial add-on: checkbox on re- intakes →
  bookings.notes tag (zero schema); rate single-sourced in
  `specialties.AERIAL_PASS_CENTS` (**$150 — set by Kevin 2026-07-13**).
- **Client:** ticket-stub PIN gates (shared `_ticket_gate.html`; PIN logic
  untouched), premiere gallery (Reel One row, circled takes = favorites
  numbered in pick order, sticky export rail w/ OOB count, REC tiles polling
  `/g/{slug}/rendition-tile/{id}` every 8s), portal/workspace/drop/zip-wait/
  email-gate/error/expired restyles. **Money docs (invoice/proposal/receipt)
  are display-only bodyclass opt-ins in their own commit (bc5d3c7) —
  red-adjacent, flag for Kevin.** contract.html untouched.
- **Admin:** command deck — 64px rail (legacy sidebar kept behind the kill
  switch), ON DECK ranked queue (read-only merge; nudge dismiss = snooze),
  day strip with Aerial Pass preflight, ⌘K command runner (multi-token, ">"
  actions filter, client bindings lazy-loaded from /admin/palette.json),
  bench culling keys (data-cull in behaviors.js → existing endpoints) +
  premiere check, ledger month reel + one action per row, 5-tab mobile bar.
- **Gates at completion: 59 unit / 174 smoke / ruff clean.** Tests updated
  where they pinned pre-redesign copy (each noted in commit messages).
- Aerials launch checklist: `ops/SPECIALTY-LAUNCH.md`. Post-merge deploy
  checks (Kevin): /healthz · home · one spoke · one PIN gallery · /admin
  login · one favorite toggle — a missed CSP spot fails silently, check the
  console. Rollback: `MISE_SCREENING_ROOM=false` restores the legacy look
  without a revert; full revert = revert the merge.
- **Completion round (2026-07-12, same branch, second draft PR after the
  first merged as #46): NOTHING remains deferred.** Kevin asked for the
  deferred list to be completed to completion; it was:
  - Focused project (3h) full anatomy on /admin/studio/projects/{id}:
    delivery workbench (`templates/admin/_delivery_check.html`, polls
    `GET /admin/studio/projects/{id}/delivery-check` every 8s only while
    encodes run), money card (paid-vs-total + 1-tap draft invoice via the
    existing invoice endpoint), wire card (latest timeline), stock chip
    from the booking's event-slug prefix. Read-only over existing rows.
  - Long-tail admin sweep, verified with a 28-page 1440px screenshot
    sweep: crew-pass login ticket (kill switch falls back to the cream
    card; auth untouched), inbox queue, activity/audit/today mono-ledger,
    scheduling Aerial-Pass preflight badge, and a systemic
    `body.sr-admin` re-map of the legacy variable set so unmapped
    components convert wholesale.
  - Mobile deck (3j): at ≤860px ON DECK deals one card at a time —
    swipe ← done / swipe → snooze (both submit the existing
    `/admin/home/nudge/dismiss` form; no new endpoints), Back/Skip
    buttons + "1 of N" counter, progressive enhancement via
    `data-deck-swipe` in behaviors.js (desktop/no-JS keep the list).
    Included fix: dismissing now clears the whole card from the deck for
    the rest of the local day (the ◯ says "snooze until tomorrow" and now
    means it); it re-ranks tomorrow if the condition holds.
  - Premiere on second visit: the gallery title-card ceremony plays once
    per browser (seen-cookie `sr_seen_g{id}`, set only after PIN
    admission, path-scoped, display-only); repeat visits get a compact
    "welcome back" strip. Kill switch keeps the full card every visit.

## 0a. STATUS 2026-07-11 — 3-specialty flagship revamp (in flight)

Kevin approved the hub-and-spoke revamp: real estate / portrait & lifestyle /
food & beverage, photo AND video, under the kept "Kevin Lee Photography" brand
("Photography & Film" lockup). Green work is on branch
`claude/kleephotography-flagship-revamp-v0vgl9` (draft PR — Kevin merges):

- Specialty taxonomy = portfolio-tag prefix convention (`re/`, `pl/`, bare =
  legacy F&B) in `app/specialties.py` — zero schema. Spokes at
  `/real-estate` `/portraits` `/food-beverage` (INDEXABLE + sitemap updated —
  both lists live in `app/public/site.py`); homepage is the router (three
  doors); portfolio/reels/work/about/contact broadened in place. F&B spoke
  inherits the old home copy (SEO). JSON-LD broadened + dead-image fix.
  Video delivery UX: duration badges, web-MP4 download, per-specialty booking
  intake labels (event-slug prefix convention `re-`/`pl-`).
- **No client-facing URL moved. Zero redirects needed. Money/schema/contract
  files untouched in the green pass.**
- Operator runbook: `ops/SPECIALTY-LAUNCH.md` (tagging, event types,
  launch checklist).
- RED (draft PRs, Kevin merges, never self-merge): R1 per-specialty
  services/pricing (site.py SERVICES + proposals.py PRESETS — includes the
  $650-display vs $900-anchor question), R2 RE+portrait contract templates,
  R3 video renditions migration (9:16/1:1 encodes, format-choice downloads).

## 0b. STATUS 2026-07-10 — prior refactor queue COMPLETE

Everything below this section is historical context. PRs #2–#18 are all MERGED.
The audit remediation and the follow-up queue finished as:

- **§6a red-light findings: ALL SEVEN FIXED** (verified in main 2026-07-10) —
  portal PIN buckets offset (`portal._pin_bucket`), server-side revocable admin
  sessions (migration 065 + `admin_sessions`), `COOKIE_SECURE` derives from an
  https BASE_URL, cross-IP per-target PIN cap (`PIN_TARGET_MAX_FAILS`), Stripe
  `?thanks=1` return acknowledged (#17), `/c/ /w/ /t/` now rate-metered, admin
  password compare is bytes-safe. HSTS ships at `max-age=300` when COOKIE_SECURE.
- **Security follow-ups:** Permissions-Policy header + RFC 9116 security.txt (#16);
  CSP `script-src` has NO `unsafe-inline` — per-request nonces + delegated
  `static/behaviors.js` (data-confirm / data-print / data-autosubmit / data-goto),
  45-file sweep (#18). `style-src` keeps `unsafe-inline` deliberately (documented
  in `app/main.py`). New fragment endpoints must stay script-free (nonce mismatch).
- **Design pass:** marketing + client-facing form polish, focus-visible states,
  testimonials elevation (#14, #15). Refactor duplication collapse (#11).
- Gates at completion: **50 unit + 166 smoke, ruff clean**, CI green on main.
- **Standing deploy reminders for Kevin:** full-site deploy via git pull on flow
  ([`ops/DEPLOY.md`](ops/DEPLOY.md)); delete/flip any `MISE_COOKIE_SECURE=false`
  left in flow's `.env`; expect one admin re-login after the 065 session-table
  migration; post-deploy click through admin flows — a missed CSP spot fails
  silently with a console `Refused to execute…` line.
- **Do not start §6b-remaining / §6c or anything new without Kevin's explicit ask.**

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
`MISE_DATA_DIR=<tmp> MISE_SECRET_KEY=devtest MISE_ADMIN_PASSWORD=pw`. Fresh
databases intentionally start without published proof; seed only verified content.

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

## 7. What remains (STALE — archive only)

> **Stale.** The PR #2 / `claude/klee-photography-refactor-y9tr5g` queue below is
> historical. For deploy, use [`ops/DEPLOY.md`](ops/DEPLOY.md) (git pull on flow),
> not `scripts/deploy-flow.sh`. Ask Kevin before picking up §6b / §6c leftovers.

1. ~~Green fixes from §6b~~ — only with Kevin's explicit ask.
2. ~~Push to refactor branch / PR #2~~ — completed / superseded.
3. ~~Keep PR #2 body current~~ — superseded.
4. §6c sweeps — optional, Kevin-gated.
5. ~~Finalize draft PR #2~~ — superseded.
6. **Deploy:** git pull on flow `/opt/mise` + restart mise ([`ops/DEPLOY.md`](ops/DEPLOY.md)).
   Post-deploy: `/healthz`, home, spoke, `/admin` login, one gallery PIN page.
   Rollback look: `MISE_SCREENING_ROOM=false`. Data: nightly backups per `ops/BACKUP.md`.

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
