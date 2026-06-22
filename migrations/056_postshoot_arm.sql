-- One-shot flag so a finished shoot arms a single post-shoot "pull/cull/back-up the
-- cards" reminder via Hermes (postshoot_reminders), not one every sweep. Set only
-- after the arm push succeeds, so a Hermes hiccup leaves it 0 and the next sweep
-- retries (same posture as nudged_unsigned). A reschedule makes a fresh booking row
-- with the flag at its default, so the new shoot date arms on its own.
ALTER TABLE bookings ADD COLUMN armed_postshoot INTEGER NOT NULL DEFAULT 0;
