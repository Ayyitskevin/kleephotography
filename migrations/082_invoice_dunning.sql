-- Client-facing invoice dunning (enhancement brief R1): one-shot send tracker.
--
-- One row per dunning notice the sweep has SUCCESSFULLY emailed — the stamp is
-- written after the send (ops/AT-LEAST-ONCE.md), so a crash in the gap re-sends
-- once rather than silently dropping the chase. Stages escalate at due+3
-- (polite), due+7 (firmer) and due+14 (final); UNIQUE makes each one-shot.
--
-- Keyed by (invoice, stage, due_date), not just (invoice, stage): the stamp
-- records which due date it chased, so extending an invoice's due date
-- naturally re-arms the whole ladder for the new date without any admin-side
-- reset hook (compare galleries.reminded_expiry, which needs one).
--
-- Deliberately its OWN table rather than columns on invoices: this is
-- notification state, not money state. The dunning sweep's only writes are
-- INSERTs here — invoices and payments stay single-writer through the manual
-- send path and the Stripe webhook respectively.
CREATE TABLE invoice_dunning (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL CHECK (stage IN ('due3','due7','due14')),
    due_date    TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (invoice_id, stage, due_date)
);
