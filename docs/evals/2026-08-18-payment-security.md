# Payment security assumptions and verification — 2026-08-18

Status: implementation complete on isolated local branches; not approved to ship.

## Decision

Prevent one invoice installment or one booking from producing more than one
successful customer charge, even when requests race, Stripe delivers distinct
valid events, or an old Checkout Session completes after a replacement exists.

## Assumptions

1. Stripe idempotency prevents duplicate object creation only while the same key
   remains retained; Stripe documents that keys may be pruned after 24 hours.
2. Event-id uniqueness stops redelivery of one Event but not two separately
   completed Checkout Sessions.
3. A Checkout Session can be fulfilled safely only when its ID, amount, currency,
   entity, and installment kind match immutable server-side state.
4. SQLite uniqueness must be the final race-proof payment invariant; an
   application SELECT followed by INSERT is not sufficient under concurrency.
5. Invoice deposits and balances are separate legitimate installments, while
   `full`, `deposit`, or `balance` may succeed at most once per invoice.
6. A booking has exactly one legitimate successful payment.
7. Permanent stale/surplus events should be durably audited and acknowledged
   without applying state twice; operator refund/reconciliation remains manual.
8. Public health checks need only a truthful status code and `{"ok": bool}`;
   detailed disk, backup, and job telemetry is authenticated operational data.

## Current primary guidance

- Stripe idempotent requests: https://docs.stripe.com/api/idempotent_requests
- Stripe webhook duplicate/replay handling: https://docs.stripe.com/webhooks
- Stripe Checkout lifecycle and expiration: https://docs.stripe.com/payments/checkout/how-checkout-works
- Stripe limited-inventory expiration: https://docs.stripe.com/payments/checkout/managing-limited-inventory
- SQLite UPSERT and uniqueness: https://sqlite.org/lang_upsert.html
- SQLite constraint conflict behavior: https://www.sqlite.org/conflict.html
- Quo webhook signing and five-minute replay window:
  https://support.quo.com/core-concepts/integrations/webhooks

The mandatory DeepAPI research calls could not run because this node has neither
`DEEPAPI_API_BASE_URL` nor `DEEPAPI_API_KEY` configured. Official primary sources
and repository/live-state measurements are the documented fallback. This is a
shipping hold, not permission to silently skip the missing research gate.

## Measurement matrix

Evidence status for each case:

1. Two concurrent invoice checkout requests for the same installment.
2. Two distinct paid invoice sessions for the same installment.
3. Deposit followed by its legitimate balance.
4. Same Stripe Event delivered twice.
5. Two distinct Events for one Checkout Session.
6. Wrong invoice session ID with the correct amount.
7. Correct session ID with the wrong amount.
8. Correct amount in the wrong currency.
9. Stale invoice session after a replacement exists.
10. Two concurrent booking checkout requests.
11. Two distinct paid booking sessions.
12. Booking fee changed after Checkout creation.
13. Booking paid after its hold is released.
14. Webhook signature outside the allowed timestamp tolerance.
15. Public health request without credentials.
16. Detailed health request with correct, incorrect, and missing configuration.
17. Migration applied twice and against representative pre-existing payment rows.
18. Rollback/forward-recovery rehearsal against a disposable database copy.

Cases 2–9 and 11–16 are directly exercised by the focused payment, booking,
health, and SMS tests. Cases 1 and 10 are covered by repeat-request tests that
prove one Checkout Session and a deterministic Stripe idempotency key; a true
parallel HTTP load test remains a pre-deploy canary item. Case 17 is exercised
by the real-chain migration test (including representative historic payments)
and by repeated app startups across the full suite. Case 18 remains a deploy
runbook item because these additive, money-preserving migrations intentionally
use inert rollback scripts; recovery is restoring the pre-migration snapshot,
not deleting payment constraints in place.

## Observed local results

- 937 tests passed under a clean Python 3.12 environment installed exclusively
  from `requirements-dev.lock` with `--require-hashes`.
- The focused payment and booking tests passed (41 tests); Quo/SMS tests passed
  (52 tests, including stale, future, malformed, and boundary timestamps).
- Ruff check and format passed; Bandit reported no high-severity findings.
- `pip-audit -r requirements.lock` reported no known vulnerabilities.
- actionlint 1.7.12 and YAML parsing accepted both active workflows.
- `systemd-analyze verify` accepted `mise.service`; offline exposure is 2.8
  (`OK`). No live service was restarted under the new sandbox.
- The reviewed detect-secrets 1.5.0 baseline contains 13 known test/config
  placeholders; the baseline-aware hook passed all tracked and new source files.

## Release gate

Money/schema changes require an independent reviewer and Kevin's explicit merge
and deployment approval. Post-deploy verification must use Stripe test mode or a
synthetic flow and must never create an unplanned live customer charge.
