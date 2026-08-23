-- Rollback for 046_invoices_terms.sql — no-op on purpose.
--
-- Additive single column (invoices.terms). Code that predates it never selects
-- it, so leaving it in place costs nothing and the column simply goes inert —
-- the same reasoning as rollback/069.
--
-- DROP COLUMN is NOT the undo here: the column holds payment terms Kevin wrote
-- per invoice (deposit timing, refund policy), which is client-facing contract
-- language that exists nowhere else. Dropping it to "clean up" destroys it.
SELECT 1;
