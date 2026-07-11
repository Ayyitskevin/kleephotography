-- Flagship revamp R3: per-aspect video deliverables. /services has promised
-- "Delivered 9:16 + 1:1" since the videography tiers shipped, but the pipeline
-- renders exactly one web MP4 per video — this table models the missing
-- social-cut renditions, built on demand from the camera original (the web
-- proxy is already downscaled). Files land at
-- MEDIA_DIR/{gallery_id}/renditions/{stem}_{preset}.mp4.
--
-- Additive only — NO ALTER on existing tables. Rollback in
-- migrations/rollback/066_asset_renditions.sql.
--
-- status mirrors the assets.status vocabulary (pending/ready/failed) so admin
-- chips and job logic read identically; UNIQUE(asset_id, preset) makes the
-- admin "build social cuts" action idempotent (INSERT OR IGNORE re-runs
-- without duplicating rows). CASCADE: renditions die with their video, same
-- rule as video_comments (026).
CREATE TABLE IF NOT EXISTS asset_renditions (
  id          INTEGER PRIMARY KEY,
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  preset      TEXT NOT NULL CHECK (preset IN ('9x16', '1x1')),
  stored      TEXT NOT NULL,
  width       INTEGER,
  height      INTEGER,
  bytes       INTEGER,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'ready', 'failed')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (asset_id, preset)
);

CREATE INDEX IF NOT EXISTS idx_asset_renditions_asset
  ON asset_renditions (asset_id);
