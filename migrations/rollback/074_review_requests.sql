-- Rollback for 074_review_requests.sql — no-op on purpose (additive nullable
-- column; older code never selects it; dropping it would forget who was
-- already asked and re-ask every past client on the next sweep).
SELECT 1;
