-- Durable owner-email delivery + Notion create-race orphan tracking for leads.
-- No raw SMTP bodies, secrets, or visitor PII in the new columns — only state
-- machine fields and opaque Notion page ids for operator reconciliation.

ALTER TABLE inquiries ADD COLUMN owner_email_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inquiries ADD COLUMN owner_email_last_attempted_at TEXT;
-- Privacy-safe category only: smtp_error | mailer_not_configured | unknown
ALTER TABLE inquiries ADD COLUMN owner_email_failure_category TEXT;
ALTER TABLE inquiries ADD COLUMN owner_email_delivered_at TEXT;
-- Claim lock for concurrent workers: NULL | in_flight | failed
ALTER TABLE inquiries ADD COLUMN owner_email_status TEXT;

-- Create-race orphan: remote page created but not stamped (kept stamp wins).
ALTER TABLE inquiries ADD COLUMN notion_orphan_page_id TEXT;
ALTER TABLE inquiries ADD COLUMN notion_orphan_recorded_at TEXT;
-- open | relinked | dismissed
ALTER TABLE inquiries ADD COLUMN notion_orphan_status TEXT;
