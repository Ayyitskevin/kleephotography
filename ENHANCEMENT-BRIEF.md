# ENHANCEMENT BRIEF — competitive benchmark → work queue (2026-08)

**Produced by:** a benchmark of Mise against the commercial photography stack, run
2026-08-21 against `main` @ `0606fd2`, cross-checked against the code by an adversarial
critic pass. Every claim below was verified by opening the file; findings that turned out
false on inspection are recorded in §5 rather than deleted, so nobody re-derives them.

**Benchmarked against:** Pixieset, Pic-Time, ShootProof, CloudSpot, Zenfolio, SmugMug
(delivery) · HoneyBook, Dubsado, Studio Ninja, Táve, Sprout Studio, Iris Works, 17hats,
Bloom, Pixieset Studio, Session (CRM) · Flothemes, Squarespace, Showit, Format, Adobe
Portfolio (sites) · WHCC, Miller's, ProDPI (labs) · Aftershoot, Narrative, Imagen (AI).

**How to use this file:** it is the sibling of [`UPGRADE-BRIEF.md`](UPGRADE-BRIEF.md)
(which reviewed the code against itself) and [`REVENUE-ROADMAP.md`](REVENUE-ROADMAP.md)
(which is the revenue engine's build-out). This one asks a different question: *where does
Mise sit against the products a working photographer would otherwise be paying for?*
`AGENTS.md` overrides everything here.

---

## 1. The finding

Mise is **ahead** of the commercial tools on craft. Nothing in the benchmarked set has its
security posture, its transactional job staging, its distrusting Stripe webhook, its
conditional-media discipline, or its habit of writing down why a thing is the way it is.
Several ship *worse* galleries than this one.

The gaps that remain are not craft gaps. They are three kinds of thing:

1. **Data Mise already generates and never publishes.** The alt text was the headline case:
   Argus has written a real description of every analyzed frame since migration 064, and
   its only reader was an admin hover overlay.
2. **Bytes shipped more than once.** JPEG-only derivatives, and public media routes that
   set a cache window and then never honoured a conditional request.
3. **Owner controls the competition treats as table stakes.** Download rules, dunning,
   presentation control. Most of these need schema, so most of them are red-light.

The honest summary of the money side: **the four items that would move revenue most all
need either a migration or the money path.** They are §3, and they need Kevin.

---

## 2. Landed (this pass, all green-light, all four gates green per commit)

| # | Change | Why it mattered |
|---|--------|-----------------|
| 1 | Publish the per-frame alt text Argus writes | `/portfolio` shipped one identical alt string per tag; the gallery shipped `"— frame 0042"` |
| 2 | Conditional requests on `/site/img`, `/site/vid`, `/site/poster` | Same defect already fixed for `/media/*`; extracted to `app/http_cache.py` rather than copied |
| 3 | `sitemap.xml` image + video children | Twelve page URLs and nothing else, for a studio whose product is the frames |
| 4 | Share debugger og:image parity | `/admin/share` promised "what the socials will scrape" and previewed a different frame |
| 5 | AVIF/WebP derivatives, negotiated per request | Every derivative ever written was a JPEG. Pillow 12.3.0 already carries both codecs |
| 6 | Notes on stills, not just films | `video_comments` never had a kind constraint; one WHERE clause and a video-only viewer branch |
| 7 | Favourites + client notes on the deck wire | The strongest buying signal in the business reached the owner nowhere |
| 8 | ZIP_DIR eviction | Every prune was per-gallery; nothing swept the directory, so disk crept toward 2× the media library |

Measured before this pass: public pages shipped **288 KB of CSS/JS (63 KB gzipped)**;
derivatives were JPEG-only; AVIF/WebP encode measured **0.08–0.29 s** per 2048px frame,
which fits the existing durable job queue.

---

## 3. RED-LIGHT queue — each is its own PR, Kevin merges

Ranked by revenue impact per unit of work. All are red under `AGENTS.md` (schema, money
path, or client-signed artefacts).

### R1. Client-facing invoice dunning — HIGH / M
An overdue invoice produces a dashboard nudge and a board tally. Nothing ever reaches the
client — `app/scheduler.py` has eleven sweeps and no invoice sweep at all. Every
benchmarked CRM ships this; **Pixieset Studio Manager ships client chase loops with no
workflow engine at all**, just three hard-coded state checks. This is the 80/20 of studio
automation and Mise is at the wrong end of it.
*Shape:* a dunning sweep on the existing scheduler thread — polite at due+3, firmer at
due+7 and +14, idempotent per (invoice, stage), stamped only after a successful send.
**Strictly notification-only**: no late fees, no auto-charge, so it never writes payments
state, which must stay single-writer through the Stripe webhook.
*Red because:* it acts on invoice state and needs a one-shot tracker (new columns).

### R2. Post-delivery sales sequence — HIGH / M
The delivery window is spent on one email. `app/gallery_reminders.py` sends a T-3d expiry
nudge and a 5-day proofing nudge; neither asks for money. The Plutus-generated print and
album bundles that already exist render only on the admin rail — the client never sees
them. ShootProof's aggregate data (vendor-framed, no published methodology) puts galleries
with email campaigns at 220% more orders, with 55% of orders landing in the first two weeks.
*Shape:* staged, keyed off delivery — ready → day 3 "still time to pick" → day 10 print
offer surfacing the existing Plutus bundle URL → anniversary. Reuse the sweep shape and the
stamp-only-after-successful-send discipline from `booking_reminders.py`, and the per-client
cooldown from `review_requests.py`. Pair client-facing sends with an approval gate so the
manual-send doctrine in `app/scheduler.py` stays intact.
*Red because:* needs a durable per-gallery/per-stage one-shot — new table or columns.

### R3. Notify on proposal accept / decline — HIGH / S
`app/public/docs.py` accept and decline each do one UPDATE and a `log.info`. The owner
discovers a yes by refreshing the board. This is the highest-value notification in the
funnel and it is silent.
*Shape:* a Telegram line and an owner email on both, via the existing dormant-channel
pattern. **Do not** move the stage from send to accept as part of this — `proposal_sent`
is only reachable because the stage advances on send, and moving it would make that stage
unreachable rather than making the funnel more honest.
*Red because:* `docs.py` also carries the contract-sign path, so the safe read of
`AGENTS.md` ("anything a client signs") puts it behind a PR even though the diff is small.

### R4. Per-gallery download controls — HIGH / M
The owner has no control over downloads: no column exists, the settings form does not offer
one, and `downloads.py` serves full-resolution originals to anyone past the email gate.
Pixieset, ShootProof and Pic-Time all treat download toggles, per-size sets and limits as
table stakes.
*Shape:* columns for `download_enabled`, `download_size` (web vs original), an optional
limit, and an optional separate download PIN. Mise's visitor model is already closer to
ShootProof's best feature than any competitor's — every visitor has a token row and
downloads are already logged per `visitor_id` — so per-recipient entitlement is an
extension, not a rewrite.
*Red because:* new columns, and the download-PIN half is security-adjacent.

### R5. Print store with lab fulfilment — HIGH / XL
The revenue path stops at a free-text quote request. A full table inventory confirms no
orders/cart/products/price_lists/coupons/gift_cards tables — this is a new money subsystem,
not a feature. `REVENUE-ROADMAP.md` already defers it pending conversion evidence from the
manual quote probe, and **that evidence is currently unreadable** (see G1 below: the quote
email reports the wrong favourite count). Fix the instrument before reading it.
*When it earns its spec:* WHCC exposes a documented JSON Order Submit API with HMAC-signed
webhooks — the same trust shape `pay.py` already implements for Stripe — plus an Editor API
that embeds an unbranded designer. Use Pic-Time's payment architecture: the client pays
Mise's own Stripe, the lab bills the studio, zero commission. Note WHCC credentials need
manual approval, which is calendar time before line one.

### R6. UTM / referrer / landing-page capture — MED / S
`inquiries` stores a fixed-vocabulary `referral_source` dropdown and nothing else; a grep
for `utm_` across the repo returns nothing. Every paid or campaign click is unattributable.
*Red because:* new columns on `inquiries` and `bookings`.

### R7. Watermarking as a rendition variant — LOW / L
Deliberately ranked low. `asset_renditions` already exists and `_apply_overlay` is already
written, so the shape is clear — but for real estate, F&B and commercial portrait work the
files ARE the deliverable and the client has paid before delivery. Protected previews solve
a problem this niche mostly does not have. **Build it if and only if a pre-payment proofing
flow goes on the roadmap.**

---

## 4. GREEN-LIGHT queue — remaining, ranked

Verified against the code and not yet done. An agent may take these without a human.

**High**
- **G1. The print-quote email reports the wrong favourite count.** `app/public/gallery.py`
  counts every favourite row in the gallery across ALL visitors. Blocks R5's demand signal.
  *Careful:* the naive fix (`AND f.visitor_id=?`) makes it correct for one device and zero
  for the other — a new visitor row is minted on every successful PIN check, so a client who
  circled on their phone and requested from their laptop would report zero. Count per
  gallery-and-email, or state the scope in the email.
- **G2. Mobile/iOS per-file save path.** Four ZIP endpoints and no single-file save flow;
  iOS Safari handles a large ZIP badly. Called the highest-leverage gap in the delivery
  category.
- **G3. "Mark sent" and "email it" are unlinked.** Nothing reconciles a document marked
  `sent` against whether it was ever emailed. A proposal the client never received cannot
  convert, and nothing surfaces the discrepancy.
- **G4. Deepen the LocalBusiness entity graph.** No `@id`, no `telephone`, no
  `openingHours`, no `geo`. Cheap local-SEO surface.
- **G5. Above-the-fold what/who/where + a price floor on the spokes.** The homepage H1 is
  evocative but does not say what is sold or where.

**Medium**
- Expose `require_pin` on gallery settings (the column exists; only `create_transfer`
  writes it — **no migration needed**).
- Anchor the review ask on an actual gallery view rather than `galleries.created_at`.
- Honest gallery analytics: "Client views" is counting PIN admissions, not views.
- Client share affordance + a favourites-only view.
- Lightbox zoom / fullscreen / actual-size (the enlarged image is a 2048px derivative
  scaled to 92vw).
- Notes panel is `position: fixed; width: min(340px, 86vw)` — unusable on a phone. **Any
  fix must land in both `mise.css` and `screening.css`** (`ops/CSS-DUAL-STACK.md`).
- Consent notice + opt-out at the email gate.
- Acknowledge completed selections; give the client a receipt.
- Fix the board's two lying numbers. *Careful:* adding a status filter to the pipeline-value
  subquery the way `reports.py` does it silently breaks the proposal fallback.
- Wire `argus_keywords` into admin search (needs a new asset category — not the one-liner
  it looks like).
- Acknowledge form-builder leads (the contact form does; the form builder does not).
- Preload `newsreader-italic-latin.v1.woff2` — at 147 KB it is the largest font on the site
  and the only one not preloaded.
- Gallery presentation control (cover style / grid / typography as one-click bundles) —
  Pic-Time 2.0's headline feature; Mise has a single `cover_asset_id` and nothing else.
- PDF for proposals, contracts and invoices. `mailer.send` has no attachment path but `.ics`.
- Per-gallery installable PWA. *Caveat:* a manifest alone does not produce an Android
  install prompt; that needs a registered service worker with a fetch handler.
- Resumable per-file uploads with a drop zone (currently one FormData, one XHR, all files).
- Reels CLS + a watch page per film.
- Bench reordering is one adjacent swap per click and re-renders the whole masonry.
- Favourite toggling is O(favourites) OOB fragments per click; the public gallery renders
  every asset with no pagination.

**Low**
- Two ZIP-path hardening fixes (`download_section` lacks the disk floor `download_favorites`
  has).

---

## 5. Checked and rejected — do not re-derive these

- **"~42% of blocking CSS is admin shell the public never sees."** False. The admin half is
  already split into `static/mise-admin.css` and attached only by `admin/base_admin.html`.
  What a public visitor loads is the public + shared + client half, and `base.html` documents
  the split verbatim.
- **"Specialty spoke CTAs silently degrade to `/book`."** Not a bug. `app/public/site.py`
  says so in a comment: the spoke deep-links the moment Kevin creates the conventional event
  type in the live admin, with no code redeploy.
- **"Kill the CSS `@import` waterfall by converting it to a `<link>`."** The mechanism does
  not work. No shipping browser lets a plain `<link rel=stylesheet>` join a named cascade
  layer, and an `@layer mise;` ordering statement does not capture a later link's rules.
  The waterfall is real; this particular fix is not available.
- **Face / selfie search.** Excluded as a bad fit: BIPA exposure, the obvious model is
  non-commercial-licensed, and the value is concentrated in 300-guest weddings — not real
  estate, portraits or F&B.
- **AI style-matched RAW editing.** `app/imaging.py` is a Pillow delivery pipeline and
  `PHOTO_EXTS` carries no RAW format. Wrong layer of the stack.
- **`llms.txt`.** Measurably ignored by the answer engines that were supposed to read it.

---

## 6. Conventions this pass had to learn the hard way

- **Pinned markup hooks are real** (`AGENTS.md`). Four items land on them. When behaviour
  moves, the assertion moves in the same commit — it does not get relaxed.
- **Source-contract tests in `tests/test_public_site.py`** assert the *shape* of
  `static/lightbox.js`, including that the four comment-draft helpers are a contiguous,
  ambient-state-free block. Refactors that reorder that file will fail them, and that is
  the tests working.
- **The CSS dual stack is load-bearing.** `MISE_SCREENING_ROOM=false` must stay honest, so a
  client-facing style fix usually lands twice. Prefer toggling from JS over adding a class
  when it avoids touching both sheets.
- **ffmpeg AND ffprobe** are needed for the video smoke tests, not just ffmpeg.
