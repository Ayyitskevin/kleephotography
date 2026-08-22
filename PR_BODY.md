# R3 — Notify the owner on proposal accept / decline

## What

Proposal accept and decline (`/p/{slug}/accept`, `/p/{slug}/decline` in
`app/public/docs.py`) each did one UPDATE and a `log.info`; the owner discovered a
yes by refreshing the board. This PR adds an owner notification on **both**
decisions — a Telegram line and an owner email — via the existing dormant-channel
patterns:

- **New `app/proposal_notify.py`** — `enqueue_decision()` stages a
  `proposal_decision_notify` job after the committed status UPDATE;
  `deliver_decision()` is the queue handler: `alerts.notify()` Telegram line
  (dormant unless `MISE_TELEGRAM_TOKEN` + `MISE_TELEGRAM_CHAT_ID`), then
  `mailer.send()` to `GMAIL_USER` with the client's address as Reply-To (skipped
  with a log line when the mailer is unconfigured). Message carries proposal
  title, client/company, total, and the `/admin/studio/proposals/{id}` deep link.
- **`app/jobs.py`** — registers the handler in `HANDLERS` (same lambda shape as
  `inquiry_owner_email`).
- **`app/public/docs.py`** — one `enqueue_decision()` call added to each of the
  accept and decline handlers, after the commit and the existing log line.
  Nothing else in the file is touched.

## Why

Highest-value notification in the funnel and it was silent (ENHANCEMENT-BRIEF §3
R3). Every benchmarked competitor pushes this event.

## Reliability shape (per `ops/AT-LEAST-ONCE.md`)

- **The client's action can never fail on a notify problem.** The enqueue runs
  after the committed UPDATE and is wrapped — any failure costs a log line, not a
  500. Delivery itself runs on a queue worker after the response is gone.
- **SMTP down** → the job parks behind the standard backoff and retries; a dead
  job surfaces in Admin → Jobs and the ops heartbeat like any other.
- **Channel unconfigured** → dormant: log line, job completes. Unlike
  `inquiry_notify` (where email is the only way a lead reaches Kevin) the
  decision is already durable on the board, so dormant-stays-dormant is not an
  error worth a parked retry.
- **Duplicate tolerance**: the Telegram line carries no send-stamp, so a retry
  after an SMTP failure can repeat it — the documented acceptable failure for an
  internal nudge; a claim lock like `inquiry_notify`'s was deliberately not
  copied ("do not copy it by default").

## Deliberately NOT done (brief hard constraints)

- **Funnel stage untouched**: projects still advance to `proposal_sent` on send
  (`app/admin/proposals.py:mark_proposal_sent`) — moving the advance to accept
  would make `proposal_sent` unreachable.
- **Contract-sign path untouched**: zero changes to `sign_contract` or anything
  below it in `docs.py`; a test pins that signing stages no job of this kind.
- No schema change, no new columns, no client-facing sends.

## Red classification

`app/public/docs.py` also carries the contract-sign path, so under `AGENTS.md`
("Contracts / legal — anything a client signs") the safe read puts this file
behind a PR even though the diff is small. Built on `claude/r3-proposal-decision-notify`;
Kevin merges.

## Test evidence

New `tests/test_proposal_decision_notify.py` (6 tests, `integration` marker):

- accept and decline each stage exactly one job with the right payload, and
  draining it sends the Telegram line (decision, title, total, admin URL) and the
  owner email (to `GMAIL_USER`, Reply-To client) — **fails without the change**;
- enqueue itself exploding still returns the client's 303 with the decision
  committed, and proves the notify was attempted — **fails without the change**;
- SMTP failure parks a retry (`queued` + `next_attempt_at`), decision untouched;
  recovered SMTP delivers on the retry — **fails without the change**;
- both channels unconfigured: job completes dormant, no send, HTMX response
  path covered — **fails without the change**;
- contract sign stages no `proposal_decision_notify` job (guards the
  zero-changes-to-sign-path constraint; passes before and after by design).

Verified fail-without-change: `git stash push -- app/` → 5 of 6 fail, restore →
all green.

Gates (from the worktree, repo venv, run serially):

```
python -m pytest tests/ --ignore=tests/smoke -m unit          # 287 passed
python -m pytest tests/ --ignore=tests/smoke -m integration   # 526 passed
MISE_DATA_DIR=$(mktemp -d) MISE_SECRET_KEY=test MISE_ADMIN_PASSWORD=pw \
  python -m pytest tests/ -m smoke                            # 187 passed
ruff check .                                                  # All checks passed
ruff format --check .                                         # 188 files already formatted
```

Adversarial (non-author) review verdict: **APPROVE_WITH_NOTES** — sign-path
guard proven non-inert by mutation (enqueue added to `sign_contract` fails the
guard test), all channel/ordering mutations caught after a review-driven test
was added pinning that the enqueue runs only after the decision row is durable.

Two trade-offs for the merger's eyes, both documented in the module docstring:
1. A process death between the decision's commit and the jobs INSERT loses the
   nudge with no log line (board still shows the decision) — same window the
   inquiry notify accepts; money paths use in-transaction `jobs.stage()` instead.
2. Pre-existing (not introduced here): the accept/decline UPDATEs carry no
   status predicate, so racing double-POSTs can stage two nudges. Suggested
   follow-up outside this PR: `AND status IN ('sent','viewed')` on the UPDATE.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
