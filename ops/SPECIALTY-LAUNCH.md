# Specialty launch runbook — 3-vertical flagship site

The public site now runs hub-and-spoke: `/` routes visitors to
`/real-estate`, `/portraits`, and `/food-beverage`. All specialty grouping
derives from **portfolio tags** — no schema changed. This runbook is the
operator side: how to tag work, create the booking event types, and verify
the launch. Everything here happens in the **live admin**; nothing needs a
deploy beyond the revamp itself.

## 1. The tag convention (app/specialties.py)

One free-text tag per starred asset (`assets.portfolio_tag`), with an
optional specialty prefix:

| Tag shape | Specialty | Examples |
|---|---|---|
| `re/<subject>` | Real Estate | `re/exteriors`, `re/interiors`, `re/twilight`, `re/aerial`, `re/walkthrough` |
| `pl/<subject>` | Portrait & Lifestyle | `pl/headshots`, `pl/branding`, `pl/family`, `pl/golden-hour` |
| `fb/<subject>` or **no prefix** | Food & Beverage | `Dishes`, `Drinks`, `fb/motion` |

- **Unprefixed tags mean F&B.** The entire pre-revamp archive is F&B, so
  nothing needs re-tagging. Tag *new* RE/portrait work with prefixes.
- The prefix is stripped everywhere it displays ("re/exteriors" shows as
  "Exteriors"); it only drives grouping (spokes, /portfolio and /reels
  chips, /work groups, homepage doors) and the image alt-text craft phrase.
- Tags are set per asset on the gallery page (the same tag field as
  always); the datalist now suggests the prefixed vocabulary.
- **Case studies** inherit their specialty from the majority tag-prefix of
  their starred assets — star and tag the assets and the case study files
  itself under the right vertical automatically.
- Videos use the same tags. A video tagged `re/walkthrough` appears on
  /real-estate's motion band and under Real Estate on /reels.

## 2. Booking event types (live admin → Scheduling)

Event types are DB rows — create these in the live admin, no commit needed.
**The slug prefix matters**: the public booking form shows real-estate
intake labels for slugs starting `re-`, portrait labels for `pl-`, and the
original F&B intake for everything else (legacy event types keep working
unchanged).

Suggested setup (durations/buffers are starting points — adjust to taste):

| Field | Real Estate | Portrait session | F&B shoot |
|---|---|---|---|
| Name | Real Estate Shoot | Portrait Session | F&B Shoot |
| **Slug** | `re-shoot` | `pl-session` | `fb-shoot` |
| Duration | 120 min | 90 min | 240 min |
| Buffer before/after | 30 / 30 | 15 / 30 | 30 / 30 |
| Min notice | 24 h | 24 h | 48 h |
| Booking window | 45 days | 60 days | 60 days |
| "Creates session" | ✓ on | ✓ on | ✓ on |

- **Golden-hour / twilight window:** for the RE and portrait event types,
  add per-event availability windows that include the evening block (e.g.
  a second window 17:00–20:00) so twilight exteriors and golden-hour
  sessions are actually bookable. Availability rules are per-event-type on
  the same admin page.
- The 15-min `intro-call` event type stays as-is (it isn't a shoot; no
  intake fields render for it).
- Spoke CTAs currently link `/book` (the index lists every active event
  type). Once the three event types exist, deep-linking each spoke's CTA
  to `/book/re-shoot` etc. is a one-line template change per spoke —
  flag it and it ships green.

## 3. Launch checklist

1. **Tag & star** at least ~6 photos + 1–2 videos per new vertical
   (RE, PL) so the spokes and homepage doors render with real work instead
   of empty states. F&B needs nothing.
2. **Create the three event types** (§2) and set their availability
   windows.
3. **Check the live sitemap** (`/sitemap.xml`) for a seeded demo case
   study: `MISE_SHOWCASE_SEED` auto-publishes gallery #1 as a demo
   (`/work/{slug}`). If one is live and unwanted, unpublish it from the
   gallery's case-study settings.
4. **Contract templates:** the videography agreement already exists; RE
   and portrait service agreements arrive via the red-light PR (Kevin
   merges; see PR list in HANDOFF).
5. **Pricing:** the public /services page still shows the pre-revamp
   photography/videography/brand-partner tiers; per-specialty packages are
   in the red-light pricing PR (includes the $650-display vs $900-anchor
   question on Photography Starter).
6. **Post-deploy spot checks:** `/`, the three spokes, `/portfolio`
   (chips), `/work` (groups), `/about`, `/contact` (new project types),
   one live client gallery (video MP4 button + duration badge), one
   invoice/contract/proposal link, `/book` + one event page.
7. **Search Console:** after deploy, watch the F&B queries for a couple of
   weeks. The F&B copy moved to `/food-beverage` (home links to it from
   the doors); if F&B impressions dip hard, the F&B spoke can absorb more
   of the old home copy — it's all green-light template work.
