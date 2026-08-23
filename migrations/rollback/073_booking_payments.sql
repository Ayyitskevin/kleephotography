-- Rollback for 073_booking_payments.sql — DELIBERATELY NOT EXECUTABLE.
-- booking_payments holds money records; dropping it deletes the evidence of
-- what clients paid to reserve sessions (see rollback/002 for the doctrine).
-- The added columns are additive and go inert under older code. The real
-- rollback for a money migration is a snapshot restore — ops/MIGRATIONS.md.
SELECT 1;
