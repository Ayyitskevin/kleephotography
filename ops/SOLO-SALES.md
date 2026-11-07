# Solo sales workflow

Mise is the first target for the reusable sales-workspace findings in private Vision.
The operator is one person. Keep photography language, the existing stack and the
existing proposal, contract, payment and delivery flows.

## First slice: Sales review

The Board shows up to five early-stage opportunities with an existing pipeline
`days_quiet` of at least seven days, oldest first, then recorded value, then ID.
The total includes matching opportunities beyond the first five. Every project
remains in the Board/List/Map. The queue refreshes with the whole-board HTMX
fragment after an existing stage move; it is available without JavaScript.

Only inquiry_received, consultation_call and proposal_sent qualify. This is an
advisory review list, not scheduled tasks, a reply detector or permission to send.
Recorded client activity includes document actions as well as inbound messages.
When there is no recorded activity, the existing age falls back to stage entry or
creation; the UI says so. Moving a stage can reset that fallback age. Missing age
is excluded; missing value is shown explicitly and sorts as zero. Financial
projections are reused without changing their meaning. This is project review,
not deduplicated contact-level outreach: check other projects and the conversation
history before reaching out. External calls and mailbox activity may be absent.

## Next slices

1. Prospect capture with business/contact, specialty, source and qualification;
   distinguish outbound prospects from inbound inquiries and paying clients.
   Search before creation and define deduplication before importing lists.
2. Explicit next-action dates, contact attempts and outcomes. Keep outbound
   attempts separate from client responses. Due tasks should use their own clock,
   not the current stage-age fallback.
3. Contextual message drafts reviewed and sent by the operator; bind every draft
   to its contact/project so switching records cannot send the wrong text.
4. Stop/snooze rules with a recorded reason; preserve communication preferences
   across projects. A silent prospect is not automatically lost or opted out.

Prospect/task persistence needs a separately reviewed schema PR. Sending/provider
work, payment changes, contracts and deployment keep their existing review gates.
Do not infer that a manual send or an external call succeeded from a button click.

## Verification

`tests/test_studio_sales_review.py` covers eligibility, deterministic ordering,
visible truncation count, auth, escaping, read-only GET, and refreshed stage-move
fragments against disposable SQLite. The route test does not start background
jobs or senders. Full integration/smoke run in hosted CI. Browser appearance and
production deployment require separate verification.
