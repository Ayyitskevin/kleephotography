-- Drafts are preparation, never contact events or delivery receipts.
CREATE TABLE prospect_message_drafts (
    id INTEGER PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE RESTRICT,
    operation_key TEXT NOT NULL UNIQUE CHECK (length(operation_key) BETWEEN 16 AND 100),
    request_digest TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
    purpose TEXT NOT NULL CHECK (purpose IN ('introduction', 'follow_up', 'conversation')),
    subject TEXT NOT NULL DEFAULT '' CHECK (length(subject) <= 240),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 4000),
    context_json TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((channel = 'email' AND length(trim(subject)) > 0)
        OR (channel = 'sms' AND subject = '' AND length(body) <= 1600))
);
CREATE INDEX idx_prospect_drafts_history ON prospect_message_drafts(prospect_id, id);
