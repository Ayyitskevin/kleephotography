-- Outbound research stays separate from inbound inquiries, clients and revenue.
-- Keys are normalized by app/admin/prospects.py; NULL means no contact channel.
CREATE TABLE prospects (
    id INTEGER PRIMARY KEY,
    business TEXT NOT NULL CHECK (length(trim(business)) BETWEEN 1 AND 200),
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    specialty TEXT NOT NULL DEFAULT '' CHECK (specialty IN ('', 're', 'pl', 'fb')),
    source TEXT NOT NULL CHECK (length(trim(source)) BETWEEN 1 AND 200),
    qualification TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (qualification IN ('unreviewed', 'potential_fit', 'not_fit')),
    notes TEXT NOT NULL DEFAULT '',
    business_key TEXT NOT NULL,
    contact_key TEXT NOT NULL,
    email_key TEXT UNIQUE,
    phone_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (business_key, contact_key)
);
