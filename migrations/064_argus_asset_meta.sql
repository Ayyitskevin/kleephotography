-- Argus vision writeback: per-asset alt text / scores + gallery hero summary.

ALTER TABLE galleries ADD COLUMN argus_hero_asset_ids TEXT;
ALTER TABLE galleries ADD COLUMN argus_analyzed_count INTEGER;

ALTER TABLE assets ADD COLUMN argus_alt_text TEXT;
ALTER TABLE assets ADD COLUMN argus_keywords TEXT;
ALTER TABLE assets ADD COLUMN argus_keeper_score REAL;
ALTER TABLE assets ADD COLUMN argus_hero_potential REAL;