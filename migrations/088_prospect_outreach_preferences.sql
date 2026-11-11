-- Sales outreach controls, retained on the source prospect across client handoffs.
CREATE TABLE prospect_outreach_preferences (
    prospect_id INTEGER PRIMARY KEY REFERENCES prospects(id) ON DELETE RESTRICT,
    mode TEXT NOT NULL CHECK (mode IN ('active', 'snoozed', 'stopped')),
    snoozed_until TEXT,
    email_blocked INTEGER NOT NULL DEFAULT 0 CHECK (email_blocked IN (0, 1)),
    sms_blocked INTEGER NOT NULL DEFAULT 0 CHECK (sms_blocked IN (0, 1)),
    call_blocked INTEGER NOT NULL DEFAULT 0 CHECK (call_blocked IN (0, 1)),
    preferred_channel TEXT NOT NULL DEFAULT '' CHECK (preferred_channel IN ('', 'email', 'sms', 'call')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 1000),
    version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version >= 1),
    last_command_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((mode = 'snoozed' AND snoozed_until IS NOT NULL
            AND length(snoozed_until) = 10 AND date(snoozed_until, '+0 days') IS NOT NULL
            AND date(snoozed_until, '+0 days') = snoozed_until)
        OR (mode != 'snoozed' AND snoozed_until IS NULL)),
    CHECK (NOT (preferred_channel = 'email' AND email_blocked = 1)
       AND NOT (preferred_channel = 'sms' AND sms_blocked = 1)
       AND NOT (preferred_channel = 'call' AND call_blocked = 1))
);
