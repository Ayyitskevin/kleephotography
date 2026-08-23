-- Rollback for 071_admin_session_token_hash.sql.
--
-- Restores the column name only. The hashes cannot be turned back into the
-- tokens they came from — that is the entire point — so any surviving row would
-- be unusable by the old code. They are deleted, which forces one more login.
-- Not money-touching; provided because reverting the app code without this
-- would leave every admin locked out by a column name.
DELETE FROM admin_sessions;
ALTER TABLE admin_sessions RENAME COLUMN token_hash TO token;
