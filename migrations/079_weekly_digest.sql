-- Weekly owner digest ledger.
--
-- One row per covered week (its local Monday, ISO). The row is the idempotency
-- stamp — written only after the email actually went out, so a mail hiccup
-- retries on the next sweep — and audience_count is the baseline next week's
-- digest diffs against to report list growth without a snapshot table.
CREATE TABLE IF NOT EXISTS digest_log (
  week_start     TEXT PRIMARY KEY,
  sent_at        TEXT NOT NULL DEFAULT (datetime('now')),
  audience_count INTEGER
);
