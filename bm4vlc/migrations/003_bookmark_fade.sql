-- Fade in/out during bookmark loop playback (direct user request: "add options to
-- fade in and fade out when playing back"). 0 disables, same "0 means off"
-- convention as loop_gap_ms.
ALTER TABLE bookmarks ADD COLUMN fade_in_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bookmarks ADD COLUMN fade_out_ms INTEGER NOT NULL DEFAULT 0;
