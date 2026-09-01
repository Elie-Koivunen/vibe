-- Initial schema (spec #72-#77, #185).

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE playlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_uri TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,
    is_ad_hoc INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE playlist_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_playlist_signature ON playlist_signatures(signature);

CREATE TABLE media (
    id TEXT PRIMARY KEY,
    canonical_uri TEXT,
    filename TEXT,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_us INTEGER,
    file_size INTEGER,
    mtime_ns INTEGER,
    fast_fingerprint TEXT,
    full_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE media_uri_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
);

CREATE TABLE playlist_items (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    occurrence_index INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY (media_id) REFERENCES media(id)
);

CREATE TABLE lanes (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    name TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1,
    locked INTEGER NOT NULL DEFAULT 0,
    color_key TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
);

CREATE TABLE bookmarks (
    id TEXT PRIMARY KEY,
    playlist_id TEXT,
    media_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    lane_id TEXT,
    bookmark_type TEXT NOT NULL,
    name TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER,
    loop_enabled INTEGER NOT NULL DEFAULT 0,
    repeat_count INTEGER,
    loop_gap_ms INTEGER NOT NULL DEFAULT 0,
    completion_action TEXT NOT NULL DEFAULT 'continue',
    color_key TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id),
    FOREIGN KEY (media_id) REFERENCES media(id),
    FOREIGN KEY (lane_id) REFERENCES lanes(id)
);

CREATE TABLE bookmark_tags (
    bookmark_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (bookmark_id, tag),
    FOREIGN KEY (bookmark_id) REFERENCES bookmarks(id) ON DELETE CASCADE
);

CREATE TABLE settings_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE waveform_cache (
    cache_key TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    algorithm_version INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channel_mode TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE
);

CREATE TABLE recent_projects (
    path TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL
);

CREATE INDEX idx_bookmark_playlist_media ON bookmarks(playlist_id, media_id, start_us);
CREATE INDEX idx_bookmark_media ON bookmarks(media_id);
CREATE INDEX idx_media_fingerprint ON media(fast_fingerprint);
CREATE INDEX idx_media_uri ON media(canonical_uri);
CREATE INDEX idx_playlist_items ON playlist_items(playlist_id, ordinal);
