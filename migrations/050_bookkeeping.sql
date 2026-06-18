-- Own-business bookkeeping: expenses ledger, mileage log, receipt files.
-- Adapts the Admin Expenses / Mileage / Receipts prototypes, but everything here
-- is operator-entered REAL data. The prototypes' auto-import ("auto-detected from
-- your calendar"), AI auto-matching ("forward to receipts@…"), hardcoded 1099 watch,
-- and fabricated tax set-aside goals are dropped — Mise has no data source for them.
-- Deductible % and the IRS mileage rate are honest operator inputs, not guesses.
-- Receipts attach to an expense (expense_id NULL = unlinked); deleting an expense
-- leaves its receipt in the inbox rather than destroying the scan.

CREATE TABLE IF NOT EXISTS expenses (
    id             INTEGER PRIMARY KEY,
    spent_on       TEXT NOT NULL,                 -- ISO date the money went out
    vendor         TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'Other',
    amount_cents   INTEGER NOT NULL,
    deductible_pct INTEGER NOT NULL DEFAULT 100,  -- 0..100, operator-set (meals 50, etc.)
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(spent_on);

CREATE TABLE IF NOT EXISTS mileage (
    id          INTEGER PRIMARY KEY,
    drove_on    TEXT NOT NULL,                    -- ISO date
    from_place  TEXT NOT NULL,
    to_place    TEXT NOT NULL,
    purpose     TEXT,
    miles       REAL NOT NULL,
    rate_cents  INTEGER NOT NULL DEFAULT 70,      -- per-mile IRS rate, cents, frozen per trip
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mileage_date ON mileage(drove_on);

CREATE TABLE IF NOT EXISTS receipts (
    id           INTEGER PRIMARY KEY,
    filename     TEXT NOT NULL,                   -- original upload name (display)
    stored       TEXT NOT NULL,                   -- on-disk name in DATA_DIR/receipts
    content_type TEXT,
    size_bytes   INTEGER,
    expense_id   INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_receipts_expense ON receipts(expense_id);
