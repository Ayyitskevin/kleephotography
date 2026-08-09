-- Rollback for 017. Manual/emergency only (not globbed by db.migrate).
-- Verified against the exact 017 schema on sqlite 3.45.1 and
-- 3.46.1: the naive DROP COLUMN succeeds because parent_id's
-- constraints are all column-level/self-contained (its own self-FK + own
-- CHECK) and the lone index is dropped first; data and inbound FKs survive.
-- Does NOT delete the schema_migrations row (consistent with prior rollbacks).
DROP INDEX IF EXISTS idx_clients_parent;
ALTER TABLE clients DROP COLUMN parent_id;
