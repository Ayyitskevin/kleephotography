-- Rollback for 077_sms_reminders.sql — no-op on purpose (additive flag column;
-- dropping it would forget who was already texted and re-send the T-24h SMS to
-- every booking still inside its window on the next sweep).
SELECT 1;
