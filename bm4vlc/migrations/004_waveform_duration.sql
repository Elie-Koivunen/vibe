-- Exact decoded media length (from the waveform), recorded with each cached waveform so
-- the app knows a track's precise duration without loading its waveform. VLC's own HTTP
-- interface only reports whole seconds, which skewed every position<->time conversion.
ALTER TABLE waveform_cache ADD COLUMN duration_us INTEGER;
