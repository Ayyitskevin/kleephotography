-- Pay-to-book Checkout invariants.
--
-- A booking's quoted fee is immutable once its hold exists. The authoritative
-- Checkout snapshot lets the webhook reject stale sessions and amount/currency
-- drift, while the unique indexes ensure one successful payment per booking.
ALTER TABLE bookings ADD COLUMN stripe_checkout_amount_cents INTEGER;
ALTER TABLE bookings ADD COLUMN stripe_checkout_currency TEXT
  CHECK (stripe_checkout_currency IS NULL OR length(stripe_checkout_currency) = 3);

-- Preserve an in-flight hold created by migration 073-era code.
UPDATE bookings
   SET stripe_checkout_amount_cents = (
         SELECT booking_fee_cents FROM event_types
          WHERE event_types.id = bookings.event_type_id
       ),
       stripe_checkout_currency = 'usd'
 WHERE stripe_session_id IS NOT NULL
   AND status = 'pending_payment';

-- Fail loudly on historic duplicates so they can be reconciled before release.
CREATE UNIQUE INDEX ux_booking_payments_booking
    ON booking_payments(booking_id);
CREATE UNIQUE INDEX ux_booking_payments_stripe_session
    ON booking_payments(stripe_session_id)
    WHERE stripe_session_id IS NOT NULL;
