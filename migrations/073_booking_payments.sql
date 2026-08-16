-- Pay-to-book (revenue roadmap item 1): a booking slot can require a Stripe
-- payment before it is confirmed.
--
-- event_types.booking_fee_cents = 0 keeps today's behaviour exactly (book →
-- confirmed, no money involved). A fee turns POST /book/{slug} into a HOLD:
-- the row is created status='pending_payment' — a status the slot engine counts
-- as occupying the slot, so two visitors cannot pay for the same Saturday —
-- and flips to 'confirmed' only when Stripe's webhook lands. Holds that never
-- pay are expired by the scheduler sweep and release the slot.
--
-- booking_payments mirrors payments: stripe_event_id UNIQUE is the webhook
-- idempotency key, so a retried delivery rolls back its whole transaction
-- (same doctrine as app/public/pay.py). Money rows CASCADE with their booking
-- deliberately NOT — RESTRICT is unavailable mid-ALTER and losing a payment
-- record because a booking row was pruned would be worse than an orphan, so
-- no FK action beyond the reference itself.
ALTER TABLE event_types ADD COLUMN booking_fee_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bookings ADD COLUMN paid_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bookings ADD COLUMN paid_at TEXT;
ALTER TABLE bookings ADD COLUMN stripe_session_id TEXT;

CREATE TABLE IF NOT EXISTS booking_payments (
  id                INTEGER PRIMARY KEY,
  booking_id        INTEGER NOT NULL REFERENCES bookings(id),
  stripe_event_id   TEXT UNIQUE,
  stripe_session_id TEXT,
  amount_cents      INTEGER NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_booking_payments_booking ON booking_payments(booking_id);
