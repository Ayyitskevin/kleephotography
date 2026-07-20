-- Forward-only note: SQLite cannot DROP COLUMN on older versions used in some
-- deploys. Rollback is a no-op marker; columns remain inert if unused.
SELECT 1;
