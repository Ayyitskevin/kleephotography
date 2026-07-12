-- One-way Notion "Leads" mirror: stamp the created Notion page id on the
-- inquiry so a later status change (convert/dismiss/undo) patches the same
-- page instead of creating a duplicate. NULL = never mirrored (sync dormant
-- when the row was created, or the row predates 067).
ALTER TABLE inquiries ADD COLUMN notion_page_id TEXT;
