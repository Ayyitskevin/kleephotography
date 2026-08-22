-- Invoice Checkout invariants.
--
-- One invoice may legitimately collect a deposit and then a balance, but each
-- installment kind may succeed only once. The active Checkout snapshot binds a
-- webhook to the exact server-authorized session, kind, amount, and currency.
ALTER TABLE invoices ADD COLUMN stripe_checkout_amount_cents INTEGER;
ALTER TABLE invoices ADD COLUMN stripe_checkout_kind TEXT
  CHECK (stripe_checkout_kind IS NULL OR stripe_checkout_kind IN ('deposit','balance','full'));
ALTER TABLE invoices ADD COLUMN stripe_checkout_currency TEXT
  CHECK (stripe_checkout_currency IS NULL OR length(stripe_checkout_currency) = 3);

-- Preserve an in-flight most-recent session across the additive migration.
UPDATE invoices
   SET stripe_checkout_amount_cents =
         CASE
           WHEN status='deposit_paid' THEN total_cents - deposit_cents
           WHEN deposit_cents > 0 THEN deposit_cents
           ELSE total_cents
         END,
       stripe_checkout_kind =
         CASE
           WHEN status='deposit_paid' THEN 'balance'
           WHEN deposit_cents > 0 THEN 'deposit'
           ELSE 'full'
         END,
       stripe_checkout_currency = 'usd'
 WHERE stripe_session_id IS NOT NULL
   AND status != 'paid';

-- Fail the migration loudly if historic rows already violate either invariant;
-- deployment must reconcile them rather than silently discarding money records.
CREATE UNIQUE INDEX ux_payments_invoice_kind
    ON payments(invoice_id, kind);
CREATE UNIQUE INDEX ux_payments_stripe_session
    ON payments(stripe_session_id)
    WHERE stripe_session_id IS NOT NULL;
