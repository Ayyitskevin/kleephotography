# CSS dual-stack inventory (Screening Room + legacy)

Mise loads styles in this order ([`templates/base.html`](../templates/base.html)):

1. `mise.css` inside `@import … layer(mise)` — legacy cream + candlelight + editorial-dark + admin
2. `screening-room-tokens.css` — SR design tokens
3. `screening.css` — SR components (scoped under `body.sr` / `body.sr-admin`)

Kill switch: `MISE_SCREENING_ROOM=false` → no `body.sr` / `sr-admin`; cream +
admin shell fall back to mise.css only. Money docs opt into `sr-moneydoc` or
stay `.cream-theme`.

## What still matters when Screening Room is ON

| Layer | Required when `body.sr` | Notes |
|-------|-------------------------|--------|
| `screening-room-tokens.css` + `screening.css` | Yes | Primary look for marketing + admin deck |
| `mise.css` admin / `.admin-shell` rules | Yes | Many admin components still share mise primitives under SR remaps |
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
