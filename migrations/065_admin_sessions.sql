-- Server-side admin session tokens.
--
-- The admin cookie used to carry a signed constant ("admin"): is_admin accepted
-- any cookie that unsigned to that string, so logout only cleared the cookie in
-- one browser and a leaked cookie stayed valid for the full SESSION_MAX_AGE (90d)
-- with no way to revoke it short of rotating MISE_SECRET_KEY — which would also
-- invalidate every client gallery/portal/workspace cookie.
--
-- Now each login mints a random token stored here; the cookie carries that token,
-- is_admin checks the row exists, logout deletes it (real revocation), and a
-- "sign out everywhere" can DELETE all rows. Additive + forward-only: this creates
-- one new table and touches nothing existing, so it cannot affect any current data.
CREATE TABLE IF NOT EXISTS admin_sessions (
  token      TEXT PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
