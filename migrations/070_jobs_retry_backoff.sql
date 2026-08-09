-- Retry backoff for the job queue: earliest wall-clock instant a queued job may
-- be claimed. NULL = eligible now (every existing row, and every newly enqueued
-- job). Set to datetime('now','+N seconds') when an attempt fails, so a transient
-- vendor outage no longer burns all MAX_ATTEMPTS inside one second.
-- No index: the claim reads by id, and the due-sweep already narrows on
-- idx_jobs_status before evaluating this column.

ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT;
