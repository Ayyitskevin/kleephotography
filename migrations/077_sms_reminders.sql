-- T-24h SMS booking reminder (Cal.com-parity work, item 1).
--
-- Its own flag rather than reusing reminded_24h: email and SMS succeed or fail
-- independently, and a booking whose email went out while Quo was still dormant
-- should still get the text once SMS is armed (the sweep never stamps a channel
-- it did not actually send on).
ALTER TABLE bookings ADD COLUMN reminded_sms INTEGER NOT NULL DEFAULT 0;
