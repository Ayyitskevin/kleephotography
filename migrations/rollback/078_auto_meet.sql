-- Rollback for 078_auto_meet.sql — no-op on purpose (additive columns; dropping
-- meet_url would strip join links out of already-confirmed video bookings while
-- their emails still point clients at the manage page to find one).
SELECT 1;
