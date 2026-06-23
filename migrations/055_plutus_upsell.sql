-- 055_plutus_upsell.sql — Plutus print upsell status on galleries (Phase 1).

ALTER TABLE galleries ADD COLUMN plutus_last_run_id INTEGER;
ALTER TABLE galleries ADD COLUMN plutus_last_status TEXT;
ALTER TABLE galleries ADD COLUMN plutus_last_error TEXT;
ALTER TABLE galleries ADD COLUMN plutus_last_at TEXT;