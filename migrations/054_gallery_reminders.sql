-- Per-gallery client-reminder send tracking. The recurring sweeper sends two
-- one-shot client nudges off gallery_reminders.sweep: an expiry warning as the
-- gallery nears expires_at, and a proofing nudge when selections are still due.
-- These flags make each send idempotent so the loop can fire as often as it likes
-- and a gallery gets at most one of each. reminded_expiry is reset to 0 when the
-- gallery's expiry date is changed (see admin.galleries.update_gallery) so an
-- extended gallery re-reminds near its new date.
ALTER TABLE galleries ADD COLUMN reminded_expiry INTEGER NOT NULL DEFAULT 0;
ALTER TABLE galleries ADD COLUMN reminded_proofing INTEGER NOT NULL DEFAULT 0;
