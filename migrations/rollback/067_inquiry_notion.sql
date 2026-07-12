-- Rollback for 067_inquiry_notion.sql. Pages already created in the Notion
-- Leads database are not deleted — they just stop being patched.
ALTER TABLE inquiries DROP COLUMN notion_page_id;
