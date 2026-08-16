-- Auto Google Meet links for virtual event types (Cal.com-parity work, item 2).
--
-- auto_meet is the per-event-type opt-in (discovery calls yes, shoots no); the
-- minted link lives on the booking row because each booking gets its own
-- conference. Nullable meet_url: NULL means "no link" — either the type doesn't
-- want one or the calendar sync hasn't produced one yet.
ALTER TABLE event_types ADD COLUMN auto_meet INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bookings ADD COLUMN meet_url TEXT;
