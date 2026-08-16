# Revenue roadmap

The working plan from the August 2026 business review ("Mise, Graded"): the
platform graded **B+ — an A− platform carrying a C− revenue engine** — and this
file is the engine build-out, ranked by revenue per unit of work. Research
grounding (working photographers' checklists, income breakdowns, platform
complaints) lives in the review artifact; the grades and reasoning are summarized
there. Every item that touches money or schema is red-light per AGENTS.md: PR +
Kevin's merge, no exceptions.

| # | Item | Status |
|---|------|--------|
| 1 | **Pay-to-book** — Stripe reservation fee holds the slot; the mini-session engine | **built** (this PR) |
| 2 | **Google review engine** — automated post-delivery review ask | next up |
| 3 | **Lead attribution** — "how did you hear about me?" + reports rollup | **built** (this PR) |
| 4 | **List announcements** — one-shot campaigns to past clients + gallery-gate emails | queued |
| 5 | **Session-anniversary nudges** — 11 months after a portrait/family delivery | queued |
| 6 | **Prints, phase one** — "order prints of your favorites" request flow, quoted by hand | queued |
| 7 | **Expired galleries → reactivation pages** — dead end becomes a re-engagement lead | queued |

## Built in this PR

**1 — Pay-to-book.** `event_types.booking_fee_cents` (admin: "Reservation fee");
fee'd types hold the slot as `status='pending_payment'` and redirect to Stripe
Checkout. The hold occupies the slot in every conflict query but only while
younger than `MISE_BOOKING_PAY_TTL_MIN` (default 30) — expiry is exact and lazy,
the hourly sweep just tidies rows. The shared Stripe webhook confirms the hold
(idempotent via `booking_payments.stripe_event_id UNIQUE`), and only then do
confirmation emails / calendar / Notion fire. Money on an already-released hold
is acked + audited + alerted for a manual refund — never silently re-confirmed,
because the slot may have been re-sold. Free event types are byte-for-byte
unchanged. Refunds are always manual, from the Stripe dashboard; the admin
cancel dialog says so.

**3 — Lead attribution.** Optional "How did you hear about me?" select on the
contact and booking forms, answers constrained to `REFERRAL_SOURCES`
(`app/public/site_catalog.py`) so free text never reaches the rollup. Stored on
`inquiries.referral_source` (and `bookings.referral_source`, because a paid
booking's inquiry row is created at webhook time). Reports gains "How they found
you" with an honest unattributed count.

## Design notes for what's queued

**2 — Review engine.** Hang the ask on the existing delivery event (gallery
publish → reminder sweep). One warm email per delivered gallery, N days after
delivery, with a direct Google review link (`MISE_GOOGLE_REVIEW_URL`); route the
unhappy to a private reply instead. Throttle per client, never per gallery; a
`review_requested_at` column keeps it one-shot. The pros' baseline is 50+
reviews; the sweep and mailer already exist.

**4 — Announcements.** Not a marketing suite. One admin page: audience = past
clients ∪ gallery-gate emails (deduped, minus unsubscribes), one message, one
send via the existing mailer with per-recipient jitter. Needs an `unsubscribes`
table + `/u/{token}` one-click link in every send (CAN-SPAM). First real use:
announcing item 1's mini-session days.

**5 — Anniversary nudges.** Scheduler sweep: portrait/family projects delivered
~11 months ago and not since rebooked → draft a nudge for Kevin to approve, not
an auto-send. Reuses the reminder-sweep pattern and the one-heads-up throttle.

**6 — Prints phase one.** A "print your favorites" request button on delivered
galleries that opens a quote conversation (new inquiry kind `prints`). No lab
API, no cart. If requests convert, THEN spec the store; the fulfillment margin
research says exists should be proven on this client base first.

**7 — Reactivation.** The expired-gallery page offers "request this gallery
back" (inquiry kind `reactivation`) instead of a dead end. Pairs with a small
restore action in admin. Returning clients are usually returning to spend.
