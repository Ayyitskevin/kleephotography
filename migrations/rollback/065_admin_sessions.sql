-- Rollback for 065_admin_sessions.sql. Additive migration, so the undo is a
-- clean drop of the single new table (all admin sessions are revoked as a result;
-- admins simply re-log-in). Not auto-applied — db.migrate only runs migrations/*.sql.
DROP TABLE IF EXISTS admin_sessions;
