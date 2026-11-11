-- Prospect follow-ups share the task list, but never masquerade as projects.
-- Existing task rows remain unchanged. Application rollback leaves this history.
ALTER TABLE tasks ADD COLUMN prospect_id INTEGER REFERENCES prospects(id) ON DELETE RESTRICT
    CHECK (prospect_id IS NULL OR (
        project_id IS NULL AND length(trim(title)) BETWEEN 1 AND 200
        AND due_date IS NOT NULL AND length(due_date) = 10
        AND due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(due_date, '+0 days') IS NOT NULL AND date(due_date, '+0 days') = due_date
        AND done IN (0, 1)
        AND ((done = 0 AND done_at IS NULL) OR (done = 1 AND done_at IS NOT NULL))
    ));
ALTER TABLE tasks ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN canceled_at TEXT
    CHECK (canceled_at IS NULL OR (
        prospect_id IS NOT NULL AND done = 0 AND done_at IS NULL
        AND length(trim(cancel_reason)) BETWEEN 1 AND 1000
    ));
ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1
    CHECK (typeof(version) = 'integer' AND version >= 1);
ALTER TABLE tasks ADD COLUMN completion_note TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN schedule_key TEXT;
ALTER TABLE tasks ADD COLUMN schedule_digest TEXT;
ALTER TABLE tasks ADD COLUMN last_command_digest TEXT;

CREATE UNIQUE INDEX idx_tasks_prospect_open ON tasks(prospect_id)
    WHERE prospect_id IS NOT NULL AND done = 0 AND canceled_at IS NULL;
CREATE UNIQUE INDEX idx_tasks_schedule_key ON tasks(schedule_key) WHERE schedule_key IS NOT NULL;
CREATE UNIQUE INDEX idx_tasks_prospect_identity ON tasks(id, prospect_id);
CREATE INDEX idx_tasks_prospect_history ON tasks(prospect_id, id);

-- Operator-entered history, not a provider delivery receipt or an Inbox message.
-- A request digest binds retries to the original contact and optional task command.
CREATE TABLE prospect_contact_events (
    id INTEGER PRIMARY KEY,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE RESTRICT,
    operation_key TEXT NOT NULL UNIQUE CHECK (length(operation_key) BETWEEN 16 AND 100),
    request_digest TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'sms', 'call', 'in_person', 'other')),
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    outcome TEXT NOT NULL CHECK (outcome IN (
        'attempted', 'no_answer', 'voicemail', 'sent', 'conversation',
        'reply_received', 'not_interested', 'other'
    )),
    occurred_on TEXT NOT NULL CHECK (
        length(occurred_on) = 10
        AND occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(occurred_on, '+0 days') IS NOT NULL
        AND date(occurred_on, '+0 days') = occurred_on
    ),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 4000),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    provenance TEXT NOT NULL DEFAULT 'manual' CHECK (provenance = 'manual'),
    task_id INTEGER,
    task_action TEXT NOT NULL DEFAULT 'keep' CHECK (task_action IN ('keep', 'complete', 'reschedule')),
    CHECK ((task_action = 'keep' AND task_id IS NULL)
        OR (task_action IN ('complete', 'reschedule') AND task_id IS NOT NULL)),
    CHECK (direction != 'in' OR outcome NOT IN ('attempted', 'no_answer', 'voicemail', 'sent')),
    CHECK (direction != 'out' OR outcome != 'reply_received'),
    CHECK (outcome NOT IN ('no_answer', 'voicemail') OR channel = 'call'),
    CHECK (outcome != 'sent' OR channel IN ('email', 'sms')),
    FOREIGN KEY (task_id, prospect_id) REFERENCES tasks(id, prospect_id) ON DELETE RESTRICT
);
CREATE INDEX idx_prospect_contact_history ON prospect_contact_events(prospect_id, occurred_on, id);
