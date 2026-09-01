# VLC Bookmark Studio (bm4vlc)

A playlist-aware visual bookmarking, segment-selection, navigation and
looping system for VLC Media Player. See [PROJECT_SPEC.md](PROJECT_SPEC.md)
for the full design and engineering specification (198 sections).

## Status

MVP core is implemented and tested (108 tests, all passing): domain
model, SQLite persistence + migrations, media fingerprint/resolution,
playlist recognition/similarity/mutation-tracking, the full FFmpeg
waveform pipeline (real decode/peaks/pyramid/cache, verified against
real `ffmpeg`), three playback adapters (Mock / VLC's built-in HTTP /
the custom Lua bridge), the software loop controller, undo/redo
commands with drag-compression, project export/import (atomic,
transactional), and a working PySide6 UI (waveform paint-to-select/
drag/resize, playlist and bookmark panels, inspector, transport bar)
wired end-to-end through a live polling `Application` composition
root. `python -m bookmark_studio` starts, discovers VLC, connects or
falls back to offline mode, and runs.

**The full live loop was confirmed working end-to-end against a real,
locally installed, running VLC 3.0.23**: launch a managed VLC instance,
connect over the Lua bridge, resolve the current track, load its
waveform via real `ffmpeg`, and stream live playback position back
into the UI's transport bar and playhead every ~150ms — not mocked,
not simulated. See "Verified live against real VLC" below for the six
real bugs this surfaced and fixed, several of which directly
contradicted the spec's own assumptions about VLC's Lua API.

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
Six real bugs were caught this way, several directly contradicting
spec #196's own claims about VLC's Lua API:

1. `obj:method and obj:method()` is invalid Lua (colon-call syntax
   needs immediate parens) — rejected at script-load time.
2. `vlc.getenv(...)` does not exist in VLC's Lua API. Config comes from
   a file at `vlc.config.configdir()` instead (verified live: resolves
   to `%APPDATA%\vlc`, exactly spec #153's "VLC user config" directory).
3. `vlc.httpd()` does not let a script pick its own host/port — it
   binds according to VLC's own `--http-host`/`--http-port` startup
   flags (default, unset: **all interfaces**, port 8080 — confirmed
   live). `app/vlc_launcher.py`'s `launch_managed_vlc()` always passes
   `--http-host=127.0.0.1` explicitly for this reason (spec #20).
4. **`vlc.httpd():handler()`'s response is not RFC 7230 compliant** —
   it sends a bare status line straight into the body with no
   header-terminating blank line, no `Content-Length`, and never
   closes the connection. Every standard HTTP client (`requests`,
   `curl`, browsers) hangs forever waiting for headers that never
   arrive, even though the correct body is delivered in milliseconds.
   `playback/bridge_client.py` reads over a raw socket with an
   idle-gap heuristic instead of relying on a header terminator or
   connection close, which handles both this and a normal
   RFC-compliant server (VLC's built-in HTTP interface, and every test
   fixture, use the latter).
5. `vlc.player.seek_by_time_absolute` (spec #196's own reference)
   **does not exist** in VLC 3.0.23's Lua API at all (`vlc.player` is
   `nil`) — every `/seek` request hung with no response. Seeking is
   instead `vlc.var.set(input, "time", us)`, the same mechanism already
   used to *read* time/length/rate.
6. `vlc.playlist.get("playlist", false)` has no `.current` field —
   there is no built-in "which item is playing" indicator. The bridge
   now matches the currently-playing input item's URI against each
   playlist child's `.path` instead.

Also found and fixed while closing this out: `Application`'s status/
playlist polling called the adapter's blocking network I/O directly
from a `QTimer` callback on the Qt main thread — a slow or stalled
bridge response froze the entire GUI for the call's timeout window,
contradicting spec #108's "no network calls on a UI-blocking thread."
Polling now dispatches through a `QThreadPool` worker with an in-flight
guard per poll kind, mirroring the pattern already used for waveform
generation.

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
