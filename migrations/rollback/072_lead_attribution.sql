-- Rollback for 072_lead_attribution.sql — no-op on purpose.
-- Two additive nullable columns; code that predates them never selects them, so
-- they go inert (same reasoning as rollback/046 and /069). DROP COLUMN would
-- destroy attribution answers real visitors gave, for no gain.
SELECT 1;
