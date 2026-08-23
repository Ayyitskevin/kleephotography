# UPGRADE BRIEF — full-codebase review → prioritized work queue (2026-08)

> ## STATUS 2026-08-10 — workstreams A–F are implemented
>
> Everything green-light in this brief has landed on `claude/kleephotography-review-93yx8l`
> (85 commits). Gates at completion: **236 unit / 378 integration / 187 smoke / ruff clean**,
> 795 tests collected, up from 134/207/187 and 522.
>
> **Not done, and why — the honest remainder:**
>
> - **Workstream G in full.** Every item is red-light under `AGENTS.md` and needs Kevin.
>   G1/G2 (media + off-host backup) are still the only existential risk in the project.
>   **Exception: G9 (and its B7 origin) is CLOSED — do not implement.** It was tried
>   and rejected in `bebf436`; it locks the owner out of `/admin/login`, and
>   `tests/test_ratelimit_admin_bypass.py` now guards against re-deriving it.
> - **B6** (TTL-cache the per-render template globals). **Closed — do not implement.**
>   Skipped first with evidence: a plain cross-render TTL breaks three smoke tests that
>   pin an admin write reflecting immediately, and doing it safely needs write-side
>   invalidation in the admin write paths. It is now moot as well as awkward. B6 existed
>   because each global opened its own connection (~0.9 ms apiece, ~1.8 ms a page);
>   thread-local read connections landed in `820944a` and the pair now measures
>   **0.005 ms per render**. A TTL would buy five microseconds in exchange for a
>   staleness window on a newly starred photo or a freshly published press hit. The
>   reasoning is recorded at the functions in `app/render.py` so it is not re-litigated.
> - **C6** (version the font filenames for immutable caching) — deferred, not attempted.
> - **C8** (raise the 8.5–9px caption floor) — deliberately left: it is a visual design
>   change this brief said Kevin should eyeball, not an agent.
> - **`inbox.html`'s own pager** stays hand-rolled: its inline gutter style is load-bearing
>   under the `MISE_SCREENING_ROOM=false` stack, which must not learn `sr-*` names. A test
>   asserts it is the *only* remaining exception, so a seventh copy cannot appear quietly.
>
> **New findings surfaced while implementing** (not in the original review):
>
> - `app/public/sms_webhook.py` calls `.get()` on a parsed webhook body before
>   establishing it is a dict — the same defect class as the four fixed in `app/sms.py`.
> - `app/admin/financials.py` `receipt_upload`, `app/admin/uploads.py` and
>   `app/admin/common.py` still run blocking work on the event loop; the de-async sweep
>   did not own those files.
> - `app/admin/reports.py` `revenue_csv` holds a fourth copy of the month-bucketing SQL.
>   Left alone deliberately: it is an unbounded all-time export, not a trailing-N series.


**Audience:** the implementing agent (and Kevin, for the red-light PRs).
**Produced by:** a five-dimension adversarial review (core backend, admin surface,
public/frontend, tests/CI, ops/security) run 2026-08-09 against `main` @ `ff441fa`,
with every finding verified against current code — line numbers below are from that
commit. All four gates were run and green before this review started
(134 unit / 207 integration / 187 smoke / ruff clean).

**How to use this file:** work top to bottom within a workstream; workstreams A–F are
green-light under `AGENTS.md` (still: gates before every commit, one logical change
per commit, surgical diffs). Workstream G is **red-light** — each item ships as its
own PR with the risk spelled out, Kevin merges, never self-merge. When a line number
has drifted, trust the quoted code/identifier, not the number. If a finding turns out
stale when you open the file, skip it and say so in the commit/PR body — do not
force a fix onto code that already changed.

**Session constraints override defaults:** if your session designates a working
branch, use it and deliver via draft PR even for green-light work.

---

## 0. Ground rules (non-negotiable)

1. Read `AGENTS.md` first. It overrides everything, including this file.
2. Gates before every commit (all four): unit, integration, smoke
   (`MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw`,
   ffmpeg installed), `ruff check . && ruff format --check .`.
3. Every behavior change lands with a test that fails without it. Mechanical
   refactors (e.g. the `get_or_404` sweep) may rely on existing coverage — say so
   in the commit message.
4. Red-light zones (money/`pay.py`, `migrations/`, `app/security.py`,
   `app/admin/auth.py`, CSRF/session/cookie/rate-limit, deploy/backup files,
   contracts): never edit in a green commit. Everything below marked **[RED]** goes
   in Workstream G PRs.
5. Match house style: bound `?` placeholders, `db.ident` for identifiers,
   `studio._today()` for studio dates, `sr-*` tokens for new UI, icons from
   `static/icons.svg`, no JS build step, no new dependencies without cause.
6. Keep the swap-contract comments on HTMX fragments current when you touch them.

---

## 1. Scorecard (context for prioritization)

| Area | Grade | One-line justification |
|---|---|---|
| Core backend (db/jobs/main/money) | A | Transactional migration runner, staged-in-tx jobs, distrusting webhook — verified true, not just documented |
| App-layer security | A− | Layered PIN defense, revocable sessions, nonce CSP; hardening nits are in G |
| Docs & operability discipline | A− | AGENTS/ops runbooks current and honest; a few stale spots (Workstream F) |
| Tests & CI | B+ | 528 gate-green runs, JS tested; no coverage measurement, one order-coupled smoke chain, ~970 untested admin lines |
| Public frontend | B | Strong a11y/SEO foundations; ~224 KB of admin CSS shipped to every visitor, CLS on gallery tiles |
| Admin back-office | B | Uniformly confirm-guarded and injection-safe; perf debt (fs walks, N+1), pagination gaps, 4 data-accuracy bugs |
| Ops resilience (backup/DR/alerting) | D | **Client media & receipts have no backup of any kind; all alerting lives on the monitored host** |

The D is the headline. Everything else is polish on a strong base.

---

## 2. Workstream A — correctness & durability (do first, all green-light)

### A1. Job queue: close the zombie-'running' hole — HIGH
- **Where:** `app/jobs.py:316-320` (`_execute`), `app/jobs.py:393-418` (`queue_health`),
  `app/main.py:337-353` (`/healthz` payload), `app/ops_monitor.py` (heartbeat).
- **What:** `payload = json.loads(job["payload"])` sits *outside* the `try` in
  `_execute`. A corrupt payload raises before the except that records failure; the row
  stays `status='running'` forever — invisible to `queue_health()` (only sums
  failed/queued), never re-offered by `sweep()` (queued-only), unlogged (the pool
  future's exception is never observed). Only a process restart recovers it, silently.
- **Do:** (1) move the parse inside the `try`; (2) add a `running_stale` count to
  `queue_health()` — `status='running' AND updated_at < datetime('now', ?)` with a
  generous ceiling (e.g. reuse `JOB_STUCK_AFTER_SECONDS`) — and surface it in
  `/healthz` and the ops heartbeat next to `jobs_stuck`.
- **Accept:** a test enqueues a job with non-JSON payload directly via SQL, runs
  `_execute`, asserts the row ends `failed` with an error message; a second test
  fabricates a stale running row and asserts `queue_health()["running_stale"] == 1`
  and `/healthz` exposes it.

### A2. `db.tx()` should BEGIN IMMEDIATE — MED
- **Where:** `app/db.py:251-266`; the existing workaround at `app/admin/uploads.py:85`
  (manual `BEGIN IMMEDIATE` inside `db.tx()`); precedent `app/db.py:150-155`.
- **What:** `db.tx()` opens a deferred transaction. Under WAL, read-then-write units
  that lose the race get `SQLITE_BUSY_SNAPSHOT` on upgrade — an instant "database is
  locked" 500 that `busy_timeout` does **not** wait out. The uploads workaround
  existing proves the default is wrong.
- **Do:** have `tx()` set `isolation_level=None` on its connection and issue
  `BEGIN IMMEDIATE` before yielding; delete the now-redundant manual BEGIN in
  uploads.py. Keep commit/rollback semantics identical.
- **Accept:** existing suite green; add a unit test that two threads in `tx()` both
  read-then-write the same row and neither raises (second waits, not errors).

### A3. Stop the sweeper's duplicate-submission churn — MED
- **Where:** `app/jobs.py:262-272` (`dispatch`), `:421-435` (`sweep`), `:316` (`_execute`).
- **What:** every 60s tick re-submits *every* due queued job. With a deep backlog
  (200 transcodes, 2 workers) each tick adds ~200 duplicate futures; each duplicate
  runs `_claim` — a write transaction that no-ops — thousands of pointless
  write-lock acquisitions per hour contending with request-path writes.
- **Do:** module-level `_inflight: set[int]` guarded by a `threading.Lock`; add id in
  `dispatch`/`retry` submission, `discard` in a `finally` at the end of `_execute`;
  `sweep` skips ids already in flight. `_claim` stays the correctness gate.
- **Accept:** unit test — freeze the pool, queue N jobs, call `sweep()` twice, assert
  the pool received N submissions, not 2N.

### A4. `/healthz` must not freeze the loop when the DB wedges — MED
- **Where:** `app/main.py:329-370`.
- **What:** the handler is async (deliberately, to answer under threadpool
  saturation) but `db.one(...)`/`jobs.queue_health()` are blocking calls with a 30s
  busy_timeout — in exactly the wedged-DB scenario health checks exist for, `/healthz`
  blocks the event loop up to 30s and freezes every other in-flight response.
- **Do:** run the DB probe via a dedicated `ThreadPoolExecutor(1)` +
  `asyncio.wait_for(..., 0.5)`; on timeout return 503 with `"db_probe": "timeout"`.
  Keep the storage check as-is.
- **Accept:** test monkeypatches `db.one` to sleep >0.5s and asserts a fast 503.

### A5. Fix the topbar "Export CSV" downloading an empty file — MED (user-facing data bug)
- **Where:** `templates/admin/financials.html:14`, `app/admin/financials.py:241-257`.
- **What:** the topbar link passes only `range`; with both `inc_paid`/`inc_out`
  empty the row filter keeps nothing — Kevin's accountant gets a header-only CSV.
- **Do:** change the backend default: when *neither* param is present, include both
  (`if not inc_paid and not inc_out: inc_paid = inc_out = "on"`). The export panel's
  explicit checkboxes keep working (an unchecked box still excludes when its sibling
  is checked).
- **Accept:** integration test — seed one paid payment + one open invoice, GET
  `/admin/financials/income.csv?range=year` bare, assert both data rows present;
  GET with `inc_paid=on` only, assert the open row absent.

### A6. Reply queue hides the oldest waiting leads — MED
- **Where:** `app/admin/activity.py:121-158` (queue built from
  `ORDER BY created_at DESC LIMIT 6`), correct pattern at `:297-313`
  (`ORDER BY created_at ASC LIMIT 5`), headline count already computed at `:55-57`.
- **What:** with >6 open inquiries the *oldest* leads — the ones a reply queue
  exists to surface — never appear, and "N inquiries are waiting" caps at 6.
- **Do:** give `queue` its own `ORDER BY created_at ASC LIMIT 6` query; headline the
  count with `new_inquiries`. The On Deck reply lane (`:422-433`) inherits the fix.
- **Accept:** integration test with 8 open inquiries asserts the oldest is in the
  rendered queue and the count reads 8.

### A7. Google Calendar connect/disconnect has no UI — MED
- **Where:** `app/admin/scheduling.py:289-290` (context passed), `:406`
  (`GET /admin/scheduling/google/connect`), `:449` (`POST .../google/disconnect`);
  `templates/admin/scheduling.html` (renders only `g_error` at :20);
  `app/admin/settings.py:37` (Settings tile links to `/admin/scheduling`).
- **What:** backend + context exist; nothing renders them. Once connected there is
  no way to disconnect without hand-crafting a POST.
- **Do:** small card on `scheduling.html` from the already-passed `gcal` context:
  configured/connected status line, Connect link, confirm-guarded
  (`data-confirm`) Disconnect form. Follow the date-overrides card idiom on the
  same page (`scheduling.html:95-125`).
- **Accept:** smoke test asserts the card renders in both states (patch
  `gcal.status`).

### A8. Portal crop failure is invisible and polls forever — MED
- **Where:** `app/public/portal.py:48-95` (`_crop_link_status`, `crops_pending`),
  `templates/public/_portal_crops.html:6` (8s self-poll); model to copy:
  `templates/public/_rec_tile.html:9-12` (explicit failed state) and
  `app/public/downloads.py:358-373` (failure supersession).
- **What:** `crops_pending` is computed purely from file existence; if a
  `social_crops` job exhausts retries the client sees "preparing…" forever and the
  browser polls forever.
- **Do:** have the crops context consult the jobs table (failed `social_crops` for
  the asset with no queued/running successor) → render a "hit a snag — being
  re-run on our side" chip and drop those ratios from `crops_pending` so polling
  stops.
- **Accept:** integration test — fabricate a failed job row, assert chip rendered
  and no `hx-trigger` poll attribute present.

### A9. Portal crops ZIP: no disk floor, no dedupe, inline build — MED
- **Where:** `app/public/portal.py:308-323`; pattern to reuse:
  `app/public/downloads.py:44-90` (`_queue_or_build`), `:266-273` (MIN_FREE_GB).
- **Do:** at minimum add the disk-floor refusal; ideally route big builds through
  the queued path like gallery ZIPs (byte-ceiling split, wait page already exists).
- **Accept:** test asserts 507-style refusal when free-space check fails
  (monkeypatch the probe).

### A10. Stale zip_build rebuild can delete the newer archive — LOW
- **Where:** `app/jobs.py:195-205` (`_h_zip` prunes everything but its own rev).
- **Do:** handler re-checks `galleries.content_rev`; if `payload["rev"]` is stale,
  return without building or pruning.
- **Accept:** unit test with a stale-rev payload asserts the newer zip file survives.

---

## 3. Workstream B — performance (green-light)

### B1. Stop shipping ~224 KB of admin CSS to every public visitor — HIGH (biggest perf win)
- **Where:** `templates/base.html:17` (imports all of `static/mise.css`, 364 KB raw /
  79 KB gz, on every surface); ~63% of it is `.admin-shell`-scoped; admin base
  `templates/admin/base_admin.html`; inventory doc `ops/CSS-DUAL-STACK.md`.
- **Do:** split `mise.css` → `mise.css` (public + shared + marketing kill-switch
  blocks) and `mise-admin.css` (every `.admin-shell`/admin-only block), the latter
  loaded only from the admin base *inside the same `layer(mise)`* so the cascade
  contract is unchanged. Mechanical move — no rule edits in the same commit. Update
  `ops/CSS-DUAL-STACK.md`'s inventory. Verify with `scripts/ui-shots.mjs`
  before/after screenshots on both admin and public pages, both flag states.
- **Accept:** public pages fetch no `.admin-shell` rules (assert the string absent
  from the public sheet in a unit test); admin pages render identically in shots;
  all smoke green.
- **Note:** retiring the *marketing* kill-switch blocks entirely (a further ~15 KB gz)
  removes an operator rollback → that decision is Kevin's; propose it in the PR body,
  don't do it.

### B2. Media route fsync storm: debounce `visitors.last_seen` — MED **[RED: app/security.py]**
- Moved to G6 — the one-line debounce lives in `security.get_visitor`. Do not edit in
  a green commit.

### B3. Replace per-gallery filesystem walks with SQL — MED
- **Where:** `app/admin/galleries.py:178` (dashboard), `:307` (transfers),
  `app/admin/studio.py:287-292`; walker `app/admin/common.py:51-54` (`rglob` + stat
  per file, per gallery, per page view).
- **Do:** `SELECT COALESCE(SUM(bytes),0) FROM assets WHERE gallery_id=?` (or one
  grouped query for all galleries); accept that derivatives are excluded and label
  the strip "originals" — or cache dir sizes keyed on `content_rev` if the full
  number matters. Check what the template label promises before choosing.
- **Accept:** dashboard renders identical totals for a seeded gallery via SQL path;
  no `dir_size` call remains on the two library pages.

### B4. Batch the gallery bench N+1s; slim the per-tile fragment context — MED
- **Where:** `app/admin/galleries.py:89-91` (`video_comment_thread` per video in a
  loop), `:124-142` (`_tile_fragment` rebuilds the whole bench for one-tile actions;
  star `:726`, tag `:737`, cover `:681`, renditions `:696`, delete `:801`).
- **Do:** one `WHERE asset_id IN (...)` query grouped in Python for threads; give
  `_tile_fragment` a slim context (the one asset + the aggregates the oob pills
  need). Keep the fragment swap contract comments current.
- **Accept:** query-count assertion (sqlite trace hook) on a star toggle for a
  seeded 20-asset/3-video gallery drops from ~25 to ≤6.

### B5. Bound the calendar bookings query — LOW
- **Where:** `app/admin/tasks.py:221-231` (all confirmed bookings ever, filtered in
  Python); pattern `app/admin/scheduling.py:204-213`.
- **Do:** `WHERE start_utc BETWEEN ? AND ?` with a ±1-day guard band.

### B6. Cache the per-render template-global DB hits — LOW
- **Where:** `app/render.py:52-74` (`_og_image_id`, `_has_press_features` open fresh
  connections per render); TTL pattern already at `app/render.py:13-30`.
- **Do:** TTL-cache both (60s is plenty).

### B7. Rate limiter: only check `is_admin` when over limit — **CLOSED / WON'T DO** (see G9)
- **Misclassified here.** This brief filed B7 as green because the change is a pure
  reorder inside `ratelimit.check` that never opens `security.py`. That reasoning is
  wrong: `AGENTS.md` puts "rate-limit/lockout" in the red-light list by *subject*, not
  by which file the diff lands in, and the repo's own tiebreak is "when unsure which
  side a change sits on, treat it as red". Reordering when the admin bypass is
  consulted changes who gets metered, which is exactly the class of change that rule
  exists to gate.
- Moved to Workstream G as **G9**, then **rejected outright** in `bebf436` — it locks
  the owner out of `/admin/login`. Do not implement it in any commit, green or red;
  the reasoning and the regression guard are recorded under G9.

### B8. Retention sweeps — LOW
- **Where:** new hourly task alongside `app/scheduler.py` consumers; `jobs` and
  `visitors` tables currently grow forever.
- **Do:** `DELETE FROM jobs WHERE status='done' AND updated_at < datetime('now','-30 days')`
  and a similar stale-anonymous-visitors prune, run from the existing hourly
  scheduler. **No schema change** (that would be red).
- **Accept:** unit test seeds old done jobs, runs the sweep, asserts pruned; recent
  and non-done rows survive.

### B9. Finish the de-async sweep (green subset) — MED
- **Where:** ~20 remaining `async def` handlers doing blocking DB work on the event
  loop after their `await request.form()`: `app/public/sms_webhook.py:174-220`,
  `app/public/forms.py:56-178`, `app/service_api.py:158-171`, and the async form
  handlers across `app/admin/{invoices,galleries,gallery_sections,recurring,licenses,presets,shotlist,press,proposals,scheduling,studio_brand,financials}.py`.
- **Do:** the established pattern from commit `2879636`: async wrapper awaits
  body/form, then `await run_in_threadpool(sync_impl, ...)` for everything after.
  `app/public/pay.py` has the same defect but is **[RED]** — covered in G8, do not
  touch it here.
- **Accept:** no behavior change; suite green; grep shows no async handler in the
  listed files doing direct `db.` calls after the form parse.

---

## 4. Workstream C — frontend: SEO, CLS, a11y (green-light)

### C1. Un-noindex the video assets JSON-LD points at — MED (one line)
- **Where:** `app/main.py:249` exemption tuple lacks `/site/vid/`, `/site/poster/`;
  `templates/site/reels.html:97-98` VideoObject `contentUrl`/`thumbnailUrl` point at
  them. The rich-result markup is currently self-defeating.
- **Do:** add both prefixes to the tuple.
- **Accept:** unit test asserts no `X-Robots-Tag` on `/site/vid/{id}`.

### C2. Real `srcset`/dimensions on home + specialty tiles — MED
- **Where:** `templates/site/home.html:129-130` (filmstrip), `specialty.html:115-116,
  149-150, 230-237, 305-313, 379-380` — `sizes` present without `srcset` (inert), no
  width/height; spec builder already exists (`app/public/site.py:138-198`,
  used by `portfolio()` at `:514`).
- **Do:** build `asset_images` maps in `home()` / `_specialty_page()` exactly as
  portfolio does; emit `srcset` + `width`/`height`. Where a spec isn't available,
  delete the dead `sizes` attribute rather than leave it lying.

### C3. Gallery/portal/drop tile dimensions (CLS on the highest-emotion page) — MED
- **Where:** `templates/public/gallery.html:68-69`, `portal.html:80`,
  `_portal_crops.html:12-13`, `drop.html:27-28` — bare lazy `<img>`, no dimensions;
  assets table already stores width/height after A-series ingest
  (`app/jobs.py:74,97-99`).
- **Do:** emit `width`/`height` (derivative dims — same aspect) in the tile macros.
  Thumb dims can be derived from stored aspect at THUMB_MAX_PX or probed via the
  cached `imaging.image_dimensions`; prefer stored data over per-render probes.
- **Accept:** rendered tiles carry dimensions; existing pixel hooks (`lb-*` etc.)
  untouched.

### C4. Lightbox SR announcement uses unfiltered indices — MED (a11y)
- **Where:** `static/lightbox.js:313-316` announces `idx+1 of tiles.length`;
  `visibleTile()` already exists at `:242-252`.
- **Do:** announce position within the visible subset.
- **Accept:** extend `tests/js/lightbox-comments.test.mjs` (or sibling) to cover a
  filtered set.

### C5. Small a11y/markup fixes — LOW (one commit)
- Email gate input has no label: `templates/public/email_gate.html:10-11` (PIN gate
  does it right at `_ticket_gate.html:15`).
- Contact form errors don't mark fields: `templates/site/contact.html:123-125`;
  reuse the `invalid()` macro pattern from `templates/site/form.html:18`; the POST
  handler already distinguishes the failing field (`app/public/site.py:704-706`).
- Honeypots are `aria-hidden` yet focusable controls: `contact.html:121-122`,
  `form.html:59`, `_book_card.html:172-173` — move `aria-hidden` to a wrapper.
- `uploadDate` can render `""` in VideoObject: `templates/site/reels.html:99` —
  wrap in `{% if r.created_at %}`.
- `copy_link.js` binds at load only (dead after HTMX swaps): `static/copy_link.js:8`
  → one delegated document listener, matching `behaviors.js` style.

### C6. Font caching — LOW
- **Where:** `app/main.py:269-270` bounds `/static/fonts/` at 86400 because
  filenames are stable; ~350 KB re-validated daily.
- **Do:** version the font filenames (e.g. `.v2.woff2`, update `fonts.css`) and let
  them fall into the immutable `/static/` branch; or raise max-age to 30d. Prefer
  versioning — it matches the existing `?v=` doctrine.

### C7. ui-shots hardening — LOW
- **Where:** `scripts/ui-shots.mjs:30-43` (`PUBLIC_PAGES` lacks `/work`), `:55-58`.
- **Do:** add `/work`; create Playwright contexts with `reducedMotion: "reduce"` so
  ticker/blink animations stop making before/after diffs flaky (also exercises the
  reduced-motion CSS paths).

### C8. Micro-type floor — LOW (judgment call, propose in PR)
- `.portfolio-cap` 8.5px / `.sr-footbase` 9px (`static/screening.css:363-368,
  107-110`). Captions are content (client names), not decoration; floor at 10–11px.
  Screenshot before/after in the PR — this is a design change Kevin should eyeball.

---

## 5. Workstream D — admin correctness & polish (green-light)

### D1. Month bucketing on UTC contradicts the one-clock doctrine — MED
- **Where:** `app/admin/financials.py:208-212`, `app/admin/activity.py:186-191`,
  `app/admin/reports.py:69-73,282-285` bucket with `strftime('%Y-%m', ...)` on raw
  UTC; `_spark_series` (`activity.py`) already does `date(...,'localtime')` and its
  comment explains why. A 9 PM EDT Aug-31 payment lands in September's bar and CSV.
- **Do:** add `'localtime'` at the four sites. Better: extract one
  `month_series(n, ...)` helper (three near-identical implementations exist —
  `activity.py:177-205`, `financials.py:199-223`, `reports.py:17-28`) and fix it once.
- **Accept:** unit test with a 23:30 EDT month-boundary timestamp asserts the bucket.

### D2. Reconcile the two definitions of "collected" — MED
- **Where:** Home KPIs sum `invoices.total_cents` by `paid_at`
  (`app/admin/activity.py:102-105,184-192,491-495`); Financials/Reports sum the
  `payments` table (`financials.py:163-166`, `reports.py:90-94`). A deposit-paid
  invoice counts $0 on Home; month attribution differs.
- **Do:** standardize dashboard money on the payments table (repo doctrine calls
  payments the ground truth). Reuse the D1 helper.
- **Accept:** integration test — deposit-paid invoice shows its deposit in Home's
  collected figure and matches Financials for the same window.

### D3. Audit CSV silently truncates at 500 — MED (evidence-trail bug)
- **Where:** `app/admin/audit.py:22` (`_LIMIT = 500`), `:171-182` (`audit_csv` says
  "full window", exports the capped list).
- **Do:** un-LIMIT the CSV route's query (viewer keeps 500).
- **Accept:** test seeds 501 events, asserts CSV row count.

### D4. Pagination sweep on forever-growing ledgers — MED
- **Where:** expenses `app/admin/financials.py:393`, receipts `:536`, mileage
  `:651`; form submissions `app/admin/forms.py:227-231`; press `app/admin/press.py:160`;
  past bookings `app/admin/scheduling.py:561-564` (LIMIT 100, silent); inbox
  `app/admin/inbox.py:363` (100-cap, badges show true counts — list diverges
  permanently past 100 archived threads).
- **Do:** copy the in-repo pager pattern (`activity.py:632-659` +
  `templates/admin/emails.html:33-35`) — `offset`, `page_size`, true totals,
  Newer/Older links. Full CSV exports stay full.
- **Accept:** each page seeded past one page size shows the pager and reaches page 2.

### D5. CSV writing: use `csv.writer`; keep cents raw — LOW
- **Where:** hand-rolled escaping ×3 — `app/admin/financials.py:271-277, 520-529,
  735-743`; summary re-parses `"$1,234.56"` back to cents at `:263-264`; correct
  in-repo pattern `app/admin/audit.py:171-178`.
- **Do:** `csv.writer` everywhere; carry `cents` in `_ledger` rows alongside the
  display string.

### D6. Input-validation trio — LOW
- `app/admin/galleries.py:632` — `int(v)` on form input 500s; wrap → 400
  (pattern: `_posint`, `app/admin/scheduling.py:65-75`).
- Raw date strings stored verbatim: `financials.py:483,506` (`spent_on`),
  `:707,721` (`drove_on`), `galleries.py:445,460` (`expires_at`) →
  `date.fromisoformat`-or-400 (pattern: `scheduling.py:337-340`).

### D7. Dead/incomplete endpoints — LOW
- `POST /admin/tasks/{id}/delete` (`app/admin/tasks.py:143-149`) has no UI caller:
  add a confirm-guarded delete control to the task card (preferred) or remove the
  route.
- Content page caption card lies past 60 (`app/admin/content.py:125-136,186-190`):
  separate COUNT for the card.
- "Photos" tile counts photos+videos (`templates/admin/_gd_stats.html:7`); the
  split already exists (`gallery.html:7-8`) — render "N photos · M films" or
  relabel "Files".

### D8. `get_or_404` sweep — LOW (mechanical)
- ~22 hand-rolled one()+404 sites remain: `inbox.py:429,501,524,537`;
  `studio.py:90,111,126,139,544,833`; `galleries.py:610,659,684,703,841,865`;
  `studio_brand.py:57,134,157`; `financials.py:638`; `email_templates.py:55`;
  `emails.py:22`; `recurring.py:538`; `shotlist.py:63`; `uploads.py:30`;
  `tasks.py:132,145`. Convert where it's a straight fetch-or-404; leave sites with
  extra logic alone. Existing coverage carries this; say so in the commit.

### D9. Shared-helper dedup (only where 3+ real uses) — LOW
- `_initials()` ×3 → `admin/common.py`; month-series ×3 (see D1); open-invoice
  status tuple `('sent','viewed','deposit_paid')` ×9 → constant next to
  `common.open_invoice_balance`; inbox tab-guard redirect triple ×4 → one helper.
  Respect R9: don't abstract pairs.

### D10. Split `activity.home` — MED (behavior-neutral)
- **Where:** `app/admin/activity.py:35-558` — one 520-line handler, ~30 queries.
- **Do:** extract `_ctx_*` helpers mirroring `app/admin/studio_context.py`'s house
  style. Fold in A6 and D2 first so you're not moving code you're about to change.
- Also fix the naive-clock drift while there: `:41-43` uses server-local
  `datetime.now()`/`date.today()`; use `studio._today()` / tz-aware now from
  `config.TIMEZONE` (`:130-132` compares naive local now to stored-UTC timestamps).

---

## 6. Workstream E — tests & CI (green-light)

### E1. CI: timeout + concurrency-cancel — S
- **Where:** `.github/workflows/ci.yml:10-11` (no `timeout-minutes`, no
  `concurrency`); the smoke suite has ~10 poll loops and real ffmpeg work.
- **Do:** `timeout-minutes: 25` on the job;
  `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}`.

### E2. Coverage measurement — M
- **Do:** add pinned `pytest-cov` to `requirements-dev.txt`; run integration+smoke
  steps with `--cov=app --cov-report=term-missing:skip-covered`; after a baseline
  run, set a `fail_under` floor at the observed number minus 2 points. The gap
  table in this brief's source review is the target list.

### E3. Break the test_02 studio order-chain — M (unlocks `-k` and future xdist)
- **Where:** `tests/smoke/test_02_studio_docs.py:148,221,306,1092` — proposal/
  contract/invoice/notion tests read `ORDER BY id DESC LIMIT 1` state seeded by
  earlier tests (and assert literals like `"Dana Chef"`/`"$1151.00"` seeded two
  tests up); model fix already in-tree at `:1538-1549`.
- **Do:** extract `_studio_chain()` in `tests/smoke/_helpers.py` building
  client→project→accepted-proposal→paid-invoice via the routes; each lifecycle test
  calls it. Convert the `>= 3` job-count assert to a per-invoice
  `json_extract(payload,'$.invoice_id')=?` count (model at `:1563-1568`).
- **Accept:** `pytest tests/smoke/test_02_studio_docs.py -k proposal` passes alone.

### E4. Guard against ambient `MISE_DATA_DIR` — S (destructive-fixture footgun)
- **Where:** `tests/conftest.py:21` only `setdefault`s; `_strip_showcase_seed`
  (`:24-39`) then migrates and deletes rows in whatever DB that points at.
- **Do:** in conftest, always override `MISE_DATA_DIR` to a fresh tmpdir unless an
  explicit `MISE_TEST_DATA_DIR` is set.

### E5. Close the six zero-coverage admin modules — M
- `app/admin/forms.py` (242 ln), `audit.py` (182), `content.py` (218),
  `doc_templates.py` (142), `reference.py` (112), `email_templates.py` (72) — no
  test touches any route. Even login + GET 200 + one CRUD round-trip each closes
  ~970 untested lines. Fold in the D3 (audit CSV) and D4 (form submissions pager)
  tests here.

### E6. Test the never-executed integration bodies — M
- `app/mailer.py:13-36` — `send()` is always the patch seam; the MIME/ICS
  construction (Reply-To, the ICS `method` param gating Accept/Decline rendering)
  has zero executions. Patch `smtplib.SMTP_SSL`, assert the built `EmailMessage`.
- `app/sms.py:42-77` — same via the urllib seam.
- `app/gcal.py:139-221` — token exchange/refresh/expiry-skew/disconnect and the
  `/admin/scheduling/google/callback` route: patch the HTTP seam. (Pairs with A7.)

### E7. Suite hygiene — S
- Delete vestigial `tests/test_smoke.py` (docstring-only) + its references
  (`ci.yml:47,51`, `README.md:160-162`, `pyproject.toml:26`).
- Remove no-op `pytestmark` in `tests/smoke/conftest.py:35` (conftest marks don't
  apply); refresh the stale coupling bullets there (`:15-18`) and HANDOFF's
  (`HANDOFF.md:349-354`) — the two named-brittle tests are already fixed.
- Replace ~10 copy-pasted sleep-poll loops with a shared
  `wait_for(predicate, timeout, on_fail=...)` in `tests/smoke/_helpers.py`
  that dumps the jobs table on timeout (sites listed: `_helpers.py:79-83,104-108`,
  `test_01:212,308,433`, `test_03:118-122`, `test_04:595,684`, `test_07:190`,
  `tests/test_public_security.py:133-140`).
- Deduplicate `_configure_tmp_db` (argus/plutus copies drifting) and the four
  module-scoped `admin_client` fixtures into shared helpers.
- `tests/smoke/_helpers.py:40` — replace deprecated `tempfile.mktemp`.

### E8. Dependency/workflow hygiene — S
- Add a `github-actions` ecosystem block to `.github/dependabot.yml`; align
  `checkout@v6` (`opencode.yml:36`) with `@v7`.
- New CI step: fail on duplicate migration `NNN_` prefixes outside the known
  `{054,055}` allowlist (~10 lines; enforces `ops/MIGRATIONS.md` policy — CI edit
  is green, note the policy linkage in the PR).
- Either add a `python-version: ["3.12", "3.14"]` matrix for unit+integration, or
  change the README/pyproject floor claim to match reality.

---

## 7. Workstream F — docs reconciliation (green-light, one commit)

- `README.md:8,119,126-130` — "71 numbered .sql files" → 72; "~120 lines" db.py →
  ~266; drop the exact per-marker test counts (they've already drifted) or
  CI-assert them.
- `ops/TRUTHFUL-HTTPS.md:37-39` says to confirm an "off-machine restore proof" that
  `ops/BACKUP.md:23-35` says doesn't exist (chain retired). Reconcile to BACKUP.md's
  truth; leave the aspiration as an explicitly-open gap.
- `ops/HANDOFF-UX-REVAMP.md:12-20` claims the dead-CSS excavation is uncommitted on
  a machine — it shipped (`68cace7`). Archive-stamp it like HANDOFF.md.
- `ops/prototype-parity.md:10` keys its contract to `/tmp/mise-prototype-project`, a
  machine-local path — archive-stamp or reword.
- `.env.example` — add commented `MISE_TELEGRAM_TOKEN`/`MISE_TELEGRAM_CHAT_ID` with
  one line stating that without them backup-staleness/disk/queue alerting is
  dormant (the ops heartbeat silently no-ops — `app/ops_monitor.py:105-106`).
- `HANDOFF.md` §6b — mark the two named-brittle tests fixed; note the §6b findings'
  current verdicts (13 fixed / 2 partial / 1 open) so nobody re-verifies them.

---

## 8. Workstream G — RED-LIGHT: separate PRs, Kevin merges each

> Order reflects risk-adjusted value. Each is its own PR with the risk stated in
> the body. NEVER bundle a G item into a green commit.

### G1. Back up the media (the business's actual product) — CRITICAL
- **Today:** `ops/backup.sh:13` snapshots only `mise.db`. `MISE_DATA_DIR` also
  holds `media/` (client originals — unrecoverable), `brand/`, `receipts/`
  (financial records). None has *any* backup, even local. A wrong `rm` or disk
  death loses every client original; the DB snapshot would reference files that no
  longer exist.
- **PR:** extend the nightly unit (or add a second timer) to rsync `media/`,
  `brand/`, `receipts/` to a second local disk at minimum; state plainly in
  `ops/BACKUP.md` what is and isn't covered. (`zips/`, `tmp/` are rebuildable —
  exclude.)

### G2. Off-host DR — CRITICAL (pairs with G1)
- **PR:** nightly `restic` (or `rclone`+age) encrypted push of `data/backups/`
  latest snapshot + `media/` + `brand/` + `receipts/` to B2/S3 or a second box;
  monthly scripted restore-verify (gunzip → `PRAGMA integrity_check` →
  `foreign_key_check` → row-count sanity — the pattern already written in
  `ops/TRUTHFUL-HTTPS.md:22-33`); store `.env`, the cloudflared credential, and
  both systemd units off-host so the host is rebuildable. New runbook `ops/DR.md`.

### G3. External uptime + dead-man's switch — HIGH
- **Today:** every alarm originates on the host being monitored
  (`app/alerts.py`, `app/ops_monitor.py`); a dead host/tunnel/process is silent.
- **PR:** (a) document an external monitor polling
  `https://kleephotography.com/healthz` (zero code); (b) one line at the end of
  `ops/backup.sh`: `curl -fsS https://hc-ping.com/<uuid>` — backup silence then
  alerts through a channel that doesn't share the host's fate.

### G4. Minimal public `/healthz` — MED
- **Today:** `app/main.py:329-370` publicly serves `disk_free_gb`,
  `backup_age_hours`, `jobs_failed/stuck` — an attacker-readable ops feed and
  DoS-progress meter.
- **PR:** public path returns `{"ok": true}` only; full payload gated on loopback
  peer or the existing service bearer. Coordinate with G3's monitor config.

### G5. Session/auth hardening bundle — MED (small, mechanical; one PR)
- Hash `admin_sessions.token` at rest (sha256 lookup) — nightly snapshots currently
  contain live 90-day admin tokens (`app/security.py:226-229`, needs one migration).
- Split `MISE_ADMIN_SESSION_MAX_AGE` (7–14d) from the 90d client cookie age
  (`app/config.py:324`, `app/security.py:191,227-228`).
- Constant-time PIN compares: `app/public/gallery.py:180`,
  `app/public/portal.py:227`, `app/public/workspace.py:122` — the one place the
  compare-digest doctrine isn't applied.
- CSP: include `https://plausible.io` in script-src/connect-src only when
  `config.PLAUSIBLE_DOMAIN` is set (`app/main.py:133-134`).

### G6. Hot-path write relief — MED (touches security.py; pairs naturally with G5)
- Debounce `visitors.last_seen` (`app/security.py:161` via media route): the UPDATE
  fires per thumbnail request — 60 serialized fsync'd writes per gallery load on
  the rate-limit-exempt hottest path. One-line WHERE-clause debounce (10 min).
- `PRAGMA synchronous=NORMAL` in `db.connect()` (`app/db.py:40-46`) — standard WAL
  pairing; durability loss limited to OS crash, nightly backup exists. State the
  tradeoff in the PR.

### G7. HSTS ratchet — S
- `app/main.py:260-261` still `max-age=300` (the "temporary" value from July).
  After confirming cert coverage per `ops/TRUTHFUL-HTTPS.md`, raise to ≥15552000,
  no `includeSubDomains`/preload yet. Add a dated owner task so it can't idle again.

### G9. Rate limiter: only check `is_admin` when over limit — **CLOSED / WON'T DO** (was: LOW, moved from B7)
- **Do not implement this.** It was implemented, it failed, and it was rejected in
  `bebf436` ("security: ratchet HSTS to 180 days; document why the rate-limiter
  reorder is unsafe"). Read that commit message before reopening the idea.
- **The failure mode in one sentence:** deferring the `is_admin` check to the limit
  boundary lets admin requests land in the sliding window while under the limit, so
  Kevin's own HTMX-partial-heavy admin browsing fills the `(ip, "admin")` bucket
  during an ordinary minute and `/admin/login` then answers 429 the moment his
  session expires — locking the owner out of his own admin exactly when a deploy or
  a session expiry sends him there.
- **The guard:** `tests/test_ratelimit_admin_bypass.py` is kept precisely to stop a
  re-derivation — `test_admin_traffic_does_not_meter_a_later_anonymous_login` fails
  if the check moves. Seven tests caught it the first time.
- **The saving was not worth it:** one indexed SELECT on requests that already carry
  an admin cookie. `security.is_admin` returns `False` without touching the database
  when there is no cookie, so public traffic never paid for it. Every variant that
  avoids metering admins (bucket keyed on cookie presence, skipping the append on
  cookie presence, caching the lookup) either hands an attacker a free or doubled
  allowance or weakens session revocation.
- **The code is correct as written** — the `is_admin` short-circuit stays at the top
  of `ratelimit.check` (`app/ratelimit.py:56`). If this brief and that code ever
  disagree again, the code wins.

### G8. Money-path notes (flag only — Kevin decides)
- **Stripe 409 retry storm:** `app/public/pay.py:216-232` answers settled/mismatch
  webhooks 409 with an *unthrottled* alert per delivery; Stripe retries ~3 days and
  counts 4xx toward auto-disabling the endpoint — after which real payment events
  stop arriving. Proposal: record the anomaly once (throttled, keyed by event id),
  return 200 so retries stop; money state is already protected by re-derivation.
- **Notion sync outside the payment tx:** `app/public/pay.py:264` enqueues after
  commit rather than `jobs.stage(con, ...)` inside it — the app's own best pattern,
  absent from its most important transaction.
- **De-async the webhook handler** (same pattern as B9) — ~6 blocking DB
  round-trips currently run on the event loop.
- **Backfill rollback SQL for money migrations** (002/046/049/050) or codify in
  `ops/MIGRATIONS.md` that snapshot-restore is their official rollback.
- **`scripts/deploy-flow.sh` rsyncs `migrations/` and restarts** (`:19,21-30`) —
  a stale clone can apply un-merged migrations to the live DB. Drop the migrations
  line or guard with a clean-tree + `merge-base --is-ancestor` check.

---

## 9. Sequencing & PR slicing

1. **PR 1 (green):** A1–A4 + A10 (queue/db durability) — small diffs, high value.
2. **PR 2 (green):** A5–A9 (user-facing correctness: CSV, reply queue, gcal UI,
   portal crops).
3. **PR 3 (green):** B1 (admin CSS split) — isolated, screenshot-verified.
4. **PR 4 (green):** C1–C7 (SEO/CLS/a11y batch).
5. **PR 5 (green):** D1–D3 + D5–D7 (admin data accuracy), then D4 (pagination) —
   two commits or two PRs.
6. **PR 6 (green):** E1, E4, E7, E8 (CI + hygiene), then E2 (coverage) once green.
7. **PR 7 (green):** E3, E5, E6 (test debt).
8. **PR 8 (green):** B3–B9 (perf batch), D8–D10 (mechanical/refactor) last — they
   churn the most lines and should rebase onto everything above.
9. **PR F (green):** docs commit — can ship any time.
10. **G1/G2 first among red PRs** — they close the only existential risk in the
    project. Then G3–G7 in order; G8 is a findings memo unless Kevin green-lights
    specific items.

Do not parallelize PRs that touch the same files (A6/D2/D10 all live in
`activity.py` — land in that order).

## 10. Definition of done

- All four gates green at every commit; CI green on every PR.
- Each finding either fixed-with-test, or explicitly skipped with a one-line reason
  in the PR body ("stale — code changed", "needs owner decision").
- No red-light file touched outside a G PR. No new dependencies except pytest-cov.
- HANDOFF.md §6b annotated and this file's checkboxes… there are none. The PR
  bodies are the ledger; keep them accurate.
