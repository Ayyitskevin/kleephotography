-- Rollback for 066_asset_renditions.sql. Rendition files on disk
-- (MEDIA_DIR/{gallery_id}/renditions/) are not tracked by the DB after this
-- and can be removed manually if space matters.
DROP INDEX IF EXISTS idx_asset_renditions_asset;
DROP TABLE IF EXISTS asset_renditions;
