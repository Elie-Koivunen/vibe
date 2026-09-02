-- Manual bookmark ordering (spec: "row entries should be possible to manually
-- reorder them moving up/down"). Existing rows default to 0; BookmarkRepository
-- backfills real, distinct values (by current start_us order) the first time it
-- reads bookmarks lacking one, same one-time-backfill pattern as
-- rename_legacy_default_names().
ALTER TABLE bookmarks ADD COLUMN sort_index INTEGER NOT NULL DEFAULT 0;
