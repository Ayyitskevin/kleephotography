-- Rollback for 075_announcements.sql — DELIBERATELY NOT EXECUTABLE.
-- unsubscribes is a LEGAL ledger: dropping it re-subscribes every person who
-- opted out, which is a CAN-SPAM violation on the next send, not a cleanup.
-- announcements is the record of what was sent to whom-sized audiences.
-- Snapshot-restore doctrine applies — ops/MIGRATIONS.md.
SELECT 1;
