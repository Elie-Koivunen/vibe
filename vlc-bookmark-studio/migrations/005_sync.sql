-- Folder sync between machines / Windows and WSL (0.3.0): deleted bookmarks leave a
-- tombstone so the deletion reaches the other side instead of the bookmark coming back.
-- Restoring a bookmark (undo) removes its tombstone.
CREATE TABLE bookmark_tombstones (
    bookmark_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
