-- Rollback for 076_anniversary_nudges.sql — no-op on purpose (additive nullable
-- column; dropping it would forget who was nudged and re-ping Kevin about every
-- eligible client on the next sweep).
SELECT 1;
