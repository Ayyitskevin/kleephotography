-- List announcements (revenue roadmap item 4).
--
-- unsubscribes is the CAN-SPAM ledger: one row per opted-out address, checked
-- at audience build AND again at send time. Emails are stored lowercased so a
-- Client@ and client@ can never diverge into "one of them kept getting mail".
--
-- announcements is the paper trail: what went out, to how many. Recipients are
-- not stored per-row — the audience is recomputed at send and each delivery is
-- a durable job, so the queue (Admin -> Jobs) is the per-recipient record.
CREATE TABLE IF NOT EXISTS unsubscribes (
  id         INTEGER PRIMARY KEY,
  email      TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS announcements (
  id             INTEGER PRIMARY KEY,
  subject        TEXT NOT NULL,
  body           TEXT NOT NULL,
  audience_count INTEGER NOT NULL DEFAULT 0,
  sent_count     INTEGER NOT NULL DEFAULT 0,
  skipped_count  INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
