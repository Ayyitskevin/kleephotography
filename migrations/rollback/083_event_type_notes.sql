-- Rollback for 083_event_type_notes.sql — no-op on purpose (additive columns;
-- same reasoning as rollback/072 and /078).
--
-- Code that predates the slots never selects them, so they go inert and
-- reverting the app is the whole rollback. DROP COLUMN would delete the notes
-- Kevin wrote, which exist nowhere else. A deliberate full removal, after
-- copying any note text out, is:
--   ALTER TABLE event_types DROP COLUMN prep_note;
--   ALTER TABLE event_types DROP COLUMN confirm_note;
SELECT 1;
