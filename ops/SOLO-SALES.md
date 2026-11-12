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
4. Dated next actions, contact outcomes and the reviewed project handoff are
   described below. Operator-reviewed drafts and communication preference
   inheritance remain later work before adding any sender/import.

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

## Third slice: follow-ups and manual contact history

The prospect follow-up workspace is at `/admin/studio/prospects/follow-ups`,
linked from Home, Tasks and the prospect list. Due work, upcoming work and
prospects needing a next step are separate views. Every scheduled row explains
the action and due date; an unplanned prospect is not automatically overdue.
Not-fit prospects are excluded from the unplanned view, but an explicitly
scheduled task still appears until the operator changes it.

Each prospect can have one open next-action task. The existing `tasks` table
remains the obligation store; general and project tasks retain their behavior.
Prospect follow-ups use explicit complete, reschedule and cancel commands with
an outcome/reason note. Shared task toggle/delete routes refuse prospect tasks
and link operators to the prospect page. Cancellation is distinct from
completion, preserves history, and excludes the task from due and completed
counts. It does not mark the prospect lost, paused or opted out.

Dates use `studio._today()`: Today, Tomorrow and Next week mean that date,
plus one day and plus seven days. The form displays the resolved dates and
offers an arbitrary date picker. This is day-level planning, not an appointment
or a promise to call at an exact local time.

Contact history is manually recorded with a channel, direction, outcome and
occurrence date. The server also keeps when the entry was recorded. An outgoing
attempt and an incoming reply are different facts; a voicemail cannot become a
reply. No provider delivery is inferred. Logging contact keeps the pending task
unless the operator explicitly selects complete or reschedule. A selected task
change, contact entry and audit commit together or not at all. Task completion
alone does not manufacture a contact event or client response.

Write versions prevent stale pages from overwriting a newer task decision.
Operation keys bind retry-sensitive saves to their original payload. Retrying a
successful submission cannot create a second contact event; reusing its key for
different contents is rejected. The DB enforces one open task, exclusive
prospect/project ownership and contact-event/task ownership.

The existing project client-activity clock and sales-review criteria remain
unchanged. This feature creates no inquiries, clients, projects, messages,
provider jobs, financial records or qualification transitions. Calendar
integration and automated resolution are outside this slice.

### Migration and recovery

`085_prospect_followups.sql` adds task columns/indexes and a manual
`prospect_contact_events` table. Existing tasks retain their values. The
migration runs in the existing atomic, forward-only runner. A human reviews and
merges the schema PR; deploying to the origin is a separate action.

Keep the additive schema and history on an application rollback. **Older app
versions do not understand prospect ownership or cancellation:** their shared
Tasks controls can misclassify/corrupt these rows. If reverting application code
after prospect follow-ups have been created, suspend Task writes and use a
forward fix or restore a reviewed consistent code/database snapshot. Do not
drop follow-up/contact history as a routine rollback.

### Follow-up verification (2026-11-10 UTC)

Based on main `12470f2`, using Python 3.12.14 and the locked dependencies:

| Check | Result |
| --- | --- |
| New follow-up integration cases | 51 passed |
| Full unit gate | 457 passed |
| Full integration gate | 1,137 passed |
| Full smoke gate | 188 passed |
| Existing JavaScript tests | 50 passed |
| Combined integration/smoke app coverage | 89% (required floor: 78%) |
| Follow-up route module coverage | 97% |
| Ruff lint/format, migration numbering, whitespace | Passed |
| Bandit high-severity scan and repository secret scan | Passed |
| Locked runtime dependency audit | No known vulnerabilities found |

No tests were skipped in these selected gates; marker deselections select other
gates. The existing third-party TestClient deprecation warning remains. Broad
test data used disposable workspace directories above the storage reserve; new
feature tests use isolated SQLite databases without starting background workers.

An independent review found no remaining P1/P2 issues and reran nine focused
cases covering concurrency, migration preservation, retries, stale writes, atomic
rollback, ownership and shared task controls. Rendered-template checks covered
both UI modes, form nesting, unique IDs, CSP nonces, preserved retry identities
and the date/form script behavior. Local Chromium could not launch because the
workspace denied socket creation, so desktop/mobile visual review remains open.
No deployment, production admin write, contact or message send was performed.

## Fourth slice: reviewed prospect-to-project handoff

The prospect page now offers **Start opportunity**. First select an existing
client or a new client copied from the prospect, then choose a new opportunity
or an open project belonging to that client. Capture the photography need,
optional specialty and requested target date, explicitly confirm a real
opportunity, and review the complete plan before saving.

A new project starts in the existing `inquiry_received` stage (Inquiry), with
the photography need as its notes. It is an unbooked opportunity, not a new
inbound inquiry. The requested target lives on the handoff and does not set
`projects.shoot_date`. Linking an existing client/project preserves all its
details and its stage. No proposal, contract, invoice, payment, booking, message,
job or qualification change is created by this action.

New-client creation checks normalized email, phone and business, including a
different contact at the same business. A match requires explicitly choosing
an existing client. The write repeats this check under the transaction lock;
this is still exact matching, not fuzzy entity resolution or a uniqueness rule
over other legacy client-creation routes.

If a prospect has a pending follow-up, explicitly keep, complete or cancel it.
Complete/cancel requires an outcome or reason. Keeping it retains the same
prospect-owned task and due date, visible from the linked project. Contact
events stay on the prospect; the project and client link back to them. A saved
snapshot retains the original research even if the prospect is later edited.
Linked prospects leave the **Needs next step** queue, while their scheduled
actions remain in the due/upcoming views. Handoff alone records no reply.

The review binds the exact form and the prospect/client/project/pending-task
state for one hour. A changed record requires review again. An exact retry
returns the saved project; another operation cannot convert the same prospect
again. The client/project creation, task decision, handoff and audit rows commit
together or roll back together. Future shoots start from the linked client's
normal project controls.

### Migration and recovery

`086_prospect_handoffs.sql` adds the handoff table and project ownership index.
Existing data is not rewritten. Foreign keys retain the source prospect, client,
project and selected task; a linked client cannot be force-deleted. The delete
route checks that restriction before unlinking galleries or removing brand
files, serialized against a simultaneous handoff. The write lock remains held
during brand cleanup so another client cannot reuse the deleted integer ID and
its directory before cleanup finishes.

This is an additive, forward-only schema change requiring human review and
merge. Keep the handoff table and audit history on an application rollback.
Older client-delete handlers do not understand its retention constraint and
could remove files/unlink galleries before the database refuses deletion:
**suspend client deletion if reverting application code after handoffs exist**,
then use a forward fix or a reviewed consistent restore. The follow-up rollback
restrictions above still apply. A manual browser check in both UI modes at
desktop/mobile widths remains required before deployment.

### Handoff verification (2026-11-10)

`tests/test_prospect_handoffs.py` uses isolated SQLite databases without lifespan
workers and synthetic records. It exercises read-only review, new/reused records,
unbooked dates, preserved history, explicit task choices, stale state, review
expiry/tampering, retry/concurrent saves, competing client deletion, duplicate
business races, audit/constraint rollback, migration preservation, authentication,
origin checks and escaped rendered forms in both UI modes.

Based on main `29d819f`, using Python 3.12.14 and locked dependencies:

| Check | Result |
| --- | --- |
| New handoff integration cases | 55 passed |
| Full unit gate | 457 passed |
| Full integration gate | 1,192 passed |
| Full smoke gate | 188 passed |
| Existing JavaScript tests | 50 passed |
| Combined integration/smoke app coverage | 89% (required floor: 78%) |
| Handoff route module coverage | 96% |
| Ruff lint/format, migration numbering, whitespace | Passed |
| Bandit high-severity scan and repository secret scan | Passed |
| Locked runtime dependency audit | No known vulnerabilities found |

No selected tests were skipped; marker deselections select other gates. The
existing third-party TestClient deprecation warning remains. Broad test data
used disposable workspace directories above the storage reserve. The suites
use their existing synthetic fixtures and explicit transport mocks; no live
provider behavior was validated. Self-review and rendered-form checks completed;
this slice has not received an independent review or visual-browser verification.
No deployment, production admin write or message send was performed.

## Fifth slice: contextual outreach drafts

Open **Message drafts** from a prospect, **Prepare message** from the follow-up
queue, or the prospect source panel on its linked project. The workspace creates
editable email/text starters for an introduction, follow-up to an outgoing
attempt, or a recorded conversation/reply. A preview is read-only; saving creates
a draft, then its editor supports changes and separate subject/message copying.

This first version uses fixed, original wording with the prospect's identity,
photography specialty and a selected contact record. It does not call an AI
provider. The operator can add a personal sentence and one next step; these are
included verbatim. Research notes, the photography brief, task title and requested
date stay in a reference panel unless the operator chooses to put them in the
message. A voicemail cannot supply the basis for “Thanks for your reply.” Missing
names/specialties use neutral wording; no price, availability, result or booking
claim is invented.

The recipient always comes from the **prospect** and the selected channel must
have a recorded address/number. A linked client's contact does not silently take
its place. The latest meaningful response marked Not interested, or a prospect
marked Not a fit, blocks preparing/saving/copying a new message. This is a review
guard, not a cross-project preference or opt-out system; explicit stop/snooze and
communication preferences remain future work.

### Saved text and current context

Each draft retains its channel, purpose, selected contact, research and project
context at preparation. A one-hour signed preview binds creation to that exact
prospect and context. The final transaction repeats the check, saves the draft
and appends an audit row atomically. An operation key makes exact retries return
the same saved draft; changing its payload cannot replace an existing draft.

Saved edits use a signed, record-specific version so another tab cannot overwrite
newer work. Invalid or conflicting edits retain their submitted text. Context
changes (recipient/research, contact history, selected event, linked project/brief
or pending task) require a fresh draft; the older text remains accessible for
reference. Saved drafts paginate per prospect. Starting another draft opens a
new tab and does not replace the current editor.

The Copy control first checks the editor's version and current context through a
read-only endpoint, then copies the selected field, including unsaved edits. It
refuses a wrong-record response or text changed while the check was pending.
Clipboard failure leaves the text available for manual selection. Copied edits
still need Save to persist; navigation warns about unsaved text. Responses carry
`Cache-Control: no-store`; there is no browser-storage cache.

The check establishes freshness at the time it runs, not at a later manual send.
Review the recipient again in the email/text app. This slice does not send, invoke
Inbox/provider handlers, create a contact event, complete a task, change a stage
or update client activity. After sending outside Mise, use the existing explicit
contact-history form. A copy success is not a delivery receipt.

### Migration and recovery

`087_prospect_message_drafts.sql` adds only the draft table and its history index;
existing rows are not rewritten. The prospect foreign key preserves ownership.
Keep the additive table and audit history on application rollback; older code
ignores drafts. Existing follow-up/handoff rollback restrictions above still
apply. This schema change goes through a human-reviewed PR and a separate deploy.

### Draft verification (2026-11-11)

`tests/test_prospect_drafts.py` checks grounded wording, private-note exclusion,
contact ownership, missing channels, selected-contact eligibility, recipient and
context changes, retries, concurrent edits, audit rollback, migration preservation,
pagination, auth/origin checks, escaping and both UI modes on isolated SQLite.
`tests/js/prospect-drafts.test.mjs` exercises copying unsaved edits, wrong-record
responses, stale context, in-flight changes, tab/navigation changes, duplicate
clicks, expired login, unavailable clipboard and unsaved navigation.

Based on main `e9eb5c5`, using Python 3.12.14 and the locked dependencies:

| Check | Result |
| --- | --- |
| New draft integration cases | 57 passed |
| New copy/editor JavaScript cases | 10 passed |
| Full unit gate | 457 passed |
| Full integration gate | 1,249 passed |
| Full smoke gate | 188 passed |
| Full JavaScript test set (`tests/js/*.test.mjs`) | 60 passed |
| Combined integration/smoke app coverage | 89% (required floor: 78%) |
| Draft route module coverage | 99% |
| Ruff lint/format, migration numbering, whitespace | Passed |
| Bandit high-severity scan and repository secret scan | Passed |
| Locked runtime dependency audit | No known vulnerabilities found |

No selected tests were skipped; marker deselections select other gates. The
existing TestClient deprecation warning remains. Broad suites use disposable
workspace data directories above the storage reserve and their existing explicit
transport mocks; live provider behavior is outside this verification.

Desktop/mobile visual review remains open: the workspace denied Chromium's socket
creation. Rendered-form and JavaScript checks do not establish visual appearance.
No independent review, deployment, live admin write or message send was performed.

## Sixth slice: outreach holds and channel preferences

From a prospect, open **Manage this prospect’s outreach**. Record a reason and
choose no hold, pause through a date, or stop outbound outreach. Email, text and
phone exclusions remain independent of the hold; a preferred channel is advisory.
An excluded channel cannot be preferred on the same source record. Excluding all
three channels holds sales work even with status “No outreach hold.” The default
means no restriction has been recorded, never consent to contact.

A pause includes its end date on `studio._today()`. It expires by reading the
current day, without a worker or a write. The next day, pending tasks return with
their original due dates and may be overdue. Stops require an explicit edit and
reason to resume. A silent prospect is never automatically stopped or marked lost;
resuming does not erase a historical Not interested response or Not a fit status.

### Enforcement and history

Held prospects are excluded from Due, Upcoming and Needs next step, from Home’s
open tasks and task action count, and from the Tasks board’s active columns. The
**On hold** queue retains them; **All prospects** also shows their restrictions.
Filtering happens before counts and pagination. The Tasks board links retained
work to On hold. General/project delivery tasks and completed history remain.

Draft preview, initial save, edit and the Copy check re-read the effective hold
and selected channel. New follow-up scheduling also checks the hold inside its
write transaction. Existing task completion, cancellation and rescheduling, and
manual recording of contact that already happened, remain available. An exact
retry of an already saved command returns without adding another write or audit.

Any explicit preference change invalidates previously prepared drafts, including
a stop followed by a resume. Text stays available for reference. Drafts created
before this slice remain current when no preferences exist and other context is
unchanged. The freshness check describes the instant it runs, not a later manual
send. Nothing here sends, invokes a provider, creates contact history, changes
qualification/stage, completes tasks, or rewrites due dates.

### Explicit client scope

Before a handoff, controls apply to one prospect. After handoff, restrictions from
**all prospects explicitly linked to the same client** combine across that client’s
outbound sales work, including future projects. One stop wins; pauses use the latest
active end date; channel exclusions combine. Conflicting preferred channels require
review instead of choosing one automatically. Resuming one source never clears a
restriction from another source. Source links and reasons appear on prospect,
draft, client and project pages. Held clients leave the advisory Sales review queue;
pipeline membership, stages and financial values remain unchanged.

The handoff review shows the combined controls and explicitly explains the scope
change before linking. Its signed snapshot includes current preference versions
and linked source membership. Preference edit forms bind that same scope: a new
handoff or a change on another source after opening a form requires fresh review.
No email/business-name matching silently expands this scope, and client parent/
child relationships do not inherit controls. A client-wide hold is intentionally
coarser than person-level consent; the UI names this boundary.

These are **outbound prospect sales controls**, not a global do-not-contact system.
Inbox/provider sends, external apps, inbound inquiry queues and transactional client
service are outside enforcement. Review restrictions before using those surfaces.
No contact permissions, legal conclusions or external opt-out acknowledgements are
inferred from the stored controls.

### Persistence, scale and recovery

`088_prospect_outreach_preferences.sql` adds one record per prospect with validated
status/date/channel fields and a version. It does not rewrite existing records.
Each change and its before/after values, reason and reviewed scope commit with an
audit event in one transaction. Optimistic checks reject stale submissions; exact
most-recent retries are idempotent. GET is read-only and forms use no-store responses.

The shared projection reads the small solo CRM once per consumer, with no per-row
policy queries or persistent cache. Queue filtering and pagination happen in memory
after ordered reads. Revisit indexed SQL projections before bulk imports or large
team workloads; this slice is not a bulk-outreach engine.

Schema changes require Kevin’s review and a separate deploy. Keep the additive
table and audit history on rollback. **Older code ignores these controls:** if an
application rollback is needed, suspend outbound drafting/queue use until an
operator reviews the retained restrictions and restores enforcing code. Do not
interpret an older queue as permission to resume outreach.

### Preference verification (2026-11-11)

`tests/test_prospect_preferences.py` exercises stop/snooze/channel enforcement,
expiry boundaries, linked-source unions, future projects, scope changes during
handoff, stale drafts, exact retries, concurrent writes, audit rollback, unchanged
workflow/provider tables, migration constraints, pagination, escaping, auth/origin
checks and both UI modes on disposable SQLite without lifespan workers.

Based on main `31c303e`, using Python 3.12.14 and the locked dependencies:

| Check | Result |
| --- | --- |
| New preference integration cases (included below) | 40 passed |
| Full unit gate | 457 passed |
| Full integration gate | 1,289 passed |
| Full smoke gate | 188 passed |
| Full JavaScript test set (`tests/js/*.test.mjs`) | 60 passed |
| Combined integration/smoke app coverage | 89% (required floor: 78%) |
| Preference route / shared policy coverage | 99% / 100% |
| Ruff lint/format, migration numbering, whitespace | Passed (90 migration files) |
| Bandit high-severity scan and repository secret scan | Passed |
| Locked runtime dependency audit | No known vulnerabilities found |

No selected tests were skipped. Marker deselections select other gates. Broad
suites use disposable workspace data directories and their existing explicit
transport mocks; live provider behavior is outside this verification. The existing
Starlette TestClient deprecation warning remains.

Browser visual review remains open: this workspace previously denied Chromium
socket creation. Rendered-template checks do not establish desktop/mobile appearance.
No independent review, production writes, deployment or message sends are part of
this slice.

## Guided prospect review (2026-11-11)

The Due, Upcoming and Needs next step queues now offer “Review one at a time”
at `/admin/studio/prospects/review-work`. Review uses the same ordered, searched
and outreach-filtered projection as the list, without a first-page limit. It
shows one prospect, research notes, current outreach restrictions, next action,
manual contact forms and history. Message preparation opens separately.

Task/contact saves and validation errors preserve the chosen queue and search.
The existing command routes still enforce versions, operation-key retries,
transactional audit and scheduling holds. No new write semantics or schema are
introduced. Recording historical contact is still distinct from permission to
send; blocked channels and linked-source restrictions remain visible.

Navigation and counts are live, not a persisted session or completion metric.
Next wraps to the first eligible prospect, records nothing and does not complete
an action. A completed/rescheduled/held prospect that leaves the selected queue
shows an explicit state-change page with a link to the next eligible record.
GET requests never manufacture contact, completion, consent or progress.

The focused integration cases cover auth/no-store, read-only navigation, ordering,
holds and all-channel exclusion, changing eligibility, idempotent saves, stale
forms, preserved error inputs, search escaping and crossing list pagination.
They use isolated SQLite without lifespan workers. Existing full-suite provider
mocks remain; visual desktop/mobile review and production deployment are separate.

On Python 3.12.14 with the current hash locks, this change passed 496 unit,
1,299 integration (including 9 new review cases), 188 smoke and 60 JavaScript
tests. Ruff lint/format, Bandit, secrets, migration numbering and requirements
consistency checks passed. Each broad suite used fresh disposable workspace data.
Existing AnyIO/Starlette test deprecation warnings remain. Hosted checks must pass
before merge; no independent review or browser appearance verification is claimed.

### Returning from research and preference edits (2026-11-11)

Guided review now carries its queue, search and original prospect through research
edits and outreach-preference pages, including a linked source's controls. “Back
to guided review” returns without saving; successful explicit saves return to the
same live review. Validation errors, duplicate matches and stale preference forms
keep their submitted values and return context. A hold or qualification change
can make the original prospect ineligible; the existing state-change view explains
that outcome instead of silently selecting a different prospect.

Return handling accepts only the existing queue enum, a bounded search and an
optional positive numeric origin ID (at most 18 digits). It builds a fixed local
review route, never accepts an arbitrary return URL, and never uses the origin ID
as the write target. Invalid context falls back to each standalone page's existing
navigation. Preference scope tokens, optimistic checks, exact retries, audit and
outreach enforcement are unchanged. Drafts still open separately; project handoff
and other general navigation remain outside this return flow.

The additional integration cases cover research saves/errors/duplicate conflicts,
holds, preference retries and stale tokens, linked-source navigation versus write
identity, and malformed return IDs. They use disposable SQLite without lifespan
workers. PR #160's post-merge CI and CodeQL both passed before this slice began.

On Python 3.12.14 with unchanged hash locks, the final candidate passed 496 unit,
1,308 integration (9 additional cases), 188 smoke and 60 JavaScript tests, plus
Ruff lint/format, Bandit, secrets, migration numbering and lock consistency checks.
The existing safe-redirect test spy was updated to forward the optional request;
the complete integration gate then passed. No selected tests were skipped.
Existing provider mocks and AnyIO/Starlette warnings remain. Hosted CI/CodeQL must
pass before merge; no browser appearance verification or deployment is claimed.

### Keyboard navigation and return focus (2026-11-12)

The command palette and full index use native modal dialogs. Opening one moves
focus inside; Tab keeps page focus in the dialog (native browser controls remain
reachable), and Escape or its close button returns focus to the invoker. The full
index remains plain grouped navigation links.
Command search offers Daily follow-ups; arrow keys focus results, and Enter on
a result follows that actual link. Background controls stay inert while a dialog
is open. The command palette also retains the legacy theme.

Guided review focuses the current prospect heading (or the explicit empty/changed
state). Research and preference detours focus their form heading; returned server
errors focus the error text, with submitted values still intact. Successful saves
return to the live review heading. Focus does not log an action or change queue
eligibility. The script leaves an already active control alone, and scroll padding
accounts for the sticky header and mobile navigation.
The admin shell opts out of the shared cross-page fade so focused content is
revealed immediately; public pages keep their existing transition behavior.

Two additional browser journeys at desktop/mobile widths use real Tab, Shift+Tab,
Enter, Escape, arrow and typing events, without programmatically setting focus or
clicking through the workflow. They cover menu containment/return, the focused
command link, research edits, conflicting preferences and recovery, a manually
recorded contact, task completion and scheduling the next action. Focus targets
must be visible and unobscured. Existing pointer journeys and stale-form checks
remain, with accessibility reports for opened dialogs and keyboard error states.
The fixture uses synthetic prospects and disables workers/provider connections.
Execution stays on GitHub-hosted Chromium; screen-reader and other-browser review
remain separate. No schema, write semantics, deployment or live outreach changed.

## Remaining slices

1. Use the guided review and complete prospect → draft → manual contact →
   follow-up → opportunity loop to identify remaining friction. Queue counts
   are live obligations, not an outreach performance metric.
2. Consider richer drafting only after using the grounded starters. A provider
   integration needs explicit context boundaries and review; sending remains a
   separate action and must enforce communication preferences at send time.

Further persistence changes need a separately reviewed schema PR. Sending/provider
work, payment changes, contracts and deployment keep their existing review gates.
Do not infer that a manual send or an external call succeeded from a button click.

## Verification

`tests/test_studio_sales_review.py` covers eligibility, deterministic ordering,
visible truncation count, auth, escaping, read-only GET, and refreshed stage-move
fragments against disposable SQLite. The route test does not start background
jobs or senders. Full integration/smoke run in hosted CI. Browser appearance and
production deployment require separate verification.
