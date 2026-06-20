-- Two-way messaging for the Inbox — the prototype's SMS+Email messenger, made
-- real. The conversation thread hangs off an inquiry (the inbox's existing
-- spine), so an inbound text from an unknown number auto-creates a kind='sms'
-- inquiry and reuses every convert action (quote / client / dismiss) unchanged.
--
-- Ships INERT: with no SMS provider configured (sms.configured() false) nothing
-- writes here from the SMS side; email replies keep flowing through emails_log as
-- before. No money/legal state.

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    inquiry_id      INTEGER NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    channel         TEXT NOT NULL CHECK (channel IN ('sms', 'email')),
    body            TEXT NOT NULL DEFAULT '',
    -- provider's message id; UNIQUE so a retried inbound webhook is idempotent.
    -- NULL for legacy/manual rows (SQLite lets multiple NULLs coexist in UNIQUE).
    provider_msg_id TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_inquiry ON messages(inquiry_id, created_at);

-- Phone is how an inbound SMS is matched back to a thread. Web-form inquiries
-- have always been email-keyed; this adds the SMS key alongside (default '').
ALTER TABLE inquiries ADD COLUMN phone TEXT NOT NULL DEFAULT '';
