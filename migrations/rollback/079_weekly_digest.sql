-- Rollback for 079_weekly_digest.sql — no-op on purpose (dropping the ledger
-- would re-send past weeks' digests on the next sweep and lose the audience
-- baselines the growth numbers diff against).
SELECT 1;
