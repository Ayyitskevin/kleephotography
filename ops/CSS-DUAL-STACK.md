# CSS dual-stack inventory (Screening Room + legacy)

Mise loads styles in this order ([`templates/base.html`](../templates/base.html)):

1. `mise.css` inside `@import … layer(mise)` — legacy cream + candlelight + editorial-dark
2. `screening-room-tokens.css` — SR design tokens
3. `screening.css` — SR components (scoped under `body.sr` / `body.sr-admin`)

and, on admin pages only ([`templates/admin/base_admin.html`](../templates/admin/base_admin.html)):

4. `mise-admin.css` inside `@import … layer(mise)` — the `.admin-shell` half of
   the legacy sheet, appended to the same layer after `mise.css`

Kill switch: `MISE_SCREENING_ROOM=false` → no `body.sr` / `sr-admin`; cream +
admin shell fall back to mise.css + mise-admin.css only. Money docs opt into
`sr-moneydoc` or stay `.cream-theme`.

## Which legacy file a rule belongs in

`base.html` is the root of **every** surface and `base_cream.html` is worn by
client pages (gallery, proposal, receipt, portal PIN, workspace, drop) as well
as admin, so only `admin/base_admin.html` may attach the admin sheet — and
`admin/login.html` extends `base_cream.html` directly, so it never gets it.

| Goes in | Rule |
|---|---|
| `mise-admin.css` | Every selector in the rule contains `.admin-shell` (the only markup wearing it is `base_admin.html`), and the rule is not order-pinned |
| `mise.css` | Everything else — shared primitives, resets, tokens, `.cream-theme`, client/money-doc chrome, marketing kill-switch blocks, **and anything ambiguous** |

Two things keep the split honest and both are asserted in
[`tests/test_static_assets.py`](../tests/test_static_assets.py):

- The admin sheet holds *only* `.admin-shell`-scoped rules, so loading it can
  never repaint a page outside the shell.
- Both sheets use the `<style>@import … layer(mise)</style>` idiom (a plain
  `<link>` cannot declare a layer) with the `?v={{ static_rev }}` cache-buster.

**`/* ORDER-PINNED */`** marks the ~30 `.admin-shell` rules that stay in
`mise.css` on purpose. Moving a rule to a sheet that loads later also moves it
later in the cascade; where an equal-specificity *unscoped* admin rule further
down `mise.css` currently wins on the same element (e.g. `.admin-shell a`'s
terracotta vs `.tr-card-top a`'s ink on `/admin/transfers`, or the
`prefers-reduced-motion` block that cancels `.ns-ind` / `.ns-crest`
transitions), the rule has to stay put. Same reason a `@media` block that mixes
admin and public selectors is not moved. If you add an unscoped admin rule that
an `.admin-shell` rule must not out-rank, pin the `.admin-shell` rule too.

## What still matters when Screening Room is ON

| Layer | Required when `body.sr` | Notes |
|-------|-------------------------|--------|
| `screening-room-tokens.css` + `screening.css` | Yes | Primary look for marketing + admin deck |
| `mise-admin.css` `.admin-shell` rules | Yes | Many admin components still share mise primitives under SR remaps |
| `mise.css` `.cream-theme` | Yes (kill switch + money docs) | Login + invoice/proposal/receipt when SR off or moneydoc off |
| `mise.css` `.site-body` cream + candlelight + editorial-dark | Kill-switch / non-SR marketing only | SR overrides via higher-priority unlayered CSS |
| `fonts.css` | Yes | Shared self-hosted faces |

## Quarantine policy

- Do **not** delete `.cream-theme` or kill-switch marketing chrome until Kevin
  retires `MISE_SCREENING_ROOM=false` as an operator rollback.
- Prefer marking superseded marketing blocks with
  `/* LEGACY-KILL-SWITCH — keep while MISE_SCREENING_ROOM can be false */`
  rather than silent deletion.
- New marketing UI goes in `screening.css`, not new unscoped rules in `mise.css`.
- Admin-only additions stay under `.admin-shell` or `body.sr-admin` in screening.css.
- Legacy admin edits go in `mise-admin.css`; a new rule there must be
  `.admin-shell`-scoped or the split test fails.

## Prune log (this pass)

- Documented the stack (this file).
- Bannered the superseded **Candlelight / After Dark** marketing block in
  `mise.css` as kill-switch-only (not deleted — rollback path).
- Bannered the cream `.site-body` marketing system and the **Editorial Dark**
  reskin the same way (still required for `MISE_SCREENING_ROOM=false`).
- No hero/marketing redesign in this wave; no deletion of `.cream-theme`.

### 2026-07-23 — dead-class excavation (UX revamp, final phase)

Consumption analysis (1,762 classes in `mise.css`, cross-referenced against
templates/app/JS incl. dynamic compositions) found **514 dead in both modes**,
all orphaned by redesigns 2–5 weeks prior. Deleted them: `mise.css` drops from
8,669 → 3,120 lines. Largest families: `ad-*` After Dark (139 — **overrides the
banner-only caution above**: evidence showed zero `ad-*` emitted in either mode),
old admin `dash-*`/`home-*`/`sched-*`, `kanban-*` (renamed `stu-*`), the
editorial-dark `ework/eh/emotion/e*` sections, `gal2-*`, old proofing
`crop/proof-*`, `status-col-*`, `stage-archived`, `is-amber/red/oldest`. Also
deleted 13 entangled orphans from `screening.css` (`btn-saffron`, `btn-ghost-dark`,
`v4-btn-solid/gold/ghost`, `icon-btn`, `gd-btn-sm`, `sp-pill`, `it-accent`,
`work-back`, `dash-check`, `svc-foot-sec`, `eyebrow`, `fin-export-btn`,
`ib-reply-btn`) — markup long migrated to `sr-btn`/`sr-icon-btn`.
**Kill-switch-safe by construction:** the 29 cream-nav (`ns-*`) and cream-login
(`login-*`) classes the rollback path still uses were kept and sit under the
existing banner. Verification: full gates (unit/integration/smoke/ruff) +
before/after screenshots on both themes + zero remaining references by script
assertion.

### 2026-08-09 — admin split (`mise-admin.css`)

`mise.css` was 364 KB raw / 78 KB gzip on **every** surface, ~62% of it
`.admin-shell`-scoped and unmatchable on a public page. Moved 1,455 blocks
(rules, their comments, and the `@media`/`@container` blocks that contain only
admin rules) verbatim into `mise-admin.css` — no rule bodies edited, original
relative order kept, so a regression bisects cleanly.

| Sheet | Raw | gzip -9 |
|---|---|---|
| `mise.css` before | 364,296 B | 78,027 B |
| `mise.css` after (what every public visitor fetches) | 138,400 B | 30,477 B |
| `mise-admin.css` (admin only) | 227,336 B | 48,267 B |

30 rules stayed behind under `/* ORDER-PINNED */` (see above). Verification:
full gates + Playwright before/after on both flag states (`MISE_SCREENING_ROOM`
default and `=false`), comparing 38 screenshots and the **full computed style
of every element** on 45 pages (36 admin surfaces incl. `/admin/login`, 9
public). Every surviving delta was a live animation or a clock (`.ns-pulse`
keyframes, `.ns-ind` transition, scroll reveals, the ticker, inbox timestamps);
no static property changed. Not covered: hover/focus/open states, the client
gallery/portal/money-doc pages (no seeded PIN session in the harness), and
print styles.
