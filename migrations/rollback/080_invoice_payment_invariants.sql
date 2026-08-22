-- Rollback for 080_invoice_payment_invariants.sql — deliberately non-destructive.
--
-- The added invoice columns are additive and old code ignores them. The unique
-- indexes protect money records; dropping them during a rollback would reopen
-- the double-charge race. Revert application code only. If the migration itself
-- fails because historic duplicates exist, it rolls back and those rows must be
-- reconciled from Stripe before retrying.
SELECT 1;
