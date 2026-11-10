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

## Second slice: outbound prospect capture

`/admin/studio/prospects` holds manually researched outbound businesses separately
from inbound inquiries, clients and projects. Studio, Clients, the full index and
the command menu link to it. The paginated list supports a literal substring
search on business, contact, email and phone. It does not feed pipeline values,
inquiry counts, revenue, senders, jobs or Notion mirrors.

Capture requires a business/venue and its source. Contact name, email, phone,
photography specialty and research notes can remain blank. Qualification is the
operator's assessment: **Needs research**, **Potential fit**, or **Not a fit**.
It is not communication consent, an opt-out, evidence of a reply, or a booking.
Details and qualification remain editable on the prospect page.

### Check before saving

The form first checks existing prospects, client records, and all inbound
inquiries (including archived/converted ones). A match on any of these blocks
creation and links to the existing record:

- Email after whitespace trimming and case normalization. No Gmail-specific
  dot or plus-address rewriting.
- Phone after formatting removal. US ten-digit and `+1` forms match; other
  country codes are not inferred. Extensions belong in notes.
- Business **and** contact name after case/whitespace normalization. An empty
  contact name still matches another unnamed prospect at that business.

The final save repeats this check under `BEGIN IMMEDIATE`. A record arriving
after review, or a double submit, cannot evade the check. Database uniqueness
also protects the prospect identity keys. Creation and edits append an audit
record in the same transaction; an audit failure rolls the write back.

Changing identity fields repeats the check, excluding the prospect being edited.
Research/qualification-only edits remain possible if an inbound inquiry arrives
later. Existing inquiry/client creation flows remain unchanged: this slice does
not merge, convert, link, or enforce uniqueness across their future writes.

These are exact-match safeguards, not entity resolution. Different spellings,
different contact channels, or a missing business can hide duplicates. Shared
mailboxes and switchboard numbers can also match legitimately distinct contacts;
there is deliberately no bypass/import path yet. Review the linked records.
Comparison currently scans the three small CRM tables; bulk importing needs an
indexed identity design and a reviewed conflict-resolution workflow first.

### Migration and recovery

`084_outbound_prospects.sql` adds only `prospects`; existing tables and rows are
untouched. This is a schema PR requiring human review and merge. Deployment is a
separate step. For an application rollback, leave the additive table and its
audit history intact; older code ignores them. Do not drop captured research.

### Handoff after the move

1. Review this schema PR and its test evidence.
2. Visually check list, review, duplicate and edit pages in Screening Room and
   legacy mode, at desktop and phone widths. The remote browser could not reach
   this session's local preview, so visual verification remains open.
3. Use the normal reviewed deployment procedure when the origin is accessible.
4. Next build: dated next actions and contact outcomes linked to prospect IDs,
   followed by operator-reviewed drafts. Define prospect-to-client conversion
   and communication preference inheritance before adding any sender/import.

`tests/test_outbound_prospects.py` exercises authentication, cross-origin refusal,
read-only review/GET, input validation, escaping, duplicate matches, stale
reviews, concurrent saves, audit rollback and migration over an existing DB.
These new route tests use a disposable database with no application lifespan or
background workers. Existing integration/smoke suites retain their own fixtures.

### Development verification (2026-11-09)

Run on Python 3.12.14 using `requirements-dev.lock`, based on main `12adf3f`.

| Check | Result |
| --- | --- |
| New prospect tests | 35 passed |
| Full unit gate | 457 passed |
| Full integration gate | 1,086 passed |
| Full smoke gate | 188 passed |
| JavaScript tests | 50 passed |
| Ruff lint and format, migration numbering, diff whitespace | Passed |
| Bandit high-severity scan and changed-file secret scan | Passed |
| Runtime dependency audit | No known vulnerabilities found |

No tests were skipped in these selected gates; marker deselections select the
other gates. Existing third-party TestClient deprecation warnings remain.
Integration/smoke data used a disposable workspace directory because `/tmp` was
below the existing 5 GB free-space guard. The Argus enqueue tests now use the
repository's worker-freezing fixture: their earlier TestClient lifespan could
dispatch synthetic gallery jobs to a placeholder URL. Transport tests continue
to use their explicit HTTP mocks; production Argus behavior is unchanged.

## Remaining slices

1. Explicit next-action dates, contact attempts and outcomes. Keep outbound
   attempts separate from client responses. Due tasks should use their own clock,
   not the current stage-age fallback.
2. Contextual message drafts reviewed and sent by the operator; bind every draft
   to its contact/project so switching records cannot send the wrong text.
3. Stop/snooze rules with a recorded reason; preserve communication preferences
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
