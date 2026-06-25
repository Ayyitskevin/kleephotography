-- Platekit/Dionysus campaign pack hand-off from Argus vision (studio pipeline)
ALTER TABLE galleries ADD COLUMN platekit_last_job_id TEXT;
ALTER TABLE galleries ADD COLUMN platekit_last_pack_id INTEGER;
ALTER TABLE galleries ADD COLUMN platekit_last_status TEXT;
ALTER TABLE galleries ADD COLUMN platekit_last_error TEXT;
ALTER TABLE galleries ADD COLUMN platekit_last_at TEXT;