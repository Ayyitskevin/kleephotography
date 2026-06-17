-- Add an editable payment-terms / schedule note to invoices (client-facing).
-- Mirrors proposals.intro. NULL = no note shown. Kevin authors the wording per
-- invoice (deposit timing / refund policy = his/CPA call, not seeded here).
ALTER TABLE invoices ADD COLUMN terms TEXT;
