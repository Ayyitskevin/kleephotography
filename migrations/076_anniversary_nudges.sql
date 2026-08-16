-- Session-anniversary nudges (revenue roadmap item 5).
--
-- Per CLIENT, not per project: the question is "is it time to invite this
-- person back", and a client with three past projects is still one invitation.
-- A timestamp rather than a flag so the cycle repeats — once the stamp itself
-- ages out, next year's sweep may nudge again.
ALTER TABLE clients ADD COLUMN anniversary_nudged_at TEXT;
