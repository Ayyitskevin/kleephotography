-- 044_form_fields_checkbox.sql
-- Add 'checkbox' (multi-select) to form_fields.ftype. SQLite can't ALTER a CHECK
-- constraint, so rebuild the table. Nothing else references form_fields (the FK
-- runs the other way, to forms), so this is self-contained. All rows + the
-- form_fields_form index are preserved.
CREATE TABLE form_fields_new (
    id          INTEGER PRIMARY KEY,
    form_id     INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    ftype       TEXT NOT NULL DEFAULT 'short_text'
                  CHECK (ftype IN ('short_text', 'long_text', 'dropdown',
                                   'checkbox', 'date', 'email', 'yesno')),
    required    INTEGER NOT NULL DEFAULT 0,
    options     TEXT,            -- JSON array of choices, dropdown/checkbox only
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO form_fields_new (id, form_id, label, ftype, required, options, sort_order, created_at)
    SELECT id, form_id, label, ftype, required, options, sort_order, created_at
    FROM form_fields;
DROP TABLE form_fields;
ALTER TABLE form_fields_new RENAME TO form_fields;
CREATE INDEX IF NOT EXISTS idx_form_fields_form ON form_fields(form_id, sort_order);
