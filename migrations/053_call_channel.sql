-- Calls in the unified inbox — widen messages.channel to accept 'call' alongside
-- 'sms' and 'email'. An inbound/outbound/missed call from Quo lands as a message
-- row in the same thread the texts use, so the Inbox shows the full conversation
-- (texts + calls + voicemail transcripts) against one contact.
--
-- SQLite can't ALTER a CHECK constraint, so rebuild the table. `messages` is a
-- child-only table (it references inquiries; nothing references it), so the drop
-- is safe and the one-row copy is trivial. Preserves the UNIQUE idempotency key
-- and the thread index. No money/legal state.

CREATE TABLE messages_new (
    id              INTEGER PRIMARY KEY,
    inquiry_id      INTEGER NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    channel         TEXT NOT NULL CHECK (channel IN ('sms', 'email', 'call')),
    body            TEXT NOT NULL DEFAULT '',
    provider_msg_id TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO messages_new (id, inquiry_id, direction, channel, body, provider_msg_id, created_at)
    SELECT id, inquiry_id, direction, channel, body, provider_msg_id, created_at FROM messages;

DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;

CREATE INDEX IF NOT EXISTS idx_messages_inquiry ON messages(inquiry_id, created_at);
