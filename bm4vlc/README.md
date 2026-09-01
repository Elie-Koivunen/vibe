# VLC Bookmark Studio (bm4vlc)

A playlist-aware visual bookmarking, segment-selection, navigation and
looping system for VLC Media Player. See [PROJECT_SPEC.md](PROJECT_SPEC.md)
for the full design and engineering specification (198 sections).

## Status

MVP core is implemented and tested (106 tests, all passing): domain
model, SQLite persistence + migrations, media fingerprint/resolution,
playlist recognition/similarity/mutation-tracking, the full FFmpeg
waveform pipeline (real decode/peaks/pyramid/cache, verified against
real `ffmpeg`), three playback adapters (Mock / VLC's built-in HTTP /
the custom Lua bridge — the last verified live against a real running
VLC 3.0.23), the software loop controller, undo/redo commands with
drag-compression, project export/import (atomic, transactional), and a
working PySide6 UI (waveform paint-to-select/drag/resize, playlist and
bookmark panels, inspector, transport bar) wired end-to-end through a
live polling `Application` composition root. `python -m bookmark_studio`
starts, discovers VLC, connects or falls back to offline mode, and runs.

**Known simplifications / not yet built:**
- `StandardHttpPlaybackAdapter` (spec #28) exists and is tested, but
  `bootstrap.py` doesn't wire it in as a fallback yet — an unreachable
  enhanced bridge currently falls back to an inert `MockPlaybackAdapter`
  rather than VLC's plain built-in HTTP interface.
- `PlaylistSynchronizer`'s similarity scoring against *other* known
  playlists always sees an empty candidate list (`_list_ordered_media_ids_for_playlist`
  in `app/application.py` is a documented stub) — ad-hoc context
  creation and mutation-of-the-active-playlist both work correctly;
  only cross-playlist re-recognition after a VLC restart is simplified.
  Bridge tokens are stored via `QSettings` (Windows registry), not
  Windows Credential Manager as spec #121 suggests as the ideal.
- Bookmark lanes, tags, and delete have repository/domain support and
  are unit-tested, but the UI doesn't yet expose lane assignment,
  tag editing beyond the Inspector's tags field, or a delete action/
  context menu.
- P1/P2 features (Segment Queue, Loop Trainer, clip export, Audacity
  label import/export, beat detection, video thumbnails) are out of
  scope for this pass — see PROJECT_SPEC.md #175-176.

## Verified live against real VLC

`vlc/bookmarkstudio.lua` was iteratively debugged against a real,
locally installed VLC 3.0.23 (not just written and assumed correct).
Three real bugs were caught this way and are documented inline in the
script and in `app/vlc_launcher.py`:

1. `obj:method and obj:method()` is invalid Lua (colon-call syntax
   needs immediate parens) — rejected at script-load time.
2. `vlc.getenv(...)` does not exist in VLC's Lua API. Config now comes
   from a file at `vlc.config.configdir()` (verified live: resolves to
   `%APPDATA%\vlc`, i.e. exactly the "VLC user config" directory spec
   #153 refers to).
3. `vlc.httpd()` does not let a script pick its own host/port — it
   binds according to VLC's own `--http-host`/`--http-port` startup
   flags (default, unset: **all interfaces**, port 8080 — confirmed
   live). `app/vlc_launcher.py`'s `launch_managed_vlc()` always passes
   `--http-host=127.0.0.1` explicitly for this reason (spec #20).

The full HTTP round-trip through a VLC instance launched with those
exact flags was attempted but not conclusively confirmed in this
session (VLC's own log output went silent under that specific flag
combination for reasons not fully diagnosed) — flagged here rather
than claimed, per this project's own convention (see
[buzz2vlc](../buzz2vlc)'s PROJECT_SPEC.md for the origin of that
convention). The Lua bridge's endpoint logic itself, and the Python
`BridgeClient`/`EnhancedLuaPlaybackAdapter` pair, are separately
verified against a real HTTP server in `tests/unit/test_playback_adapters.py`.

## Architecture

```text
PySide6 UI  +  Domain Logic  +  SQLite
                    |
             Playback Adapter
              /            \
   Enhanced Lua Bridge   VLC HTTP fallback
              \            /
               VLC Media Player
```

VLC performs playback; Python performs the product logic. Full rationale
in PROJECT_SPEC.md sections 2, 18-30, 196.

## Layout

```text
src/bookmark_studio/   application package (see PROJECT_SPEC.md #116)
vlc/bookmarkstudio.lua thin VLC Lua HTTP bridge (spec #18-#27)
migrations/            SQLite schema migrations (spec #126)
tests/                 unit / integration / ui / vlc / fixtures
```

## Setup

This machine's system Python (3.10) doesn't have PySide6 installable
in-place due to a Windows long-path limit hit by PySide6's wheel; a
venv at a short path (e.g. `C:\v`) sidesteps it:

```bash
python -m venv C:\v
C:\v\Scripts\pip install -e ".[dev]"
C:\v\Scripts\pytest
```

## Non-goals

Not a DAW, not a sample-accurate audio editor, not a replacement media
player. Original media is never modified. See spec #177.
