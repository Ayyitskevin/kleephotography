# P2-W2A: rule map, sealed base b2ae5b9 -> exact head c9eaf85 (PR #125)

Generated mechanically by `_evidence/P2-W2A-map/gen.py`; `replay.py` applies these blocks, as written here, to the base sheets and
asserts byte equality with the head sheets. Blocks are difflib line hunks over the full file text; the `touches` line names the CSS rule(s)
(selector, enclosing @media) the hunk lies in or inserts next to. Nothing is truncated or summarised.

**Fold note.** This file is committed in the docs-only fold commit on top of c9eaf85. That commit changes no file under `static/`, so the three
sheets are byte-identical at the fold head and at c9eaf85 (see `P2-W2A-REPLAY.txt` for the sha256 and git blob ids). `_evidence/P2-W2A-map/` lives in the
research packet folder, not in this repo.

## Summary

| sheet | base lines | head lines | blocks | distinct rules touched |
|---|---:|---:|---:|---:|
| screening-room-tokens.css | 173 | 188 | 7 | 8 |
| screening.css | 1455 | 1463 | 181 | 216 |
| screening-admin.css | 919 | 919 | 23 | 27 |

Total: **211 blocks**; 251 (sheet, rule) pairs touched.
Rule-level entries (same method as `count.py`): **251** = 245 changed + 4 added + 2 removed.

## screening-room-tokens.css  (7 blocks)

### screening-room-tokens.css #1 [insert] base lines (none, after 93) -> head lines 94-105
touches: body.sr
<<<OLD screening-room-tokens.css 1 93 93
>>>

<<<NEW screening-room-tokens.css 1 93 105
  /* type scale (plan D-1): nothing below 11px; body copy 15px; one clamp per display step.
     xs = mono labels, sm = captions / frame labels / nav / usage lines, base = body,
     lg = lede, xl = pull quotes; d-1 hero h1, d-2 split/feature/contact h1, d-3 section
     h2, d-4 card and tier h3. Labels track at 0.18em (the wider values compensated for 10px). */
  --sr-t-xs: 11px; --sr-t-sm: 13px; --sr-t-base: 15px; --sr-t-lg: 17px; --sr-t-xl: 20px;
  --sr-d-1: clamp(36px, 5vw, 68px); --sr-d-2: clamp(30px, 3.6vw, 48px);
  --sr-d-3: clamp(22px, 2.4vw, 32px); --sr-d-4: 20px;
  --sr-track-label: 0.18em;

  /* section air (plan D-2): the space above and below every .sr-section */
  --sr-section-y: clamp(48px, 7vw, 96px);

>>>

### screening-room-tokens.css #2 [replace] base lines 106-106 -> head lines 118-121
touches: .sr .sr-title | .sr .sr-h2
<<<OLD screening-room-tokens.css 2 105 106
.sr .sr-title { font: 700 clamp(32px, 4.6vw, 68px)/1.02 var(--sr-sans);
>>>

<<<NEW screening-room-tokens.css 2 117 121
.sr .sr-title { font: 700 var(--sr-d-1)/1.02 var(--sr-sans);
  letter-spacing: 0.04em; text-transform: uppercase; color: var(--sr-cream-strong); }
/* section heading: every marketing section h2 (d-3) */
.sr .sr-h2 { font: 700 var(--sr-d-3)/1.1 var(--sr-sans);
>>>

### screening-room-tokens.css #3 [replace] base lines 114-114 -> head lines 129-129
touches: .sr .sr-mono
<<<OLD screening-room-tokens.css 3 113 114
.sr .sr-mono { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.26em;
>>>

<<<NEW screening-room-tokens.css 3 128 129
.sr .sr-mono { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening-room-tokens.css #4 [replace] base lines 122-122 -> head lines 137-137
touches: .sr .sr-btn
<<<OLD screening-room-tokens.css 4 121 122
  padding: 14px 26px; border: 0; cursor: pointer;
>>>

<<<NEW screening-room-tokens.css 4 136 137
  padding: var(--sr-s4) var(--sr-s5); border: 0; cursor: pointer;
>>>

### screening-room-tokens.css #5 [replace] base lines 134-135 -> head lines 149-150
touches: .sr .sr-filmchip
<<<OLD screening-room-tokens.css 5 133 135
  font: 700 9px/1 var(--sr-mono); letter-spacing: 0.14em;
  color: var(--sr-house); background: var(--sr-stock); padding: 4px 8px; }
>>>

<<<NEW screening-room-tokens.css 5 148 150
  font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
  color: var(--sr-house); background: var(--sr-stock); padding: var(--sr-s1) var(--sr-s2); }
>>>

### screening-room-tokens.css #6 [replace] base lines 157-157 -> head lines 172-172
touches: .sr .sr-rec
<<<OLD screening-room-tokens.css 6 156 157
.sr .sr-rec { font: 700 10px/1 var(--sr-mono); letter-spacing: 0.2em; color: var(--sr-amber); }
>>>

<<<NEW screening-room-tokens.css 6 171 172
.sr .sr-rec { font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label); color: var(--sr-amber); }
>>>

### screening-room-tokens.css #7 [replace] base lines 164-164 -> head lines 179-179
touches: .sr .sr-export
<<<OLD screening-room-tokens.css 7 163 164
  display: flex; align-items: center; gap: 20px; padding: 15px 48px; }
>>>

<<<NEW screening-room-tokens.css 7 178 179
  display: flex; align-items: center; gap: var(--sr-s5); padding: 15px 48px; }
>>>

## screening.css  (181 blocks)

### screening.css #1 [replace] base lines 26-26 -> head lines 26-26
touches: .sr .sr-section
<<<OLD screening.css 1 25 26
.sr .sr-section { padding-block: 40px 8px; }
>>>

<<<NEW screening.css 1 25 26
.sr .sr-section { padding-block: var(--sr-section-y); }
>>>

### screening.css #2 [replace] base lines 31-32 -> head lines 31-32
touches: .sr .sr-kickrow | .sr .sr-kickrow .sr-mono
<<<OLD screening.css 2 30 32
  gap: 16px; margin: 0 0 16px; flex-wrap: wrap; }
.sr .sr-kickrow .sr-mono { letter-spacing: 0.3em; }
>>>

<<<NEW screening.css 2 30 32
  gap: var(--sr-s4); margin: 0 0 var(--sr-s5); flex-wrap: wrap; }
.sr .sr-kickrow .sr-mono { letter-spacing: var(--sr-track-label); }
>>>

### screening.css #3 [replace] base lines 35-37 -> head lines 35-37
touches: .sr .sr-pagehead | .sr .sr-pagehead .sr-mono | .sr .sr-pagehead h1
<<<OLD screening.css 3 34 37
.sr .sr-pagehead { padding: 56px 0 34px; border-bottom: 1px solid var(--sr-line); }
.sr .sr-pagehead .sr-mono { display: block; margin: 0 0 16px; letter-spacing: 0.34em; }
.sr .sr-pagehead h1 { margin: 0 0 12px; font: 700 clamp(30px, 3.8vw, 54px)/1.02 var(--sr-sans);
>>>

<<<NEW screening.css 3 34 37
.sr .sr-pagehead { padding: var(--sr-s8) 0 var(--sr-s7); border-bottom: 1px solid var(--sr-line); }
.sr .sr-pagehead .sr-mono { display: block; margin: 0 0 var(--sr-s4); letter-spacing: 0.34em; }
.sr .sr-pagehead h1 { margin: 0 0 var(--sr-s3); font: 700 clamp(30px, 3.8vw, 54px)/1.02 var(--sr-sans);
>>>

### screening.css #4 [insert] base lines (none, after 39) -> head lines 40-43
touches: .site-body.sr:has(.site-nav nav a[aria-current="page"][href="/portfolio"]) .sr-pagehead h1
<<<OLD screening.css 4 39 39
>>>

<<<NEW screening.css 4 39 43
/* D-1: the two pages whose h1 is a hero (portfolio, services) take the top display size. The nav
   link that carries aria-current is the page marker; browsers without :has() keep the base size. */
.site-body.sr:has(.site-nav nav a[aria-current="page"][href="/portfolio"]) .sr-pagehead h1,
.site-body.sr:has(.site-nav nav a[aria-current="page"][href="/services"]) .sr-pagehead h1 { font-size: var(--sr-d-1); }
>>>

### screening.css #5 [replace] base lines 41-41 -> head lines 45-45
touches: .sr .sr-lede
<<<OLD screening.css 5 40 41
.sr .sr-lede { font-size: 14px; line-height: 1.7; color: var(--sr-ink-mute); max-width: 60ch; }
>>>

<<<NEW screening.css 5 44 45
.sr .sr-lede { font-size: var(--sr-t-lg); line-height: 1.65; color: var(--sr-ink-mute); max-width: 60ch; }
>>>

### screening.css #6 [replace] base lines 45-45 -> head lines 49-49
touches: .sr .site-nav
<<<OLD screening.css 6 44 45
  justify-content: space-between; gap: 18px; padding: 18px 48px;
>>>

<<<NEW screening.css 6 48 49
  justify-content: space-between; gap: var(--sr-s4); padding: var(--sr-s4) 48px;
>>>

### screening.css #7 [replace] base lines 53-53 -> head lines 57-57
touches: .sr .site-brand
<<<OLD screening.css 7 52 53
.sr .site-brand { display: flex; align-items: center; gap: 14px; line-height: 1;
>>>

<<<NEW screening.css 7 56 57
.sr .site-brand { display: flex; align-items: center; gap: var(--sr-s4); line-height: 1;
>>>

### screening.css #8 [replace] base lines 57-57 -> head lines 61-61
touches: .sr .site-brand small
<<<OLD screening.css 8 56 57
.sr .site-brand small { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.2em;
>>>

<<<NEW screening.css 8 60 61
.sr .site-brand small { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #9 [replace] base lines 59-61 -> head lines 63-65
touches: .sr .site-brand small | .sr .site-nav nav | .sr .site-nav nav a
<<<OLD screening.css 9 58 61
  color-mix(in srgb, var(--sr-stock) 45%, transparent); padding: 4px 8px; white-space: nowrap; }
.sr .site-nav nav { display: flex; align-items: center; gap: 18px; flex-wrap: nowrap; }
.sr .site-nav nav a { font: 600 11px/1 var(--sr-sans); letter-spacing: 0.12em;
>>>

<<<NEW screening.css 9 62 65
  color-mix(in srgb, var(--sr-stock) 45%, transparent); padding: var(--sr-s1) var(--sr-s2); white-space: nowrap; }
.sr .site-nav nav { display: flex; align-items: center; gap: var(--sr-s4); flex-wrap: nowrap; }
.sr .site-nav nav a { font: 600 var(--sr-t-xs)/1 var(--sr-sans); letter-spacing: 0.12em;
>>>

### screening.css #10 [insert] base lines (none, after 63) -> head lines 68-70
touches: @media (min-width: 1366px) .sr .site-nav nav a
<<<OLD screening.css 10 63 63
>>>

<<<NEW screening.css 10 67 70
/* D-1: nav links are 13px where the one-line nav has room (the 1080px burger breakpoint is pinned by tests
   and 13px links need ~1,230px); below that they stay at the 11px floor. */
@media (min-width: 1366px) { .sr .site-nav nav a { font-size: var(--sr-t-sm); } }
>>>

### screening.css #11 [replace] base lines 66-66 -> head lines 73-73
touches: .sr .site-nav nav a.nav-cta
<<<OLD screening.css 11 65 66
  padding: 11px 18px; font-weight: 700; border-radius: 0; }
>>>

<<<NEW screening.css 11 72 73
  padding: 11px var(--sr-s4); font-weight: 700; border-radius: 0; }
>>>

### screening.css #12 [replace] base lines 70-71 -> head lines 77-78
touches: .sr .nav-menu-btn
<<<OLD screening.css 12 69 71
  width: 44px; height: 44px; padding: 12px 10px; cursor: pointer;
  flex-direction: column; justify-content: center; gap: 6px;
>>>

<<<NEW screening.css 12 76 78
  width: 44px; height: 44px; padding: var(--sr-s3) var(--sr-s3); cursor: pointer;
  flex-direction: column; justify-content: center; gap: var(--sr-s2);
>>>

### screening.css #13 [replace] base lines 82-82 -> head lines 89-89
touches: .sr .nav-mobile
<<<OLD screening.css 13 81 82
  flex-direction: column; align-items: stretch; justify-content: flex-start; gap: 4px;
>>>

<<<NEW screening.css 13 88 89
  flex-direction: column; align-items: stretch; justify-content: flex-start; gap: var(--sr-s1);
>>>

### screening.css #14 [replace] base lines 92-92 -> head lines 99-99
touches: .sr .nav-mobile a
<<<OLD screening.css 14 91 92
  padding: 14px 0; border-bottom: 1px solid var(--sr-line); }
>>>

<<<NEW screening.css 14 98 99
  padding: var(--sr-s4) 0; border-bottom: 1px solid var(--sr-line); }
>>>

### screening.css #15 [replace] base lines 94-94 -> head lines 101-101
touches: .sr .nav-mobile a.nav-cta
<<<OLD screening.css 15 93 94
  text-align: center; margin-top: 18px; padding: 16px 0; border-bottom: 0; }
>>>

<<<NEW screening.css 15 100 101
  text-align: center; margin-top: var(--sr-s4); padding: var(--sr-s4) 0; border-bottom: 0; }
>>>

### screening.css #16 [replace] base lines 96-96 -> head lines 103-103
touches: .sr .nav-mobile .nav-mobile-quiet
<<<OLD screening.css 16 95 96
  color: var(--sr-ink-soft); padding: 12px 0; border-bottom: 0; }
>>>

<<<NEW screening.css 16 102 103
  color: var(--sr-ink-soft); padding: var(--sr-s3) 0; border-bottom: 0; }
>>>

### screening.css #17 [replace] base lines 100-103 -> head lines 107-110
touches: .sr .site-footer | .sr .sr-credits-label | .sr .sr-credits
<<<OLD screening.css 17 99 103
  margin-top: 56px; padding: 34px 48px 30px; text-align: center; }
.sr .sr-credits-label { margin: 0 0 6px; font: 500 9.5px/1 var(--sr-mono);
  letter-spacing: 0.3em; text-transform: uppercase; color: var(--sr-ink-ghost); }
.sr .sr-credits { margin: 0 0 18px; font-size: 13px; color: var(--sr-ink-soft); }
>>>

<<<NEW screening.css 17 106 110
  margin-top: var(--sr-section-y); padding: 34px 48px var(--sr-s6); text-align: center; }
.sr .sr-credits-label { margin: 0 0 var(--sr-s2); font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-ink-ghost); }
.sr .sr-credits { margin: 0 0 var(--sr-s4); font-size: 13px; color: var(--sr-ink-soft); }
>>>

### screening.css #18 [replace] base lines 105-106 -> head lines 112-113
touches: .sr .sr-footlinks
<<<OLD screening.css 18 104 106
.sr .sr-footlinks { display: flex; justify-content: center; gap: 26px; flex-wrap: wrap;
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase; }
>>>

<<<NEW screening.css 18 111 113
.sr .sr-footlinks { display: flex; justify-content: center; gap: var(--sr-s5); flex-wrap: wrap;
  font-size: var(--sr-t-xs); letter-spacing: 0.16em; text-transform: uppercase; }
>>>

### screening.css #19 [replace] base lines 110-114 -> head lines 117-121
touches: .sr .sr-footlinks-quiet | .sr .sr-footlinks-quiet a | .sr .sr-footbase
<<<OLD screening.css 19 109 114
.sr .sr-footlinks-quiet { margin-top: 16px; gap: 18px; }
.sr .sr-footlinks-quiet a { color: var(--sr-ink-ghost); font-size: 10px; }
.sr .sr-footbase { margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--sr-line-faint);
  display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font: 500 11px/1.6 var(--sr-mono); letter-spacing: 0.18em; text-transform: uppercase;
>>>

<<<NEW screening.css 19 116 121
.sr .sr-footlinks-quiet { margin-top: var(--sr-s4); gap: var(--sr-s4); }
.sr .sr-footlinks-quiet a { color: var(--sr-ink-ghost); font-size: var(--sr-t-xs); }
.sr .sr-footbase { margin-top: var(--sr-s5); padding-top: var(--sr-s4); border-top: 1px solid var(--sr-line-faint);
  display: flex; justify-content: space-between; gap: var(--sr-s3); flex-wrap: wrap;
  font: 500 var(--sr-t-xs)/1.6 var(--sr-mono); letter-spacing: 0.18em; text-transform: uppercase;
>>>

### screening.css #20 [replace] base lines 142-143 -> head lines 149-150
touches: .sr .sr-screen-title h1, .sr .sr-screen-title h2
<<<OLD screening.css 20 141 143
.sr .sr-screen-title h1, .sr .sr-screen-title h2 { margin: 0 0 10px;
  font: 700 clamp(30px, 4.7vw, 68px)/1.02 var(--sr-sans); letter-spacing: 0.04em;
>>>

<<<NEW screening.css 20 148 150
.sr .sr-screen-title h1, .sr .sr-screen-title h2 { margin: 0 0 var(--sr-s3);
  font: 700 var(--sr-d-1)/1.02 var(--sr-sans); letter-spacing: 0.04em;
>>>

### screening.css #21 [replace] base lines 151-151 -> head lines 158-158
touches: .sr .sr-play
<<<OLD screening.css 21 150 151
  font-size: 20px; padding-left: 4px; z-index: 2; cursor: pointer; }
>>>

<<<NEW screening.css 21 157 158
  font-size: 20px; padding-left: var(--sr-s1); z-index: 2; cursor: pointer; }
>>>

### screening.css #22 [replace] base lines 156-157 -> head lines 163-164
touches: .sr .sr-playerstrip
<<<OLD screening.css 22 155 157
  display: flex; align-items: center; gap: 14px; z-index: 2;
  font: 400 10px/1 var(--sr-mono); color: var(--sr-ink); }
>>>

<<<NEW screening.css 22 162 164
  display: flex; align-items: center; gap: var(--sr-s4); z-index: 2;
  font: 400 var(--sr-t-xs)/1 var(--sr-mono); color: var(--sr-ink); }
>>>

### screening.css #23 [replace] base lines 170-170 -> head lines 177-177
touches: .sr .sr-screenbadge
<<<OLD screening.css 23 169 170
  font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.2em; text-transform: uppercase;
>>>

<<<NEW screening.css 23 176 177
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label); text-transform: uppercase;
>>>

### screening.css #24 [replace] base lines 175-175 -> head lines 182-182
touches: .sr .sr-features
<<<OLD screening.css 24 174 175
.sr .sr-features { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
>>>

<<<NEW screening.css 24 181 182
.sr .sr-features { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sr-s4); }
>>>

### screening.css #25 [replace] base lines 187-187 -> head lines 194-194
touches: .sr .sr-card-head
<<<OLD screening.css 25 186 187
  gap: 10px; padding: 12px 16px; border-bottom: 1px solid
>>>

<<<NEW screening.css 25 193 194
  gap: var(--sr-s3); padding: var(--sr-s3) var(--sr-s4); border-bottom: 1px solid
>>>

### screening.css #26 [replace] base lines 189-189 -> head lines 196-196
touches: .sr .sr-card-head .sr-mono
<<<OLD screening.css 26 188 189
.sr .sr-card-head .sr-mono { letter-spacing: 0.18em; font-size: 9.5px; }
>>>

<<<NEW screening.css 26 195 196
.sr .sr-card-head .sr-mono { letter-spacing: 0.18em; font-size: var(--sr-t-xs); }
>>>

### screening.css #27 [replace] base lines 191-191 -> head lines 198-198
touches: .sr .sr-card-head .sr-card-count
<<<OLD screening.css 27 190 191
.sr .sr-card-head .sr-card-count { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.08em;
>>>

<<<NEW screening.css 27 197 198
.sr .sr-card-head .sr-card-count { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.08em;
>>>

### screening.css #28 [replace] base lines 202-202 -> head lines 209-209
touches: .sr .sr-card-title small
<<<OLD screening.css 28 201 202
.sr .sr-card-title small { font-size: 11px; color: color-mix(in srgb, var(--sr-stock) 55%, var(--sr-ink)); }
>>>

<<<NEW screening.css 28 208 209
.sr .sr-card-title small { font-size: var(--sr-t-xs); color: color-mix(in srgb, var(--sr-stock) 55%, var(--sr-ink)); }
>>>

### screening.css #29 [replace] base lines 204-205 -> head lines 211-212
touches: .sr .sr-durchip
<<<OLD screening.css 29 203 205
  font: 500 11px/1 var(--sr-mono); color: var(--sr-cream);
  background: rgba(8, 9, 11, 0.6); padding: 4px 7px; }
>>>

<<<NEW screening.css 29 210 212
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); color: var(--sr-cream);
  background: rgba(8, 9, 11, 0.6); padding: var(--sr-s1) 7px; }
>>>

### screening.css #30 [replace] base lines 211-211 -> head lines 218-218
touches: .sr .sr-ticker
<<<OLD screening.css 30 210 211
  border-bottom: 1px solid var(--sr-line); overflow: hidden; padding: 12px 0; }
>>>

<<<NEW screening.css 30 217 218
  border-bottom: 1px solid var(--sr-line); overflow: hidden; padding: var(--sr-s3) 0; }
>>>

### screening.css #31 [replace] base lines 215-215 -> head lines 222-222
touches: .sr .sr-ticker-seq
<<<OLD screening.css 31 214 215
  font: 500 10.5px/1 var(--sr-mono); letter-spacing: 0.22em; text-transform: uppercase;
>>>

<<<NEW screening.css 31 221 222
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label); text-transform: uppercase;
>>>

### screening.css #32 [replace] base lines 220-220 -> head lines 227-227
touches: @media (prefers-reduced-motion: reduce) .sr .sr-ticker-seq
<<<OLD screening.css 32 219 220
  .sr .sr-ticker-seq { flex-wrap: wrap; white-space: normal; row-gap: 10px;
>>>

<<<NEW screening.css 32 226 227
  .sr .sr-ticker-seq { flex-wrap: wrap; white-space: normal; row-gap: var(--sr-s3);
>>>

### screening.css #33 [replace] base lines 231-232 -> head lines 238-239
touches: .sr .sr-stats | .sr .sr-stat
<<<OLD screening.css 33 230 232
  border: 1px solid var(--sr-line); margin-top: 26px; }
.sr .sr-stat { padding: 20px 22px; border-right: 1px solid var(--sr-line); }
>>>

<<<NEW screening.css 33 237 239
  border: 1px solid var(--sr-line); margin-top: var(--sr-s6); }
.sr .sr-stat { padding: var(--sr-s5) var(--sr-s5); border-right: 1px solid var(--sr-line); }
>>>

### screening.css #34 [replace] base lines 235-236 -> head lines 242-243
touches: .sr .sr-stat b .sr-stat-unit | .sr .sr-stat small
<<<OLD screening.css 34 234 236
.sr .sr-stat b .sr-stat-unit { font-size: 11px; color: var(--sr-ink-faint); }
.sr .sr-stat small { font-size: 9.5px; letter-spacing: 0.14em; text-transform: uppercase;
>>>

<<<NEW screening.css 34 241 243
.sr .sr-stat b .sr-stat-unit { font-size: var(--sr-t-xs); color: var(--sr-ink-faint); }
.sr .sr-stat small { font-size: var(--sr-t-xs); letter-spacing: 0.14em; text-transform: uppercase;
>>>

### screening.css #35 [replace] base lines 241-241 -> head lines 248-248
touches: .sr .sr-stat--cta
<<<OLD screening.css 35 240 241
  padding: 14px 18px; }
>>>

<<<NEW screening.css 35 247 248
  padding: var(--sr-s4) var(--sr-s4); }
>>>

### screening.css #36 [replace] base lines 246-246 -> head lines 253-253
touches: .sr .sr-3up > div
<<<OLD screening.css 36 245 246
.sr .sr-3up > div { padding: 26px 30px; border-right: 1px solid var(--sr-line); }
>>>

<<<NEW screening.css 36 252 253
.sr .sr-3up > div { padding: var(--sr-s6); border-right: 1px solid var(--sr-line); }
>>>

### screening.css #37 [replace] base lines 248-253 -> head lines 255-260
touches: .sr .sr-3up .sr-3up-kick | .sr .sr-3up .sr-3up-q | .sr .sr-3up p | .sr .sr-3up h3
<<<OLD screening.css 37 247 253
.sr .sr-3up .sr-3up-kick { margin: 0 0 10px; font: 500 11px/1 var(--sr-mono);
  letter-spacing: 0.22em; text-transform: uppercase; color: var(--sr-stock); }
.sr .sr-3up .sr-3up-q { margin: 0 0 8px; font-family: var(--sr-serif); font-style: italic;
  font-size: 16px; color: color-mix(in srgb, var(--sr-stock) 45%, var(--sr-ink)); }
.sr .sr-3up p { margin: 0; font-size: 12.5px; line-height: 1.68; color: var(--sr-ink-mute); }
.sr .sr-3up h3 { margin: 0 0 8px; font: 700 14px/1.3 var(--sr-sans); letter-spacing: 0.04em;
>>>

<<<NEW screening.css 37 254 260
.sr .sr-3up .sr-3up-kick { margin: 0 0 var(--sr-s3); font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-stock); }
.sr .sr-3up .sr-3up-q { margin: 0 0 var(--sr-s2); font-family: var(--sr-serif); font-style: italic;
  font-size: var(--sr-t-xl); color: color-mix(in srgb, var(--sr-stock) 45%, var(--sr-ink)); }
.sr .sr-3up p { margin: 0; font-size: var(--sr-t-base); line-height: 1.6; color: var(--sr-ink-mute); }
.sr .sr-3up h3 { margin: 0 0 var(--sr-s2); font: 700 var(--sr-d-4)/1.3 var(--sr-sans); letter-spacing: 0.04em;
>>>

### screening.css #38 [replace] base lines 257-257 -> head lines 264-264
touches: .sr .sr-filmstrip
<<<OLD screening.css 38 256 257
.sr .sr-filmstrip { display: flex; gap: 6px; padding: 12px 0; overflow-x: auto;
>>>

<<<NEW screening.css 38 263 264
.sr .sr-filmstrip { display: flex; gap: var(--sr-s2); padding: var(--sr-s3) 0; overflow-x: auto;
>>>

### screening.css #39 [replace] base lines 267-267 -> head lines 274-274
touches: .sr .sr-frameno
<<<OLD screening.css 39 266 267
  font: 400 11px/1 var(--sr-mono); color: rgba(236, 228, 211, 0.7); letter-spacing: 0.06em; }
>>>

<<<NEW screening.css 39 273 274
  font: 400 var(--sr-t-sm)/1 var(--sr-mono); color: rgba(236, 228, 211, 0.7); letter-spacing: 0.06em; }
>>>

### screening.css #40 [replace] base lines 271-274 -> head lines 278-281
touches: .sr .sr-premiere | .sr .sr-chapters | .sr .sr-chapters .sr-mono | .sr .sr-chapter
<<<OLD screening.css 40 270 274
.sr .sr-premiere { display: grid; grid-template-columns: 1fr 320px; gap: 18px; }
.sr .sr-chapters { display: flex; flex-direction: column; gap: 10px; }
.sr .sr-chapters .sr-mono { margin: 0 0 2px; letter-spacing: 0.24em; }
.sr .sr-chapter { display: flex; align-items: center; gap: 12px; border: 1px solid
>>>

<<<NEW screening.css 40 277 281
.sr .sr-premiere { display: grid; grid-template-columns: 1fr 320px; gap: var(--sr-s4); }
.sr .sr-chapters { display: flex; flex-direction: column; gap: var(--sr-s3); }
.sr .sr-chapters .sr-mono { margin: 0 0 2px; letter-spacing: var(--sr-track-label); }
.sr .sr-chapter { display: flex; align-items: center; gap: var(--sr-s3); border: 1px solid
>>>

### screening.css #41 [replace] base lines 276-276 -> head lines 283-283
touches: .sr .sr-chapter
<<<OLD screening.css 41 275 276
  padding: 12px 14px; font: 400 12.5px/1.2 var(--sr-sans); color: var(--sr-cream);
>>>

<<<NEW screening.css 41 282 283
  padding: var(--sr-s3) var(--sr-s4); font: 400 12.5px/1.2 var(--sr-sans); color: var(--sr-cream);
>>>

### screening.css #42 [replace] base lines 279-279 -> head lines 286-286
touches: .sr .sr-chapter i
<<<OLD screening.css 42 278 279
.sr .sr-chapter i { font: 400 10px/1 var(--sr-mono); font-style: normal;
>>>

<<<NEW screening.css 42 285 286
.sr .sr-chapter i { font: 400 var(--sr-t-xs)/1 var(--sr-mono); font-style: normal;
>>>

### screening.css #43 [replace] base lines 281-281 -> head lines 288-288
touches: .sr .sr-chapter .sr-chapter-badge
<<<OLD screening.css 43 280 281
.sr .sr-chapter .sr-chapter-badge { margin-left: auto; font: 700 8px/1 var(--sr-mono);
>>>

<<<NEW screening.css 43 287 288
.sr .sr-chapter .sr-chapter-badge { margin-left: auto; font: 700 var(--sr-t-xs)/1 var(--sr-mono);
>>>

### screening.css #44 [replace] base lines 283-283 -> head lines 290-290
touches: .sr .sr-chapter .sr-chapter-badge
<<<OLD screening.css 44 282 283
  background: var(--sr-stock); padding: 3px 6px; }
>>>

<<<NEW screening.css 44 289 290
  background: var(--sr-stock); padding: 3px var(--sr-s2); }
>>>

### screening.css #45 [replace] base lines 287-287 -> head lines 294-294
touches: .sr .sr-agentcut
<<<OLD screening.css 45 286 287
  padding: 14px 16px; font-size: 12px; line-height: 1.6; color: var(--sr-ink-mute); }
>>>

<<<NEW screening.css 45 293 294
  padding: var(--sr-s4) var(--sr-s4); font-size: 12px; line-height: 1.6; color: var(--sr-ink-mute); }
>>>

### screening.css #46 [replace] base lines 291-291 -> head lines 298-298
touches: .sr .sr-aerialpass
<<<OLD screening.css 46 290 291
.sr .sr-aerialpass { margin-top: 30px; border: 1px solid rgba(127, 157, 184, 0.4);
>>>

<<<NEW screening.css 46 297 298
.sr .sr-aerialpass { margin-top: var(--sr-s6); border: 1px solid rgba(127, 157, 184, 0.4);
>>>

### screening.css #47 [replace] base lines 298-298 -> head lines 305-305
touches: .sr .sr-aerialpass-copy
<<<OLD screening.css 47 297 298
.sr .sr-aerialpass-copy { padding: 24px 26px; display: flex; flex-direction: column;
>>>

<<<NEW screening.css 47 304 305
.sr .sr-aerialpass-copy { padding: 24px var(--sr-s5); display: flex; flex-direction: column;
>>>

### screening.css #48 [replace] base lines 300-300 -> head lines 307-307
touches: .sr .sr-aerialpass-copy .sr-mono
<<<OLD screening.css 48 299 300
.sr .sr-aerialpass-copy .sr-mono { margin: 0 0 12px; letter-spacing: 0.28em;
>>>

<<<NEW screening.css 48 306 307
.sr .sr-aerialpass-copy .sr-mono { margin: 0 0 var(--sr-s3); letter-spacing: var(--sr-track-label);
>>>

### screening.css #49 [replace] base lines 302-302 -> head lines 309-309
touches: .sr .sr-aerialpass-copy h3
<<<OLD screening.css 49 301 302
.sr .sr-aerialpass-copy h3 { margin: 0 0 12px; font: 700 24px/1.15 var(--sr-sans);
>>>

<<<NEW screening.css 49 308 309
.sr .sr-aerialpass-copy h3 { margin: 0 0 var(--sr-s3); font: 700 24px/1.15 var(--sr-sans);
>>>

### screening.css #50 [replace] base lines 305-305 -> head lines 312-312
touches: .sr .sr-aerialpass-copy p
<<<OLD screening.css 50 304 305
.sr .sr-aerialpass-copy p { margin: 0 0 16px; font-size: 13px; line-height: 1.7;
>>>

<<<NEW screening.css 50 311 312
.sr .sr-aerialpass-copy p { margin: 0 0 var(--sr-s4); font-size: var(--sr-t-base); line-height: 1.6;
>>>

### screening.css #51 [replace] base lines 307-309 -> head lines 314-316
touches: .sr .sr-aerialpass-checks
<<<OLD screening.css 51 306 309
.sr .sr-aerialpass-checks { display: flex; flex-direction: column; gap: 8px;
  font: 500 10px/1.4 var(--sr-mono); letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--sr-stock-re); margin: 0 0 18px; padding: 0; list-style: none; }
>>>

<<<NEW screening.css 51 313 316
.sr .sr-aerialpass-checks { display: flex; flex-direction: column; gap: var(--sr-s2);
  font: 500 var(--sr-t-xs)/1.4 var(--sr-mono); letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--sr-stock-re); margin: 0 0 var(--sr-s4); padding: 0; list-style: none; }
>>>

### screening.css #52 [replace] base lines 315-315 -> head lines 322-322
touches: .sr .sr-tier
<<<OLD screening.css 52 314 315
.sr .sr-tier { padding: 20px 22px; border-right: 1px solid var(--sr-line); }
>>>

<<<NEW screening.css 52 321 322
.sr .sr-tier { padding: var(--sr-s5) var(--sr-s5); border-right: 1px solid var(--sr-line); }
>>>

### screening.css #53 [replace] base lines 318-319 -> head lines 325-326
touches: .sr .sr-tier b span | .sr .sr-tier small
<<<OLD screening.css 53 317 319
.sr .sr-tier b span { font-size: 11px; color: var(--sr-ink-faint); }
.sr .sr-tier small { font-size: 9.5px; letter-spacing: 0.14em; text-transform: uppercase;
>>>

<<<NEW screening.css 53 324 326
.sr .sr-tier b span { font-size: var(--sr-t-xs); color: var(--sr-ink-faint); }
.sr .sr-tier small { font-size: var(--sr-t-xs); letter-spacing: 0.14em; text-transform: uppercase;
>>>

### screening.css #54 [replace] base lines 325-325 -> head lines 332-332
touches: .sr .sr-quote
<<<OLD screening.css 54 324 325
.sr .sr-quote { margin: 22px 0 28px; font-family: var(--sr-serif); font-size: 19px;
>>>

<<<NEW screening.css 54 331 332
.sr .sr-quote { margin: var(--sr-s6) 0; font-family: var(--sr-serif); font-size: var(--sr-t-xl);
>>>

### screening.css #55 [replace] base lines 328-328 -> head lines 335-335
touches: .sr .sr-quote .sr-quote-by
<<<OLD screening.css 55 327 328
.sr .sr-quote .sr-quote-by { font: 400 11px/1 var(--sr-sans); letter-spacing: 0.1em;
>>>

<<<NEW screening.css 55 334 335
.sr .sr-quote .sr-quote-by { font: 400 var(--sr-t-sm)/1 var(--sr-sans); letter-spacing: 0.1em;
>>>

### screening.css #56 [replace] base lines 332-333 -> head lines 339-340
touches: .sr .testimonials h2
<<<OLD screening.css 56 331 333
.sr .testimonials h2 { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.3em;
  text-transform: uppercase; color: var(--sr-ink-ghost); margin: 0 0 20px; }
>>>

<<<NEW screening.css 56 338 340
.sr .testimonials h2 { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
  text-transform: uppercase; color: var(--sr-ink-ghost); margin: 0 0 var(--sr-s5); }
>>>

### screening.css #57 [replace] base lines 336-336 -> head lines 343-343
touches: .sr .testimonial
<<<OLD screening.css 57 335 336
.sr .testimonial { margin: 0; padding: 24px 26px; border-right: 1px solid var(--sr-line);
>>>

<<<NEW screening.css 57 342 343
.sr .testimonial { margin: 0; padding: 24px var(--sr-s5); border-right: 1px solid var(--sr-line);
>>>

### screening.css #58 [replace] base lines 343-343 -> head lines 350-350
touches: .sr .testimonial blockquote
<<<OLD screening.css 58 342 343
.sr .testimonial blockquote { margin: 0 0 12px; font-family: var(--sr-serif);
>>>

<<<NEW screening.css 58 349 350
.sr .testimonial blockquote { margin: 0 0 var(--sr-s3); font-family: var(--sr-serif);
>>>

### screening.css #59 [replace] base lines 345-345 -> head lines 352-352
touches: .sr .testimonial figcaption
<<<OLD screening.css 59 344 345
.sr .testimonial figcaption { font-size: 11px; letter-spacing: 0.1em;
>>>

<<<NEW screening.css 59 351 352
.sr .testimonial figcaption { font-size: var(--sr-t-sm); letter-spacing: 0.1em;
>>>

### screening.css #60 [replace] base lines 350-351 -> head lines 357-358
touches: .sr .portfolio-filter | .sr .pf-chip
<<<OLD screening.css 60 349 351
.sr .portfolio-filter { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0; }
.sr .pf-chip { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening.css 60 356 358
.sr .portfolio-filter { display: flex; flex-wrap: wrap; gap: var(--sr-s2); margin: var(--sr-s5) 0; }
.sr .pf-chip { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening.css #61 [replace] base lines 353-353 -> head lines 360-360
touches: .sr .pf-chip
<<<OLD screening.css 61 352 353
  background: none; color: var(--sr-ink-soft); padding: 8px 14px; cursor: pointer; }
>>>

<<<NEW screening.css 61 359 360
  background: none; color: var(--sr-ink-soft); padding: var(--sr-s2) var(--sr-s4); cursor: pointer; }
>>>

### screening.css #62 [replace] base lines 370-370 -> head lines 377-377
touches: .sr .tile .portfolio-cap
<<<OLD screening.css 62 369 370
  margin: 0; padding: 0; background: none; font: 400 11px/1.5 var(--sr-mono);
>>>

<<<NEW screening.css 62 376 377
  margin: 0; padding: 0; background: none; font: 400 var(--sr-t-sm)/1.5 var(--sr-mono);
>>>

### screening.css #63 [replace] base lines 373-373 -> head lines 380-380
touches: .sr .tile .portfolio-cap small
<<<OLD screening.css 63 372 373
.sr .tile .portfolio-cap small { color: rgba(236, 228, 211, 0.55); font-size: 11px; }
>>>

<<<NEW screening.css 63 379 380
.sr .tile .portfolio-cap small { color: rgba(236, 228, 211, 0.55); font-size: var(--sr-t-sm); }
>>>

### screening.css #64 [replace] base lines 375-375 -> head lines 382-382
touches: .sr .tile .play-badge
<<<OLD screening.css 64 374 375
  width: auto; height: auto; border-radius: 0; font: 700 11px/1 var(--sr-mono);
>>>

<<<NEW screening.css 64 381 382
  width: auto; height: auto; border-radius: 0; font: 700 var(--sr-t-xs)/1 var(--sr-mono);
>>>

### screening.css #65 [replace] base lines 377-377 -> head lines 384-384
touches: .sr .tile .play-badge
<<<OLD screening.css 65 376 377
  padding: 4px 8px; }
>>>

<<<NEW screening.css 65 383 384
  padding: var(--sr-s1) var(--sr-s2); }
>>>

### screening.css #66 [replace] base lines 379-380 -> head lines 386-387
touches: .sr .tile .dur-badge
<<<OLD screening.css 66 378 380
  font: 500 11px/1 var(--sr-mono); background: rgba(8, 9, 11, 0.6); color: var(--sr-cream);
  padding: 4px 7px; }
>>>

<<<NEW screening.css 66 385 387
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); background: rgba(8, 9, 11, 0.6); color: var(--sr-cream);
  padding: var(--sr-s1) 7px; }
>>>

### screening.css #67 [replace] base lines 383-383 -> head lines 390-390
touches: .sr .portfolio-masonry.sr-films-first
<<<OLD screening.css 67 382 383
.sr .portfolio-masonry.sr-films-first { display: flex; flex-wrap: wrap; gap: 10px; }
>>>

<<<NEW screening.css 67 389 390
.sr .portfolio-masonry.sr-films-first { display: flex; flex-wrap: wrap; gap: var(--sr-s3); }
>>>

### screening.css #68 [replace] base lines 396-396 -> head lines 403-403
touches: .sr .motion-feature-meta
<<<OLD screening.css 68 395 396
  align-items: center; gap: 12px; padding: 12px 14px; border-top: 1px solid var(--sr-line);
>>>

<<<NEW screening.css 68 402 403
  align-items: center; gap: var(--sr-s3); padding: var(--sr-s3) var(--sr-s4); border-top: 1px solid var(--sr-line);
>>>

### screening.css #69 [replace] base lines 398-398 -> head lines 405-405
touches: .sr .motion-feature-label
<<<OLD screening.css 69 397 398
.sr .motion-feature-label { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.26em;
>>>

<<<NEW screening.css 69 404 405
.sr .motion-feature-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #70 [replace] base lines 400-400 -> head lines 407-407
touches: .sr .motion-feature-meta p
<<<OLD screening.css 70 399 400
.sr .motion-feature-meta p { margin: 4px 0 0; color: var(--sr-ink-soft); font-size: 12.5px; }
>>>

<<<NEW screening.css 70 406 407
.sr .motion-feature-meta p { margin: var(--sr-s1) 0 0; color: var(--sr-ink-soft); font-size: 12.5px; }
>>>

### screening.css #71 [replace] base lines 402-403 -> head lines 409-410
touches: .sr .motion-sound
<<<OLD screening.css 71 401 403
  color: var(--sr-ink-soft); font: 500 10px/1 var(--sr-mono); letter-spacing: 0.1em;
  text-transform: uppercase; padding: 10px 14px; cursor: pointer; border-radius: 0; }
>>>

<<<NEW screening.css 71 408 410
  color: var(--sr-ink-soft); font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.1em;
  text-transform: uppercase; padding: var(--sr-s3) var(--sr-s4); cursor: pointer; border-radius: 0; }
>>>

### screening.css #72 [replace] base lines 406-406 -> head lines 413-413
touches: .sr .motion-strip
<<<OLD screening.css 72 405 406
  gap: 12px; margin-top: 16px; }
>>>

<<<NEW screening.css 72 412 413
  gap: var(--sr-s3); margin-top: var(--sr-s4); }
>>>

### screening.css #73 [replace] base lines 411-411 -> head lines 418-418
touches: .sr .motion-reel-meta
<<<OLD screening.css 73 410 411
  padding: 10px 12px; font: 400 11px/1 var(--sr-mono); letter-spacing: 0.12em;
>>>

<<<NEW screening.css 73 417 418
  padding: var(--sr-s3) var(--sr-s3); font: 400 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.12em;
>>>

### screening.css #74 [replace] base lines 435-435 -> head lines 442-442
touches: .sr .form-label, .sr .field-label
<<<OLD screening.css 74 434 435
.sr .form-label, .sr .field-label { font: 500 10px/1 var(--sr-mono);
>>>

<<<NEW screening.css 74 441 442
.sr .form-label, .sr .field-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono);
>>>

### screening.css #75 [replace] base lines 439-439 -> head lines 446-446
touches: .sr .faq h2
<<<OLD screening.css 75 438 439
.sr .faq h2 { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.3em;
>>>

<<<NEW screening.css 75 445 446
.sr .faq h2 { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #76 [replace] base lines 442-444 -> head lines 449-451
touches: .sr .faq-item | .sr .faq-item summary | .sr .faq-item p
<<<OLD screening.css 76 441 444
  margin: 0 0 8px; }
.sr .faq-item summary { color: var(--sr-cream); font-weight: 600; padding: 14px 16px; }
.sr .faq-item p { color: var(--sr-ink-mute); padding: 0 16px 14px; margin: 0; }
>>>

<<<NEW screening.css 76 448 451
  margin: 0 0 var(--sr-s2); }
.sr .faq-item summary { color: var(--sr-cream); font-weight: 600; padding: var(--sr-s4) var(--sr-s4); }
.sr .faq-item p { color: var(--sr-ink-mute); font-size: var(--sr-t-base); line-height: 1.6; padding: 0 var(--sr-s4) var(--sr-s4); margin: 0; }
>>>

### screening.css #77 [replace] base lines 449-449 -> head lines 456-456
touches: .sr .empty-state, .sr .sr-empty
<<<OLD screening.css 77 448 449
.sr .empty-state, .sr .sr-empty { border: 1px dashed var(--sr-line-raised); padding: 44px 30px;
>>>

<<<NEW screening.css 77 455 456
.sr .empty-state, .sr .sr-empty { border: 1px dashed var(--sr-line-raised); padding: 44px var(--sr-s6);
>>>

### screening.css #78 [replace] base lines 453-453 -> head lines 460-460
touches: .sr .empty-state strong, .sr .sr-empty strong
<<<OLD screening.css 78 452 453
  margin-bottom: 6px; }
>>>

<<<NEW screening.css 78 459 460
  margin-bottom: var(--sr-s2); }
>>>

### screening.css #79 [replace] base lines 455-456 -> head lines 462-463
touches: .sr .empty-state-actions
<<<OLD screening.css 79 454 456
.sr .empty-state-actions { display: flex; justify-content: center; gap: 12px;
  margin-top: 20px; flex-wrap: wrap; }
>>>

<<<NEW screening.css 79 461 463
.sr .empty-state-actions { display: flex; justify-content: center; gap: var(--sr-s3);
  margin-top: var(--sr-s5); flex-wrap: wrap; }
>>>

### screening.css #80 [replace] base lines 459-459 -> head lines 466-466
touches: .sr .btn-pill
<<<OLD screening.css 80 458 459
  letter-spacing: 0.1em; text-transform: uppercase; font-size: 12px; padding: 14px 26px;
>>>

<<<NEW screening.css 80 465 466
  letter-spacing: 0.1em; text-transform: uppercase; font-size: 12px; padding: var(--sr-s4) var(--sr-s5);
>>>

### screening.css #81 [replace] base lines 464-464 -> head lines 471-471
touches: .sr .btn-ghost
<<<OLD screening.css 81 463 464
  text-transform: uppercase; font-size: 12px; padding: 14px 26px; }
>>>

<<<NEW screening.css 81 470 471
  text-transform: uppercase; font-size: 12px; padding: var(--sr-s4) var(--sr-s5); }
>>>

### screening.css #82 [replace] base lines 469-469 -> head lines 476-476
touches: .sr .sr-specline
<<<OLD screening.css 82 468 469
.sr .sr-specline { margin: 0; font: 400 10px/1.7 var(--sr-mono); letter-spacing: 0.06em;
>>>

<<<NEW screening.css 82 475 476
.sr .sr-specline { margin: 0; font: 400 var(--sr-t-xs)/1.7 var(--sr-mono); letter-spacing: 0.06em;
>>>

### screening.css #83 [replace] base lines 473-474 -> head lines 480-481
touches: .sr .sr-featurehead | .sr .sr-featurehead h1
<<<OLD screening.css 83 472 474
  align-items: baseline; gap: 20px; margin-bottom: 14px; flex-wrap: wrap; }
.sr .sr-featurehead h1 { margin: 0; font: 700 clamp(30px, 3vw, 42px)/1 var(--sr-sans);
>>>

<<<NEW screening.css 83 479 481
  align-items: baseline; gap: var(--sr-s5); margin-bottom: var(--sr-s4); flex-wrap: wrap; }
.sr .sr-featurehead h1 { margin: 0; font: 700 var(--sr-d-2)/1 var(--sr-sans);
>>>

### screening.css #84 [replace] base lines 481-481 -> head lines 488-488
touches: .sr .sr-herosplit-copy
<<<OLD screening.css 84 480 481
.sr .sr-herosplit-copy { padding: 62px 48px 50px; display: flex;
>>>

<<<NEW screening.css 84 487 488
.sr .sr-herosplit-copy { padding: var(--sr-s8) var(--sr-s7) var(--sr-s7); display: flex;
>>>

### screening.css #85 [replace] base lines 483-484 -> head lines 490-491
touches: .sr .sr-herosplit-copy .sr-mono | .sr .sr-herosplit-copy h1
<<<OLD screening.css 85 482 484
.sr .sr-herosplit-copy .sr-mono { display: block; margin: 0 0 16px; letter-spacing: 0.34em; }
.sr .sr-herosplit-copy h1 { margin: 0 0 14px; font: 700 clamp(32px, 3.8vw, 54px)/1.02 var(--sr-sans);
>>>

<<<NEW screening.css 85 489 491
.sr .sr-herosplit-copy .sr-mono { display: block; margin: 0 0 var(--sr-s4); letter-spacing: 0.34em; }
.sr .sr-herosplit-copy h1 { margin: 0 0 var(--sr-s4); font: 700 var(--sr-d-2)/1.02 var(--sr-sans);
>>>

### screening.css #86 [replace] base lines 487-489 -> head lines 494-496
touches: .sr .sr-herosplit-copy .sr-beat | .sr .sr-herosplit-copy .sr-lede | .sr .sr-herosplit-actions
<<<OLD screening.css 86 486 489
.sr .sr-herosplit-copy .sr-beat { margin: 0 0 26px; }
.sr .sr-herosplit-copy .sr-lede { margin: 0 0 30px; }
.sr .sr-herosplit-actions { display: flex; gap: 12px; margin-bottom: 34px; flex-wrap: wrap; }
>>>

<<<NEW screening.css 86 493 496
.sr .sr-herosplit-copy .sr-beat { margin: 0 0 var(--sr-s5); }
.sr .sr-herosplit-copy .sr-lede { margin: 0 0 var(--sr-s6); }
.sr .sr-herosplit-actions { display: flex; gap: var(--sr-s3); margin-bottom: 34px; flex-wrap: wrap; }
>>>

### screening.css #87 [replace] base lines 505-505 -> head lines 512-512
touches: .sr .sr-titlecard-copy .sr-mono
<<<OLD screening.css 87 504 505
.sr .sr-titlecard-copy .sr-mono { display: block; margin: 0 0 14px;
>>>

<<<NEW screening.css 87 511 512
.sr .sr-titlecard-copy .sr-mono { display: block; margin: 0 0 var(--sr-s4);
>>>

### screening.css #88 [replace] base lines 507-507 -> head lines 514-514
touches: .sr .sr-titlecard-copy h1
<<<OLD screening.css 88 506 507
.sr .sr-titlecard-copy h1 { margin: 0 0 12px; font: 700 clamp(34px, 4.4vw, 64px)/1 var(--sr-sans);
>>>

<<<NEW screening.css 88 513 514
.sr .sr-titlecard-copy h1 { margin: 0 0 var(--sr-s3); font: 700 var(--sr-d-1)/1 var(--sr-sans);
>>>

### screening.css #89 [replace] base lines 509-510 -> head lines 516-517
touches: .sr .sr-titlecard-copy .sr-beat | .sr .sr-titlecard-actions
<<<OLD screening.css 89 508 510
.sr .sr-titlecard-copy .sr-beat { margin: 0 0 26px; color: var(--sr-ink-soft); }
.sr .sr-titlecard-actions { display: inline-flex; gap: 12px; flex-wrap: wrap;
>>>

<<<NEW screening.css 89 515 517
.sr .sr-titlecard-copy .sr-beat { margin: 0 0 var(--sr-s5); color: var(--sr-ink-soft); }
.sr .sr-titlecard-actions { display: inline-flex; gap: var(--sr-s3); flex-wrap: wrap;
>>>

### screening.css #90 [replace] base lines 513-514 -> head lines 520-521
touches: .sr .sr-titlecard-meta
<<<OLD screening.css 90 512 514
  display: flex; justify-content: space-between; gap: 14px; z-index: 2;
  font: 500 11px/1.4 var(--sr-mono); letter-spacing: 0.18em; text-transform: uppercase;
>>>

<<<NEW screening.css 90 519 521
  display: flex; justify-content: space-between; gap: var(--sr-s4); z-index: 2;
  font: 500 var(--sr-t-xs)/1.4 var(--sr-mono); letter-spacing: 0.18em; text-transform: uppercase;
>>>

### screening.css #91 [replace] base lines 519-519 -> head lines 526-526
touches: .sr .sr-appetite
<<<OLD screening.css 91 518 519
  grid-auto-rows: 170px; gap: 8px; grid-auto-flow: dense; }
>>>

<<<NEW screening.css 91 525 526
  grid-auto-rows: 170px; gap: var(--sr-s2); grid-auto-flow: dense; }
>>>

### screening.css #92 [replace] base lines 530-530 -> head lines 537-537
touches: .sr .sr-sessions
<<<OLD screening.css 92 529 530
.sr .sr-sessions { display: grid; grid-template-columns: 1fr 1fr 1.4fr 1fr; gap: 10px; }
>>>

<<<NEW screening.css 92 536 537
.sr .sr-sessions { display: grid; grid-template-columns: 1fr 1fr 1.4fr 1fr; gap: var(--sr-s3); }
>>>

### screening.css #93 [replace] base lines 544-544 -> head lines 551-551
touches: .sr .esec-kicker, .sr .work-eyebrow, .sr .work-feature-eyebrow, .sr .cd-label
<<<OLD screening.css 93 543 544
.sr .cd-label { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.26em;
>>>

<<<NEW screening.css 93 550 551
.sr .cd-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #94 [replace] base lines 556-556 -> head lines 563-563
touches: .sr .svc-head h2
<<<OLD screening.css 94 555 556
.sr .svc-head h2 { font: 700 22px/1.2 var(--sr-sans); letter-spacing: 0.04em;
>>>

<<<NEW screening.css 94 562 563
.sr .svc-head h2 { font: 700 var(--sr-d-3)/1.1 var(--sr-sans); letter-spacing: 0.04em;
>>>

### screening.css #95 [replace] base lines 558-558 -> head lines 565-565
touches: .sr .svc-tag
<<<OLD screening.css 95 557 558
.sr .svc-tag { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

<<<NEW screening.css 95 564 565
.sr .svc-tag { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

### screening.css #96 [replace] base lines 568-568 -> head lines 575-575
touches: .sr .svc-tier h3
<<<OLD screening.css 96 567 568
  letter-spacing: 0.05em; font-size: 15px; }
>>>

<<<NEW screening.css 96 574 575
  letter-spacing: 0.05em; font-size: var(--sr-d-4); }
>>>

### screening.css #97 [replace] base lines 571-572 -> head lines 578-579
touches: .sr .svc-pick
<<<OLD screening.css 97 570 572
  font: 700 11px/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: 5px 8px; }
>>>

<<<NEW screening.css 97 577 579
  font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: 5px var(--sr-s2); }
>>>

### screening.css #98 [replace] base lines 575-575 -> head lines 582-582
touches: .sr .svc-tier ul li
<<<OLD screening.css 98 574 575
.sr .svc-tier ul li { color: var(--sr-ink-mute); }
>>>

<<<NEW screening.css 98 581 582
.sr .svc-tier ul li { color: var(--sr-ink-mute); font-size: var(--sr-t-base); line-height: 1.6; }
>>>

### screening.css #99 [replace] base lines 577-577 -> head lines 584-584
touches: .sr .svc-tier .svc-usage
<<<OLD screening.css 99 576 577
.sr .svc-tier .svc-usage { margin: 0; font-size: 12px; line-height: 1.5; color: var(--sr-ink-faint); }
>>>

<<<NEW screening.css 99 583 584
.sr .svc-tier .svc-usage { margin: 0; font-size: var(--sr-t-sm); line-height: 1.5; color: var(--sr-ink-faint); }
>>>

### screening.css #100 [replace] base lines 579-579 -> head lines 586-586
touches: .sr .svc-cta, .sr .svc-tier-mid .svc-cta
<<<OLD screening.css 100 578 579
  justify-content: center; gap: 8px; background: none; border: 1px solid
>>>

<<<NEW screening.css 100 585 586
  justify-content: center; gap: var(--sr-s2); background: none; border: 1px solid
>>>

### screening.css #101 [replace] base lines 581-582 -> head lines 588-589
touches: .sr .svc-cta, .sr .svc-tier-mid .svc-cta
<<<OLD screening.css 101 580 582
  color: var(--sr-amber); font: 700 11px/1 var(--sr-sans); letter-spacing: 0.08em;
  text-transform: uppercase; padding: 12px 18px; box-shadow: none; }
>>>

<<<NEW screening.css 101 587 589
  color: var(--sr-amber); font: 700 var(--sr-t-xs)/1 var(--sr-sans); letter-spacing: 0.08em;
  text-transform: uppercase; padding: var(--sr-s3) var(--sr-s4); box-shadow: none; }
>>>

### screening.css #102 [replace] base lines 595-595 -> head lines 602-602
touches: .sr .sp-packages .sp-included
<<<OLD screening.css 102 594 595
.sr .sp-packages .sp-included { display: grid; gap: 18px 36px; margin: 0 0 18px;
>>>

<<<NEW screening.css 102 601 602
.sr .sp-packages .sp-included { display: grid; gap: var(--sr-s4) 36px; margin: 0 0 var(--sr-s4);
>>>

### screening.css #103 [replace] base lines 597-599 -> head lines 604-606
touches: .sr .sp-packages .sp-included | .sr .sp-packages .sp-included p | .sr .sp-packages .sp-included ul
<<<OLD screening.css 103 596 599
  padding: 16px 20px; border: 1px solid var(--sr-line); }
.sr .sp-packages .sp-included p { margin: 0 0 8px; }
.sr .sp-packages .sp-included ul { margin: 0; padding-left: 18px; font-size: 12.5px;
>>>

<<<NEW screening.css 103 603 606
  padding: var(--sr-s4) var(--sr-s5); border: 1px solid var(--sr-line); }
.sr .sp-packages .sp-included p { margin: 0 0 var(--sr-s2); }
.sr .sp-packages .sp-included ul { margin: 0; padding-left: var(--sr-s4); font-size: 12.5px;
>>>

### screening.css #104 [replace] base lines 610-610 -> head lines 617-617
touches: .sr .press-date
<<<OLD screening.css 104 609 610
.sr .press-date { font-family: var(--sr-mono); font-size: 10px; }
>>>

<<<NEW screening.css 104 616 617
.sr .press-date { font-family: var(--sr-mono); font-size: var(--sr-t-xs); }
>>>

### screening.css #105 [replace] base lines 616-616 -> head lines 623-623
touches: .sr .work-feature-badge
<<<OLD screening.css 105 615 616
  border-radius: 0; font: 700 11px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening.css 105 622 623
  border-radius: 0; font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening.css #106 [replace] base lines 625-625 -> head lines 632-632
touches: .sr .placeholder
<<<OLD screening.css 106 624 625
  font-family: var(--sr-mono); font-size: 10px; letter-spacing: 0.14em;
>>>

<<<NEW screening.css 106 631 632
  font-family: var(--sr-mono); font-size: var(--sr-t-xs); letter-spacing: 0.14em;
>>>

### screening.css #107 [replace] base lines 634-634 -> head lines 641-641
touches: .sr .sr-contact-h1
<<<OLD screening.css 107 633 634
.sr .sr-contact-h1 { font: 700 clamp(30px, 3.4vw, 46px)/1.05 var(--sr-sans);
>>>

<<<NEW screening.css 107 640 641
.sr .sr-contact-h1 { font: 700 var(--sr-d-2)/1.05 var(--sr-sans);
>>>

### screening.css #108 [replace] base lines 636-636 -> head lines 643-643
touches: .sr .sr-contact-h1
<<<OLD screening.css 108 635 636
  margin: 12px 0; }
>>>

<<<NEW screening.css 108 642 643
  margin: var(--sr-s3) 0; }
>>>

### screening.css #109 [insert] base lines (none, after 644) -> head lines 652-652
touches: .site-body.sr .stack-form input, .site-body.sr .stack-form select, .site-body.sr .stack-fo
<<<OLD screening.css 109 644 644
>>>

<<<NEW screening.css 109 651 652
.site-body.sr .stack-form input, .site-body.sr .stack-form select, .site-body.sr .stack-form textarea { font-size: var(--sr-t-base); }
>>>

### screening.css #110 [replace] base lines 648-648 -> head lines 656-656
touches: .sr .stack-form button[type="submit"], .sr button[type="submit"]
<<<OLD screening.css 110 647 648
  padding: 15px 26px; cursor: pointer; }
>>>

<<<NEW screening.css 110 655 656
  padding: 15px var(--sr-s5); cursor: pointer; }
>>>

### screening.css #111 [replace] base lines 661-661 -> head lines 669-669
touches: .sr .book-event-meta
<<<OLD screening.css 111 660 661
.sr .book-event-meta { font: 400 9.5px/1.6 var(--sr-mono); letter-spacing: 0.12em;
>>>

<<<NEW screening.css 111 668 669
.sr .book-event-meta { font: 400 var(--sr-t-xs)/1.6 var(--sr-mono); letter-spacing: 0.12em;
>>>

### screening.css #112 [replace] base lines 671-671 -> head lines 679-679
touches: .sr .book-rail-host
<<<OLD screening.css 112 670 671
.sr .book-rail-host { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.26em;
>>>

<<<NEW screening.css 112 678 679
.sr .book-rail-host { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #113 [replace] base lines 681-681 -> head lines 689-689
touches: .sr .book-main .book-busy
<<<OLD screening.css 113 680 681
.sr .book-main .book-busy { display: block; width: 100%; margin: 0 0 10px;
>>>

<<<NEW screening.css 113 688 689
.sr .book-main .book-busy { display: block; width: 100%; margin: 0 0 var(--sr-s3);
>>>

### screening.css #114 [replace] base lines 686-686 -> head lines 694-694
touches: .sr .book-grid th
<<<OLD screening.css 114 685 686
.sr .book-grid th { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening.css 114 693 694
.sr .book-grid th { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening.css #115 [replace] base lines 703-703 -> head lines 711-711
touches: .sr .book-aerial
<<<OLD screening.css 115 702 703
.sr .book-aerial { border: 1px solid rgba(127, 157, 184, 0.4); padding: 12px 14px;
>>>

<<<NEW screening.css 115 710 711
.sr .book-aerial { border: 1px solid rgba(127, 157, 184, 0.4); padding: var(--sr-s3) var(--sr-s4);
>>>

### screening.css #116 [replace] base lines 705-706 -> head lines 713-714
touches: .sr .book-manage-badge
<<<OLD screening.css 116 704 706
.sr .book-manage-badge { border-radius: 0; font: 700 10px/1 var(--sr-mono);
  letter-spacing: 0.2em; text-transform: uppercase; padding: 8px 12px;
>>>

<<<NEW screening.css 116 712 714
.sr .book-manage-badge { border-radius: 0; font: 700 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; padding: var(--sr-s2) var(--sr-s3);
>>>

### screening.css #117 [replace] base lines 713-713 -> head lines 721-721
touches: .sr .book-manage-detail dt
<<<OLD screening.css 117 712 713
.sr .book-manage-detail dt { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.2em;
>>>

<<<NEW screening.css 117 720 721
.sr .book-manage-detail dt { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #118 [replace] base lines 724-724 -> head lines 732-732
touches: .sr .sr-gate
<<<OLD screening.css 118 723 724
  justify-content: center; min-height: 92vh; padding: 28px 16px; gap: 18px;
>>>

<<<NEW screening.css 118 731 732
  justify-content: center; min-height: 92vh; padding: 28px var(--sr-s4); gap: var(--sr-s4);
>>>

### screening.css #119 [replace] base lines 728-728 -> head lines 736-736
touches: .sr .sr-ticket
<<<OLD screening.css 119 727 728
  padding: 26px 24px 22px; position: relative; border-radius: 6px;
>>>

<<<NEW screening.css 119 735 736
  padding: var(--sr-s5) 24px var(--sr-s5); position: relative; border-radius: 6px;
>>>

### screening.css #120 [replace] base lines 734-736 -> head lines 742-744
touches: .sr .sr-ticket-kicker | .sr .sr-ticket-title
<<<OLD screening.css 120 733 736
.sr .sr-ticket-kicker { margin: 0 0 6px; font: 500 11px/1 var(--sr-mono);
  letter-spacing: 0.3em; text-transform: uppercase; color: var(--sr-sheet-gold); }
.sr .sr-ticket-title { margin: 0 0 4px; font: 700 24px/1.05 var(--sr-sans);
>>>

<<<NEW screening.css 120 741 744
.sr .sr-ticket-kicker { margin: 0 0 var(--sr-s2); font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-sheet-gold); }
.sr .sr-ticket-title { margin: 0 0 var(--sr-s1); font: 700 24px/1.05 var(--sr-sans);
>>>

### screening.css #121 [replace] base lines 738-738 -> head lines 746-746
touches: .sr .sr-ticket-for
<<<OLD screening.css 121 737 738
.sr .sr-ticket-for { margin: 0 0 18px; font-family: var(--sr-serif); font-style: italic;
>>>

<<<NEW screening.css 121 745 746
.sr .sr-ticket-for { margin: 0 0 var(--sr-s4); font-family: var(--sr-serif); font-style: italic;
>>>

### screening.css #122 [replace] base lines 740-742 -> head lines 748-750
touches: .sr .sr-ticket-form | .sr .sr-ticket-label
<<<OLD screening.css 122 739 742
.sr .sr-ticket-form { border-top: 2px dashed #b8ad94; padding-top: 18px; margin: 0;
  display: flex; flex-direction: column; gap: 10px; }
.sr .sr-ticket-label { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.2em;
>>>

<<<NEW screening.css 122 747 750
.sr .sr-ticket-form { border-top: 2px dashed #b8ad94; padding-top: var(--sr-s4); margin: 0;
  display: flex; flex-direction: column; gap: var(--sr-s3); }
.sr .sr-ticket-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #123 [replace] base lines 751-751 -> head lines 759-759
touches: .sr .sr-ticket-go
<<<OLD screening.css 123 750 751
  text-transform: uppercase; padding: 16px 0; cursor: pointer; }
>>>

<<<NEW screening.css 123 758 759
  text-transform: uppercase; padding: var(--sr-s4) 0; cursor: pointer; }
>>>

### screening.css #124 [replace] base lines 753-753 -> head lines 761-761
touches: .sr .sr-ticket .error
<<<OLD screening.css 124 752 753
.sr .sr-ticket .error { margin: 4px 0 0; color: #8a3d24; font-size: 12.5px; }
>>>

<<<NEW screening.css 124 760 761
.sr .sr-ticket .error { margin: var(--sr-s1) 0 0; color: #8a3d24; font-size: 12.5px; }
>>>

### screening.css #125 [replace] base lines 767-769 -> head lines 775-777
touches: .sr .sr-premiere-inner | .sr .sr-premiere-inner .sr-mono | .sr .sr-premiere-inner h1
<<<OLD screening.css 125 766 769
  padding: 70px 20px; }
.sr .sr-premiere-inner .sr-mono { display: block; margin: 0 0 18px; letter-spacing: 0.4em; }
.sr .sr-premiere-inner h1 { margin: 0 0 12px; font: 700 clamp(30px, 4.6vw, 66px)/1 var(--sr-sans);
>>>

<<<NEW screening.css 125 774 777
  padding: 70px var(--sr-s5); }
.sr .sr-premiere-inner .sr-mono { display: block; margin: 0 0 var(--sr-s4); letter-spacing: 0.4em; }
.sr .sr-premiere-inner h1 { margin: 0 0 var(--sr-s3); font: 700 clamp(30px, 4.6vw, 66px)/1 var(--sr-sans);
>>>

### screening.css #126 [replace] base lines 771-772 -> head lines 779-780
touches: .sr .sr-premiere-inner .sr-beat | .sr .sr-premiere-actions
<<<OLD screening.css 126 770 772
.sr .sr-premiere-inner .sr-beat { margin: 0 0 30px; color: #cfc4ac; }
.sr .sr-premiere-actions { display: flex; gap: 12px; justify-content: center;
>>>

<<<NEW screening.css 126 778 780
.sr .sr-premiere-inner .sr-beat { margin: 0 0 var(--sr-s6); color: #cfc4ac; }
.sr .sr-premiere-actions { display: flex; gap: var(--sr-s3); justify-content: center;
>>>

### screening.css #127 [replace] base lines 778-780 -> head lines 786-788
touches: .sr .sr-premiere-card--again .sr-premiere-inner | .sr .sr-premiere-card--again .sr-premiere-inner .sr-mono
<<<OLD screening.css 127 777 780
.sr .sr-premiere-card--again .sr-premiere-inner { padding: 34px 20px 30px; }
.sr .sr-premiere-card--again .sr-premiere-inner .sr-mono { margin-bottom: 12px;
  letter-spacing: 0.3em; }
>>>

<<<NEW screening.css 127 785 788
.sr .sr-premiere-card--again .sr-premiere-inner { padding: 34px var(--sr-s5) var(--sr-s6); }
.sr .sr-premiere-card--again .sr-premiere-inner .sr-mono { margin-bottom: var(--sr-s3);
  letter-spacing: var(--sr-track-label); }
>>>

### screening.css #128 [replace] base lines 782-783 -> head lines 790-791
touches: .sr .sr-premiere-card--again .sr-premiere-inner h1 | .sr .sr-premiere-card--again .sr-beat
<<<OLD screening.css 128 781 783
  font-size: clamp(22px, 3vw, 34px); margin-bottom: 8px; }
.sr .sr-premiere-card--again .sr-beat { margin-bottom: 18px; }
>>>

<<<NEW screening.css 128 789 791
  font-size: clamp(22px, 3vw, 34px); margin-bottom: var(--sr-s2); }
.sr .sr-premiere-card--again .sr-beat { margin-bottom: var(--sr-s4); }
>>>

### screening.css #129 [replace] base lines 786-786 -> head lines 794-794
touches: .sr .sr-reelrow
<<<OLD screening.css 129 785 786
.sr .sr-reelrow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
>>>

<<<NEW screening.css 129 793 794
.sr .sr-reelrow { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sr-s3); }
>>>

### screening.css #130 [replace] base lines 791-791 -> head lines 799-799
touches: .sr .sr-rec-tile
<<<OLD screening.css 130 790 791
  justify-content: center; gap: 10px; text-align: center; padding: 16px; }
>>>

<<<NEW screening.css 130 798 799
  justify-content: center; gap: var(--sr-s3); text-align: center; padding: var(--sr-s4); }
>>>

### screening.css #131 [replace] base lines 794-794 -> head lines 802-802
touches: .sr .sr-rec-tile small
<<<OLD screening.css 131 793 794
.sr .sr-rec-tile small { font: 400 9.5px/1.7 var(--sr-mono); letter-spacing: 0.06em;
>>>

<<<NEW screening.css 131 801 802
.sr .sr-rec-tile small { font: 400 var(--sr-t-xs)/1.7 var(--sr-mono); letter-spacing: 0.06em;
>>>

### screening.css #132 [replace] base lines 806-807 -> head lines 814-815
touches: .sr .gallery-main | .sr .gallery-main .grid
<<<OLD screening.css 132 805 807
.sr .gallery-main { padding-top: 10px; }
.sr .gallery-main .grid { gap: 12px; }
>>>

<<<NEW screening.css 132 813 815
.sr .gallery-main { padding-top: var(--sr-s3); }
.sr .gallery-main .grid { gap: var(--sr-s3); }
>>>

### screening.css #133 [replace] base lines 824-824 -> head lines 832-832
touches: .sr .icon-btn-mp4
<<<OLD screening.css 133 823 824
  font: 500 11px/1 var(--sr-mono); letter-spacing: 0.08em; }
>>>

<<<NEW screening.css 133 831 832
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.08em; }
>>>

### screening.css #134 [replace] base lines 827-827 -> head lines 835-835
touches: .sr .section-nav a
<<<OLD screening.css 134 826 827
.sr .section-nav a { color: var(--sr-ink-soft); font: 600 11px/1 var(--sr-sans);
>>>

<<<NEW screening.css 134 834 835
.sr .section-nav a { color: var(--sr-ink-soft); font: 600 var(--sr-t-xs)/1 var(--sr-sans);
>>>

### screening.css #135 [replace] base lines 835-835 -> head lines 843-843
touches: .sr .proof-progress
<<<OLD screening.css 135 834 835
.sr .proof-progress { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening.css 135 842 843
.sr .proof-progress { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening.css #136 [replace] base lines 845-845 -> head lines 853-853
touches: .sr .sr-export-actions .sr-btn
<<<OLD screening.css 136 844 845
.sr .sr-export-actions .sr-btn { padding: 11px 16px; font-size: 10px;
>>>

<<<NEW screening.css 136 852 853
.sr .sr-export-actions .sr-btn { padding: 11px var(--sr-s4); font-size: var(--sr-t-xs);
>>>

### screening.css #137 [replace] base lines 849-849 -> head lines 857-857
touches: .sr .sr-prospect
<<<OLD screening.css 137 848 849
.sr .sr-prospect { margin-top: 48px; border: 1px solid var(--sr-line); padding: 30px;
>>>

<<<NEW screening.css 137 856 857
.sr .sr-prospect { margin-top: 48px; border: 1px solid var(--sr-line); padding: var(--sr-s6);
>>>

### screening.css #138 [replace] base lines 851-852 -> head lines 859-860
touches: .sr .sr-prospect .sr-mono | .sr .sr-prospect h2
<<<OLD screening.css 138 850 852
.sr .sr-prospect .sr-mono { display: block; margin-bottom: 12px; letter-spacing: 0.3em; }
.sr .sr-prospect h2 { margin: 0 0 10px; font: 700 22px/1.2 var(--sr-sans);
>>>

<<<NEW screening.css 138 858 860
.sr .sr-prospect .sr-mono { display: block; margin-bottom: var(--sr-s3); letter-spacing: var(--sr-track-label); }
.sr .sr-prospect h2 { margin: 0 0 var(--sr-s3); font: 700 22px/1.2 var(--sr-sans);
>>>

### screening.css #139 [replace] base lines 855-855 -> head lines 863-863
touches: .sr .sr-prospect-body
<<<OLD screening.css 139 854 855
.sr .sr-prospect-body { margin: 0 auto 18px; max-width: 60ch; font-size: 13px;
>>>

<<<NEW screening.css 139 862 863
.sr .sr-prospect-body { margin: 0 auto var(--sr-s4); max-width: 60ch; font-size: 13px;
>>>

### screening.css #140 [replace] base lines 857-857 -> head lines 865-865
touches: .sr .sr-credit
<<<OLD screening.css 140 856 857
.sr .sr-credit { text-align: center; margin: 26px 0 90px; letter-spacing: 0.18em; }
>>>

<<<NEW screening.css 140 864 865
.sr .sr-credit { text-align: center; margin: var(--sr-s5) 0 90px; letter-spacing: 0.18em; }
>>>

### screening.css #141 [replace] base lines 865-865 -> head lines 873-873
touches: .sr .sr-statecard
<<<OLD screening.css 141 864 865
  justify-content: center; gap: 12px; }
>>>

<<<NEW screening.css 141 872 873
  justify-content: center; gap: var(--sr-s3); }
>>>

### screening.css #142 [replace] base lines 867-868 -> head lines 875-876
touches: .sr .sr-state-kicker
<<<OLD screening.css 142 866 868
.sr .sr-state-kicker { letter-spacing: 0.3em; text-transform: uppercase;
  font: 500 10px/1 var(--sr-mono); color: var(--sr-amber); }
>>>

<<<NEW screening.css 142 874 876
.sr .sr-state-kicker { letter-spacing: var(--sr-track-label); text-transform: uppercase;
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); color: var(--sr-amber); }
>>>

### screening.css #143 [replace] base lines 875-876 -> head lines 883-884
touches: .sr .sr-statecard .sr-rec-bar | .sr .sr-state-actions
<<<OLD screening.css 143 874 876
.sr .sr-statecard .sr-rec-bar { width: 200px; margin: 8px auto 0; }
.sr .sr-state-actions { display: flex; gap: 10px; justify-content: center;
>>>

<<<NEW screening.css 143 882 884
.sr .sr-statecard .sr-rec-bar { width: 200px; margin: var(--sr-s2) auto 0; }
.sr .sr-state-actions { display: flex; gap: var(--sr-s3); justify-content: center;
>>>

### screening.css #144 [replace] base lines 882-882 -> head lines 890-890
touches: .sr .sr-statecard .pin-form button
<<<OLD screening.css 144 881 882
  text-transform: uppercase; padding: 14px 22px; cursor: pointer; }
>>>

<<<NEW screening.css 144 889 890
  text-transform: uppercase; padding: var(--sr-s4) var(--sr-s5); cursor: pointer; }
>>>

### screening.css #145 [replace] base lines 888-888 -> head lines 896-896
touches: .sr .portal-header .sr-mono
<<<OLD screening.css 145 887 888
.sr .portal-header .sr-mono { display: block; margin-bottom: 12px; letter-spacing: 0.3em; }
>>>

<<<NEW screening.css 145 895 896
.sr .portal-header .sr-mono { display: block; margin-bottom: var(--sr-s3); letter-spacing: var(--sr-track-label); }
>>>

### screening.css #146 [replace] base lines 890-890 -> head lines 898-898
touches: .sr .portal-changelog
<<<OLD screening.css 146 889 890
.sr .portal-changelog { border: 1px solid var(--sr-line); padding: 12px 16px;
>>>

<<<NEW screening.css 146 897 898
.sr .portal-changelog { border: 1px solid var(--sr-line); padding: var(--sr-s3) var(--sr-s4);
>>>

### screening.css #147 [replace] base lines 893-894 -> head lines 901-902
touches: .sr .changelog-new, .sr .new-pill
<<<OLD screening.css 147 892 894
  border-radius: 0; font: 700 11px/1 var(--sr-mono); letter-spacing: 0.14em;
  padding: 3px 6px; }
>>>

<<<NEW screening.css 147 900 902
  border-radius: 0; font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.14em;
  padding: 3px var(--sr-s2); }
>>>

### screening.css #148 [replace] base lines 916-916 -> head lines 924-924
touches: .sr .kind-badge
<<<OLD screening.css 148 915 916
.sr .kind-badge { border-radius: 0; font: 700 11px/1 var(--sr-mono);
>>>

<<<NEW screening.css 148 923 924
.sr .kind-badge { border-radius: 0; font: 700 var(--sr-t-xs)/1 var(--sr-mono);
>>>

### screening.css #149 [replace] base lines 946-946 -> head lines 954-954
touches: @media (max-width: 700px) .sr .sr-export
<<<OLD screening.css 149 945 946
  .sr .sr-export { padding: 12px 16px 26px; gap: 10px; }
>>>

<<<NEW screening.css 149 953 954
  .sr .sr-export { padding: var(--sr-s3) var(--sr-s4) var(--sr-s5); gap: var(--sr-s3); }
>>>

### screening.css #150 [replace] base lines 956-956 -> head lines 964-964
touches: .sr .client-doc
<<<OLD screening.css 150 955 956
.sr .client-doc { max-width: 760px; margin: 0 auto; padding: 44px 20px 60px; }
>>>

<<<NEW screening.css 150 963 964
.sr .client-doc { max-width: 760px; margin: 0 auto; padding: 44px var(--sr-s5) 60px; }
>>>

### screening.css #151 [replace] base lines 958-958 -> head lines 966-966
touches: .sr .client-doc-brandbar
<<<OLD screening.css 151 957 958
  padding: 0 4px 20px; display: flex; justify-content: space-between;
>>>

<<<NEW screening.css 151 965 966
  padding: 0 var(--sr-s1) var(--sr-s5); display: flex; justify-content: space-between;
>>>

### screening.css #152 [replace] base lines 963-963 -> head lines 971-971
touches: .sr .client-doc-brandbar div span
<<<OLD screening.css 152 962 963
  font: 500 11px/1 var(--sr-mono); letter-spacing: 0.2em; text-transform: uppercase;
>>>

<<<NEW screening.css 152 970 971
  font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label); text-transform: uppercase;
>>>

### screening.css #153 [replace] base lines 970-970 -> head lines 978-978
touches: .sr .client-doc-kicker
<<<OLD screening.css 153 969 970
.sr .client-doc-kicker { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.24em;
>>>

<<<NEW screening.css 153 977 978
.sr .client-doc-kicker { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #154 [replace] base lines 976-977 -> head lines 984-985
touches: .sr .client-doc-meta p span | .sr .client-doc-table th
<<<OLD screening.css 154 975 977
  font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; }
.sr .client-doc-table th { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.2em;
>>>

<<<NEW screening.css 154 983 985
  font-size: var(--sr-t-xs); letter-spacing: 0.18em; text-transform: uppercase; }
.sr .client-doc-table th { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #155 [replace] base lines 985-985 -> head lines 993-993
touches: .sr .client-doc-totals span
<<<OLD screening.css 155 984 985
.sr .client-doc-totals span { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.22em;
>>>

<<<NEW screening.css 155 992 993
.sr .client-doc-totals span { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #156 [replace] base lines 991-991 -> head lines 999-999
touches: .sr .client-doc-terms h2
<<<OLD screening.css 156 990 991
.sr .client-doc-terms h2 { font: 500 9.5px/1 var(--sr-mono); letter-spacing: 0.2em;
>>>

<<<NEW screening.css 156 998 999
.sr .client-doc-terms h2 { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: var(--sr-track-label);
>>>

### screening.css #157 [replace] base lines 996-996 -> head lines 1004-1004
touches: .sr .client-pay-btn
<<<OLD screening.css 157 995 996
  letter-spacing: 0.1em; text-transform: uppercase; padding: 14px 26px;
>>>

<<<NEW screening.css 157 1003 1004
  letter-spacing: 0.1em; text-transform: uppercase; padding: var(--sr-s4) var(--sr-s5);
>>>

### screening.css #158 [replace] base lines 1009-1009 -> head lines 1017-1017
touches: .sr .client-doc-foot
<<<OLD screening.css 158 1008 1009
.sr .client-doc-foot { color: var(--sr-ink-ghost); font: 500 9.5px/1.7 var(--sr-mono);
>>>

<<<NEW screening.css 158 1016 1017
.sr .client-doc-foot { color: var(--sr-ink-ghost); font: 500 var(--sr-t-xs)/1.7 var(--sr-mono);
>>>

### screening.css #159 [replace] base lines 1067-1067 -> head lines 1075-1075
touches: @media (max-width: 700px) .sr .site-nav
<<<OLD screening.css 159 1066 1067
  .sr .site-nav { padding: 14px 16px; }
>>>

<<<NEW screening.css 159 1074 1075
  .sr .site-nav { padding: var(--sr-s4) var(--sr-s4); }
>>>

### screening.css #160 [replace] base lines 1070-1070 -> head lines 1078-1078
touches: @media (max-width: 700px) .sr .site-footer
<<<OLD screening.css 160 1069 1070
  .sr .site-footer { padding: 30px 16px 90px; }
>>>

<<<NEW screening.css 160 1077 1078
  .sr .site-footer { padding: var(--sr-s6) var(--sr-s4) 90px; }
>>>

### screening.css #161 [replace] base lines 1073-1073 -> head lines 1081-1081
touches: @media (max-width: 700px) .sr .sr-playerstrip
<<<OLD screening.css 161 1072 1073
  .sr .sr-playerstrip { gap: 8px; font-size: 11px; }
>>>

<<<NEW screening.css 161 1080 1081
  .sr .sr-playerstrip { gap: var(--sr-s2); font-size: var(--sr-t-xs); }
>>>

### screening.css #162 [replace] base lines 1163-1163 -> head lines 1171-1171
touches: .sr .sr-btn--sm
<<<OLD screening.css 162 1162 1163
.sr .sr-btn--sm { padding: 9px 14px; font-size: 10.5px; letter-spacing: 0.08em; }
>>>

<<<NEW screening.css 162 1170 1171
.sr .sr-btn--sm { padding: 9px var(--sr-s4); font-size: var(--sr-t-xs); letter-spacing: 0.08em; }
>>>

### screening.css #163 [replace] base lines 1180-1180 -> head lines 1188-1188
touches: .sr .sr-btn--subtle
<<<OLD screening.css 163 1179 1180
  padding-left: 10px; padding-right: 10px; }
>>>

<<<NEW screening.css 163 1187 1188
  padding-left: var(--sr-s3); padding-right: var(--sr-s3); }
>>>

### screening.css #164 [replace] base lines 1205-1205 -> head lines 1213-1213
touches: .sr .sr-label
<<<OLD screening.css 164 1204 1205
.sr .sr-label { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

<<<NEW screening.css 164 1212 1213
.sr .sr-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

### screening.css #165 [replace] base lines 1209-1209 -> head lines 1217-1217
touches: .sr .sr-error
<<<OLD screening.css 165 1208 1209
.sr .sr-error { margin: 0; display: flex; align-items: center; gap: 6px;
>>>

<<<NEW screening.css 165 1216 1217
.sr .sr-error { margin: 0; display: flex; align-items: center; gap: var(--sr-s2);
>>>

### screening.css #166 [replace] base lines 1225-1226 -> head lines 1233-1234
touches: .sr .sr-badge
<<<OLD screening.css 166 1224 1226
  font: 700 9.5px/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: 4px 8px; border: 1px solid var(--sr-line-raised); border-radius: 0;
>>>

<<<NEW screening.css 166 1232 1234
  font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: var(--sr-s1) var(--sr-s2); border: 1px solid var(--sr-line-raised); border-radius: 0;
>>>

### screening.css #167 [replace] base lines 1256-1257 -> head lines 1264-1265
touches: .sr .sr-dialog-kicker
<<<OLD screening.css 167 1255 1257
.sr .sr-dialog-kicker { margin: 0 0 10px; font: 500 10px/1 var(--sr-mono);
  letter-spacing: 0.26em; text-transform: uppercase; color: var(--sr-amber); }
>>>

<<<NEW screening.css 167 1263 1265
.sr .sr-dialog-kicker { margin: 0 0 var(--sr-s3); font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-amber); }
>>>

### screening.css #168 [replace] base lines 1260-1260 -> head lines 1268-1268
touches: .sr .sr-dialog-actions
<<<OLD screening.css 168 1259 1260
.sr .sr-dialog-actions { display: flex; justify-content: flex-end; gap: 10px; }
>>>

<<<NEW screening.css 168 1267 1268
.sr .sr-dialog-actions { display: flex; justify-content: flex-end; gap: var(--sr-s3); }
>>>

### screening.css #169 [replace] base lines 1304-1304 -> head lines 1312-1312
touches: .sr .sr-toasts
<<<OLD screening.css 169 1303 1304
  display: flex; flex-direction: column; gap: 8px; max-width: min(360px, calc(100vw - 40px)); }
>>>

<<<NEW screening.css 169 1311 1312
  display: flex; flex-direction: column; gap: var(--sr-s2); max-width: min(360px, calc(100vw - 40px)); }
>>>

### screening.css #170 [replace] base lines 1306-1306 -> head lines 1314-1314
touches: .sr .sr-toast
<<<OLD screening.css 170 1305 1306
  border-left: 2px solid var(--sr-stock); padding: 12px 16px;
>>>

<<<NEW screening.css 170 1313 1314
  border-left: 2px solid var(--sr-stock); padding: var(--sr-s3) var(--sr-s4);
>>>

### screening.css #171 [replace] base lines 1327-1328 -> head lines 1335-1336
touches: .sr .flow-chrome
<<<OLD screening.css 171 1326 1328
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 22px; background: var(--sr-house); border-bottom: 1px solid var(--sr-line); }
>>>

<<<NEW screening.css 171 1334 1336
  display: flex; align-items: center; justify-content: space-between; gap: var(--sr-s4);
  padding: var(--sr-s4) var(--sr-s5); background: var(--sr-house); border-bottom: 1px solid var(--sr-line); }
>>>

### screening.css #172 [replace] base lines 1332-1333 -> head lines 1340-1341
touches: .sr .flow-brand small
<<<OLD screening.css 172 1331 1333
.sr .flow-brand small { display: block; margin-top: 3px; font: 500 11px/1 var(--sr-mono);
  letter-spacing: 0.26em; text-transform: uppercase; color: var(--sr-ink-ghost); }
>>>

<<<NEW screening.css 172 1339 1341
.sr .flow-brand small { display: block; margin-top: 3px; font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-ink-ghost); }
>>>

### screening.css #173 [replace] base lines 1336-1337 -> head lines 1344-1345
touches: .sr .flow-foot
<<<OLD screening.css 173 1335 1337
  gap: 16px; margin-top: 56px; padding: 18px 22px; border-top: 1px solid var(--sr-line);
  font: 500 10px/1.6 var(--sr-mono); letter-spacing: 0.14em; text-transform: uppercase;
>>>

<<<NEW screening.css 173 1343 1345
  gap: var(--sr-s4); margin-top: 56px; padding: var(--sr-s4) var(--sr-s5); border-top: 1px solid var(--sr-line);
  font: 500 var(--sr-t-xs)/1.6 var(--sr-mono); letter-spacing: 0.14em; text-transform: uppercase;
>>>

### screening.css #174 [replace] base lines 1396-1396 -> head lines 1404-1404
touches: .sr-admin .sr-anchors a
<<<OLD screening.css 174 1395 1396
.sr-admin .sr-anchors a { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening.css 174 1403 1404
.sr-admin .sr-anchors a { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening.css #175 [replace] base lines 1417-1418 -> head lines 1425-1426
touches: .sr .work-crumbs
<<<OLD screening.css 175 1416 1418
.sr .work-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin: 0 0 14px; font: 500 10px/1.5 var(--sr-mono); letter-spacing: 0.12em;
>>>

<<<NEW screening.css 175 1424 1426
.sr .work-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sr-s2);
  margin: 0 0 var(--sr-s4); font: 500 var(--sr-t-xs)/1.5 var(--sr-mono); letter-spacing: 0.12em;
>>>

### screening.css #176 [replace] base lines 1422-1424 -> head lines 1430-1432
touches: .sr .work-pn | .sr .work-pn-link
<<<OLD screening.css 176 1421 1424
.sr .work-pn { display: flex; justify-content: space-between; gap: 16px;
  margin: 28px 0 8px; padding: 18px 0; border-top: 1px solid var(--sr-line); }
.sr .work-pn-link { display: grid; gap: 6px; text-decoration: none; max-width: 46%; }
>>>

<<<NEW screening.css 176 1429 1432
.sr .work-pn { display: flex; justify-content: space-between; gap: var(--sr-s4);
  margin: 28px 0 var(--sr-s2); padding: var(--sr-s4) 0; border-top: 1px solid var(--sr-line); }
.sr .work-pn-link { display: grid; gap: var(--sr-s2); text-decoration: none; max-width: 46%; }
>>>

### screening.css #177 [replace] base lines 1431-1433 -> head lines 1439-1441
touches: .sr .sr-kickrow--mt10 | .sr .sr-kickrow--mt12 | .sr .sr-kickrow--mt14
<<<OLD screening.css 177 1430 1433
.sr .sr-kickrow--mt10 { margin-top: 10px; }
.sr .sr-kickrow--mt12 { margin-top: 12px; }
.sr .sr-kickrow--mt14 { margin-top: 14px; }
>>>

<<<NEW screening.css 177 1438 1441
.sr .sr-kickrow--mt10 { margin-top: var(--sr-s3); }
.sr .sr-kickrow--mt12 { margin-top: var(--sr-s3); }
.sr .sr-kickrow--mt14 { margin-top: var(--sr-s4); }
>>>

### screening.css #178 [replace] base lines 1435-1435 -> head lines 1443-1443
touches: .sr .sr-lede--mt14
<<<OLD screening.css 178 1434 1435
.sr .sr-lede--mt14 { margin-top: 14px; }
>>>

<<<NEW screening.css 178 1442 1443
.sr .sr-lede--mt14 { margin-top: var(--sr-s4); }
>>>

### screening.css #179 [replace] base lines 1440-1441 -> head lines 1448-1449
touches: .sr .sr-cta-final .sr-title | .sr .sr-cta-final .sr-titlecard-actions
<<<OLD screening.css 179 1439 1441
.sr .sr-cta-final .sr-title { font-size: clamp(26px, 3vw, 44px); }
.sr .sr-cta-final .sr-titlecard-actions { margin-top: 20px; }
>>>

<<<NEW screening.css 179 1447 1449

.sr .sr-cta-final .sr-titlecard-actions { margin-top: var(--sr-s5); }
>>>

### screening.css #180 [replace] base lines 1444-1444 -> head lines 1452-1452
touches: .sr .sr-availstrip
<<<OLD screening.css 180 1443 1444
.sr .sr-availstrip { margin-top: 14px; }
>>>

<<<NEW screening.css 180 1451 1452
.sr .sr-availstrip { margin-top: var(--sr-s4); }
>>>

### screening.css #181 [replace] base lines 1454-1454 -> head lines 1462-1462
touches: .sr .sr-screen-empty
<<<OLD screening.css 181 1453 1454
  flex-direction: column; gap: 6px; max-width: 46ch; color: var(--sr-ink-mute);
>>>

<<<NEW screening.css 181 1461 1462
  flex-direction: column; gap: var(--sr-s2); max-width: 46ch; color: var(--sr-ink-mute);
>>>

## screening-admin.css  (23 blocks)

### screening-admin.css #1 [replace] base lines 517-517 -> head lines 517-517
touches: @media (max-width: 700px) .sr .site-nav
<<<OLD screening-admin.css 1 516 517
  .sr .site-nav { padding: 14px 16px; }
>>>

<<<NEW screening-admin.css 1 516 517
  .sr .site-nav { padding: var(--sr-s4) var(--sr-s4); }
>>>

### screening-admin.css #2 [replace] base lines 520-520 -> head lines 520-520
touches: @media (max-width: 700px) .sr .site-footer
<<<OLD screening-admin.css 2 519 520
  .sr .site-footer { padding: 30px 16px 90px; }
>>>

<<<NEW screening-admin.css 2 519 520
  .sr .site-footer { padding: var(--sr-s6) var(--sr-s4) 90px; }
>>>

### screening-admin.css #3 [replace] base lines 523-523 -> head lines 523-523
touches: @media (max-width: 700px) .sr .sr-playerstrip
<<<OLD screening-admin.css 3 522 523
  .sr .sr-playerstrip { gap: 8px; font-size: 11px; }
>>>

<<<NEW screening-admin.css 3 522 523
  .sr .sr-playerstrip { gap: var(--sr-s2); font-size: var(--sr-t-xs); }
>>>

### screening-admin.css #4 [replace] base lines 627-627 -> head lines 627-627
touches: .sr .sr-btn--sm
<<<OLD screening-admin.css 4 626 627
.sr .sr-btn--sm { padding: 9px 14px; font-size: 10.5px; letter-spacing: 0.08em; }
>>>

<<<NEW screening-admin.css 4 626 627
.sr .sr-btn--sm { padding: 9px var(--sr-s4); font-size: var(--sr-t-xs); letter-spacing: 0.08em; }
>>>

### screening-admin.css #5 [replace] base lines 644-644 -> head lines 644-644
touches: .sr .sr-btn--subtle
<<<OLD screening-admin.css 5 643 644
  padding-left: 10px; padding-right: 10px; }
>>>

<<<NEW screening-admin.css 5 643 644
  padding-left: var(--sr-s3); padding-right: var(--sr-s3); }
>>>

### screening-admin.css #6 [replace] base lines 669-669 -> head lines 669-669
touches: .sr .sr-label
<<<OLD screening-admin.css 6 668 669
.sr .sr-label { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

<<<NEW screening-admin.css 6 668 669
.sr .sr-label { font: 500 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.18em;
>>>

### screening-admin.css #7 [replace] base lines 673-673 -> head lines 673-673
touches: .sr .sr-error
<<<OLD screening-admin.css 7 672 673
.sr .sr-error { margin: 0; display: flex; align-items: center; gap: 6px;
>>>

<<<NEW screening-admin.css 7 672 673
.sr .sr-error { margin: 0; display: flex; align-items: center; gap: var(--sr-s2);
>>>

### screening-admin.css #8 [replace] base lines 689-690 -> head lines 689-690
touches: .sr .sr-badge
<<<OLD screening-admin.css 8 688 690
  font: 700 9.5px/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: 4px 8px; border: 1px solid var(--sr-line-raised); border-radius: 0;
>>>

<<<NEW screening-admin.css 8 688 690
  font: 700 var(--sr-t-xs)/1 var(--sr-mono); letter-spacing: 0.12em; text-transform: uppercase;
  padding: var(--sr-s1) var(--sr-s2); border: 1px solid var(--sr-line-raised); border-radius: 0;
>>>

### screening-admin.css #9 [replace] base lines 720-721 -> head lines 720-721
touches: .sr .sr-dialog-kicker
<<<OLD screening-admin.css 9 719 721
.sr .sr-dialog-kicker { margin: 0 0 10px; font: 500 10px/1 var(--sr-mono);
  letter-spacing: 0.26em; text-transform: uppercase; color: var(--sr-amber); }
>>>

<<<NEW screening-admin.css 9 719 721
.sr .sr-dialog-kicker { margin: 0 0 var(--sr-s3); font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-amber); }
>>>

### screening-admin.css #10 [replace] base lines 724-724 -> head lines 724-724
touches: .sr .sr-dialog-actions
<<<OLD screening-admin.css 10 723 724
.sr .sr-dialog-actions { display: flex; justify-content: flex-end; gap: 10px; }
>>>

<<<NEW screening-admin.css 10 723 724
.sr .sr-dialog-actions { display: flex; justify-content: flex-end; gap: var(--sr-s3); }
>>>

### screening-admin.css #11 [replace] base lines 768-768 -> head lines 768-768
touches: .sr .sr-toasts
<<<OLD screening-admin.css 11 767 768
  display: flex; flex-direction: column; gap: 8px; max-width: min(360px, calc(100vw - 40px)); }
>>>

<<<NEW screening-admin.css 11 767 768
  display: flex; flex-direction: column; gap: var(--sr-s2); max-width: min(360px, calc(100vw - 40px)); }
>>>

### screening-admin.css #12 [replace] base lines 770-770 -> head lines 770-770
touches: .sr .sr-toast
<<<OLD screening-admin.css 12 769 770
  border-left: 2px solid var(--sr-stock); padding: 12px 16px;
>>>

<<<NEW screening-admin.css 12 769 770
  border-left: 2px solid var(--sr-stock); padding: var(--sr-s3) var(--sr-s4);
>>>

### screening-admin.css #13 [replace] base lines 791-792 -> head lines 791-792
touches: .sr .flow-chrome
<<<OLD screening-admin.css 13 790 792
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 22px; background: var(--sr-house); border-bottom: 1px solid var(--sr-line); }
>>>

<<<NEW screening-admin.css 13 790 792
  display: flex; align-items: center; justify-content: space-between; gap: var(--sr-s4);
  padding: var(--sr-s4) var(--sr-s5); background: var(--sr-house); border-bottom: 1px solid var(--sr-line); }
>>>

### screening-admin.css #14 [replace] base lines 796-797 -> head lines 796-797
touches: .sr .flow-brand small
<<<OLD screening-admin.css 14 795 797
.sr .flow-brand small { display: block; margin-top: 3px; font: 500 11px/1 var(--sr-mono);
  letter-spacing: 0.26em; text-transform: uppercase; color: var(--sr-ink-ghost); }
>>>

<<<NEW screening-admin.css 14 795 797
.sr .flow-brand small { display: block; margin-top: 3px; font: 500 var(--sr-t-xs)/1 var(--sr-mono);
  letter-spacing: var(--sr-track-label); text-transform: uppercase; color: var(--sr-ink-ghost); }
>>>

### screening-admin.css #15 [replace] base lines 800-801 -> head lines 800-801
touches: .sr .flow-foot
<<<OLD screening-admin.css 15 799 801
  gap: 16px; margin-top: 56px; padding: 18px 22px; border-top: 1px solid var(--sr-line);
  font: 500 10px/1.6 var(--sr-mono); letter-spacing: 0.14em; text-transform: uppercase;
>>>

<<<NEW screening-admin.css 15 799 801
  gap: var(--sr-s4); margin-top: 56px; padding: var(--sr-s4) var(--sr-s5); border-top: 1px solid var(--sr-line);
  font: 500 var(--sr-t-xs)/1.6 var(--sr-mono); letter-spacing: 0.14em; text-transform: uppercase;
>>>

### screening-admin.css #16 [replace] base lines 860-860 -> head lines 860-860
touches: .sr-admin .sr-anchors a
<<<OLD screening-admin.css 16 859 860
.sr-admin .sr-anchors a { font: 500 10px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

<<<NEW screening-admin.css 16 859 860
.sr-admin .sr-anchors a { font: 500 11px/1 var(--sr-mono); letter-spacing: 0.14em;
>>>

### screening-admin.css #17 [replace] base lines 881-882 -> head lines 881-882
touches: .sr .work-crumbs
<<<OLD screening-admin.css 17 880 882
.sr .work-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin: 0 0 14px; font: 500 10px/1.5 var(--sr-mono); letter-spacing: 0.12em;
>>>

<<<NEW screening-admin.css 17 880 882
.sr .work-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sr-s2);
  margin: 0 0 var(--sr-s4); font: 500 var(--sr-t-xs)/1.5 var(--sr-mono); letter-spacing: 0.12em;
>>>

### screening-admin.css #18 [replace] base lines 886-888 -> head lines 886-888
touches: .sr .work-pn | .sr .work-pn-link
<<<OLD screening-admin.css 18 885 888
.sr .work-pn { display: flex; justify-content: space-between; gap: 16px;
  margin: 28px 0 8px; padding: 18px 0; border-top: 1px solid var(--sr-line); }
.sr .work-pn-link { display: grid; gap: 6px; text-decoration: none; max-width: 46%; }
>>>

<<<NEW screening-admin.css 18 885 888
.sr .work-pn { display: flex; justify-content: space-between; gap: var(--sr-s4);
  margin: 28px 0 var(--sr-s2); padding: var(--sr-s4) 0; border-top: 1px solid var(--sr-line); }
.sr .work-pn-link { display: grid; gap: var(--sr-s2); text-decoration: none; max-width: 46%; }
>>>

### screening-admin.css #19 [replace] base lines 895-897 -> head lines 895-897
touches: .sr .sr-kickrow--mt10 | .sr .sr-kickrow--mt12 | .sr .sr-kickrow--mt14
<<<OLD screening-admin.css 19 894 897
.sr .sr-kickrow--mt10 { margin-top: 10px; }
.sr .sr-kickrow--mt12 { margin-top: 12px; }
.sr .sr-kickrow--mt14 { margin-top: 14px; }
>>>

<<<NEW screening-admin.css 19 894 897
.sr .sr-kickrow--mt10 { margin-top: var(--sr-s3); }
.sr .sr-kickrow--mt12 { margin-top: var(--sr-s3); }
.sr .sr-kickrow--mt14 { margin-top: var(--sr-s4); }
>>>

### screening-admin.css #20 [replace] base lines 899-899 -> head lines 899-899
touches: .sr .sr-lede--mt14
<<<OLD screening-admin.css 20 898 899
.sr .sr-lede--mt14 { margin-top: 14px; }
>>>

<<<NEW screening-admin.css 20 898 899
.sr .sr-lede--mt14 { margin-top: var(--sr-s4); }
>>>

### screening-admin.css #21 [replace] base lines 904-905 -> head lines 904-905
touches: .sr .sr-cta-final .sr-title | .sr .sr-cta-final .sr-titlecard-actions
<<<OLD screening-admin.css 21 903 905
.sr .sr-cta-final .sr-title { font-size: clamp(26px, 3vw, 44px); }
.sr .sr-cta-final .sr-titlecard-actions { margin-top: 20px; }
>>>

<<<NEW screening-admin.css 21 903 905

.sr .sr-cta-final .sr-titlecard-actions { margin-top: var(--sr-s5); }
>>>

### screening-admin.css #22 [replace] base lines 908-908 -> head lines 908-908
touches: .sr .sr-availstrip
<<<OLD screening-admin.css 22 907 908
.sr .sr-availstrip { margin-top: 14px; }
>>>

<<<NEW screening-admin.css 22 907 908
.sr .sr-availstrip { margin-top: var(--sr-s4); }
>>>

### screening-admin.css #23 [replace] base lines 918-918 -> head lines 918-918
touches: .sr .sr-screen-empty
<<<OLD screening-admin.css 23 917 918
  flex-direction: column; gap: 6px; max-width: 46ch; color: var(--sr-ink-mute);
>>>

<<<NEW screening-admin.css 23 917 918
  flex-direction: column; gap: var(--sr-s2); max-width: 46ch; color: var(--sr-ink-mute);
>>>
