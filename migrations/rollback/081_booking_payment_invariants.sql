-- Rollback for 081_booking_payment_invariants.sql — intentionally inert.
-- The columns are additive and old code ignores them. The unique indexes protect
-- money history and should remain; recovery is a database snapshot restore.
SELECT 1;
