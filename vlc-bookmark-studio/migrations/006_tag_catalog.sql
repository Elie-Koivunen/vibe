-- Tag catalog (0.7.0): the tags offered in the Bookmark tab's drop list, edited in the
-- "Edit tags" window. Renaming or removing a tag rewrites every bookmark that has it,
-- and tag_changes remembers what happened, so a stale name that turns up later (undo,
-- folder sync from another machine, an older project file) is mapped onto the current
-- name, or dropped if the tag was removed, instead of coming back.
CREATE TABLE tag_catalog (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    sort_index INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE tag_changes (
    old_name TEXT PRIMARY KEY COLLATE NOCASE,
    new_name TEXT,  -- NULL: the tag was removed
    changed_at TEXT NOT NULL
);

-- Ready to use: a starting set, then every tag bookmarks already carry.
INSERT INTO tag_catalog (name, sort_index, updated_at) VALUES
    ('intro', 0, '1970-01-01T00:00:00+00:00'),
    ('build-up', 1, '1970-01-01T00:00:00+00:00'),
    ('drop', 2, '1970-01-01T00:00:00+00:00'),
    ('peak', 3, '1970-01-01T00:00:00+00:00'),
    ('breakdown', 4, '1970-01-01T00:00:00+00:00'),
    ('outro', 5, '1970-01-01T00:00:00+00:00'),
    ('game start', 6, '1970-01-01T00:00:00+00:00'),
    ('game end', 7, '1970-01-01T00:00:00+00:00');

INSERT OR IGNORE INTO tag_catalog (name, sort_index, updated_at)
    SELECT tag, 100, '1970-01-01T00:00:00+00:00' FROM bookmark_tags GROUP BY tag COLLATE NOCASE;
