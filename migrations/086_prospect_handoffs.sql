-- An explicit, reviewed bridge. Research and contact events stay on the prospect.
-- Existing client/project/task rows are not rewritten by this migration.
CREATE UNIQUE INDEX idx_projects_client_identity ON projects(id, client_id);
CREATE TABLE prospect_handoffs (
    prospect_id INTEGER PRIMARY KEY REFERENCES prospects(id) ON DELETE RESTRICT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    project_id INTEGER NOT NULL,
    operation_key TEXT NOT NULL UNIQUE CHECK (length(operation_key) BETWEEN 16 AND 100),
    request_digest TEXT NOT NULL,
    prospect_snapshot TEXT NOT NULL,
    photography_need TEXT NOT NULL CHECK (length(trim(photography_need)) BETWEEN 1 AND 4000),
    specialty TEXT NOT NULL DEFAULT '' CHECK (specialty IN ('', 're', 'pl', 'fb')),
    target_date TEXT CHECK (target_date IS NULL OR (
        length(target_date) = 10
        AND target_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(target_date, '+0 days') IS NOT NULL
        AND date(target_date, '+0 days') = target_date
    )),
    task_id INTEGER,
    task_disposition TEXT NOT NULL CHECK (task_disposition IN ('none','keep','complete','cancel')),
    task_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((task_id IS NULL AND task_disposition='none') OR
        (task_id IS NOT NULL AND task_disposition IN ('keep','complete','cancel'))),
    CHECK (task_disposition NOT IN ('complete','cancel') OR length(trim(task_note)) BETWEEN 1 AND 1000),
    FOREIGN KEY (project_id, client_id) REFERENCES projects(id, client_id) ON DELETE RESTRICT,
    FOREIGN KEY (task_id, prospect_id) REFERENCES tasks(id, prospect_id) ON DELETE RESTRICT
);
CREATE INDEX idx_handoffs_client ON prospect_handoffs(client_id);
CREATE INDEX idx_handoffs_project ON prospect_handoffs(project_id);
