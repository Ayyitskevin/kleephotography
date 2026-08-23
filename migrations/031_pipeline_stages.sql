-- Pipeline redesign: 7→8 sales-funnel stages, payment-gated.
-- lead→inquiry_received · proposal→proposal_sent · contract→contract_signed
-- invoice→retainer_paid (ONLY if a deposit/full payment actually landed;
--   an unpaid sent invoice falls back to contract_signed — payment is the gate)
-- shooting→session_planning · delivered→project_closed · archived→archived
-- consultation_call is net-new (reached manually). Changing a CHECK constraint
-- requires a table rebuild; child tables FK-reference projects(id), so we disable
-- FK enforcement around the swap. ids are preserved → referential integrity holds.
-- (db.migrate runs a file's leading and trailing PRAGMAs outside the migration
--  transaction, so this toggle actually takes effect — SQLite silently ignores
--  PRAGMA foreign_keys inside a transaction.)
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE projects_new (
    id              INTEGER PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'inquiry_received'
                    CHECK (status IN ('inquiry_received','consultation_call',
                                      'proposal_sent','contract_signed','retainer_paid',
                                      'session_planning','project_closed','archived')),
    gallery_id      INTEGER REFERENCES galleries(id) ON DELETE SET NULL,
    notion_page_id  TEXT,
    notes           TEXT,
    shoot_date      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO projects_new
       (id, client_id, title, status, gallery_id, notion_page_id, notes, shoot_date, created_at)
  SELECT id, client_id, title,
    CASE status
      WHEN 'lead'      THEN 'inquiry_received'
      WHEN 'proposal'  THEN 'proposal_sent'
      WHEN 'contract'  THEN 'contract_signed'
      WHEN 'invoice'   THEN CASE WHEN EXISTS (
                                   SELECT 1 FROM invoices i
                                   WHERE i.project_id = projects.id
                                     AND i.status IN ('deposit_paid','paid'))
                              THEN 'retainer_paid' ELSE 'contract_signed' END
      WHEN 'shooting'  THEN 'session_planning'
      WHEN 'delivered' THEN 'project_closed'
      WHEN 'archived'  THEN 'archived'
      ELSE 'inquiry_received'
    END,
    gallery_id, notion_page_id, notes, shoot_date, created_at
  FROM projects;
DROP TABLE projects;
ALTER TABLE projects_new RENAME TO projects;
CREATE INDEX IF NOT EXISTS idx_projects_client ON projects(client_id);
COMMIT;
PRAGMA foreign_keys=ON;
