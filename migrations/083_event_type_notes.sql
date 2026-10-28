-- Per-event-type content slots for the automated touches (R15).
--
-- confirm_note is appended to the client's confirmation email when set;
-- prep_note is stored for a later reminder and read by no sender yet. Both are
-- plain text Kevin writes in the event-type admin form. They are NOT
-- event_types.description, which is public (the /book card and the invite).
--
-- Default '' and no seeded text: every existing row reads empty, and an empty
-- slot adds nothing, so behaviour is identical until a note is written.
ALTER TABLE event_types ADD COLUMN confirm_note TEXT NOT NULL DEFAULT '';
ALTER TABLE event_types ADD COLUMN prep_note TEXT NOT NULL DEFAULT '';
