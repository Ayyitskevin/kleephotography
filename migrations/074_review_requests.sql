-- Google review engine (revenue roadmap item 2).
--
-- A timestamp, not a flag: the ask is one-shot per gallery (IS NULL = unsent),
-- and the per-CLIENT cooldown reads MAX(review_requested_at) across a client's
-- galleries — a family with three galleries this year gets asked once, not
-- three times. Asking is throttled per person because review fatigue is how a
-- happy client becomes an annoyed one.
ALTER TABLE galleries ADD COLUMN review_requested_at TEXT;
