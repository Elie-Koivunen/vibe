# Changelog

All notable changes to VLC Bookmark Studio (bm4vlc). Previous versions are kept in
[`archive/`](archive/) and tagged in git (`bm4vlc-v<version>`).

## 0.2.0 — 2026-09-29

Bug-fix and portability release. Every bug below was first reproduced (a failing test
against 0.1.0), then fixed and covered by a regression test
(`tests/unit/test_regressions_020.py`, `tests/unit/test_platform_support.py`,
`tests/vlc/test_live_vlc.py`).

### Runs on Windows, Linux and WSL (Ubuntu)

- New `bookmark_studio/platform_support.py` holds every OS difference: data/log
  directories (`%LOCALAPPDATA%` on Windows, `$XDG_DATA_HOME` / `~/.local/share` on
  Linux), VLC and ffmpeg discovery (Windows registry and Program Files, Linux
  PATH/`/usr/bin`/snap/flatpak, WSL falling back to the Windows `vlc.exe`/`ffmpeg.exe`),
  VLC's per-user Lua/config directories, running-VLC detection (`tasklist` / `/proc`).
- WSL driving the Windows VLC: media paths are translated for `vlc.exe`/`ffmpeg.exe`
  (`/mnt/c/...` -> `C:\...`, Linux-side files -> `\\wsl.localhost\...`), VLC's
  `file:///C:/...` URIs map back to `/mnt/c/...`, and under WSL's default NAT networking
  VLC listens on the WSL virtual switch address (reachable from WSL and Windows only,
  not the LAN) instead of an unreachable 127.0.0.1.
- `.m3u` files: Windows code-page (cp1252) playlists no longer corrupt non-ASCII file
  names; backslash paths and absolute Windows paths work on Linux/WSL; other URL
  schemes (rtsp://, smb://...) pass through.
- File dialogs list upper-case extensions too (Linux name filters are case-sensitive).
- Depends on `PySide6-Essentials` (QtCore/Gui/Widgets only) instead of the full PySide6;
  `requires-python` lowered from 3.13 to 3.10 (tested on 3.10–3.13).
- `launch_bm4vlc.sh` for Linux/WSL; `launch_bm4vlc.bat` now also finds a project `.venv`.
- `.gitattributes` keeps shell scripts LF and batch files CRLF on every checkout.
- CI: `.github/workflows/bm4vlc-tests.yml` runs the suite on Windows and Ubuntu 22.04/24.04
  with Python 3.10/3.12/3.13, and checks that an installed wheel can create its database.

### Fixed

- **Loops with a Gap paused forever.** The loop announced the gap and waited to be
  resumed, but nothing listened. The loop controller now resumes by itself.
- **"After loop: Next/Previous Bookmark" did nothing.** Now plays the neighbouring
  bookmark of the same song with its own settings. "Next Segment Queue Item" (the queue
  isn't built yet) is no longer offered.
- **Every 2-second playlist poll disturbed the UI**: it rebuilt the bookmark list,
  which re-selected the row and re-announced it -- reverting half-typed Inspector text,
  jumping a previewed song back to the selected bookmark's song, wiping a painted
  selection and resetting column widths. Unchanged polls now do nothing at all.
- **Loop/Gap/Fade dropdowns in the bookmark list never opened** (rows weren't editable).
  Values that aren't presets (a 750 ms gap, ×4 repeats) now display correctly and
  are no longer reset to "Off" by merely opening and closing the dropdown.
- **Inspector Tags and Notes were never saved.** Now saved (notes when the field loses
  focus) and undoable.
- **Inspector Start field stayed disabled** after loading a bookmark until a selection
  had been made.
- **Undo/redo didn't refresh the screen**, and edits recorded stale "old" values, so undo
  skipped intermediate states. Spinbox steps now merge into one undo step; clicking a
  bookmark without moving it no longer adds an empty undo step.
- **Export ("Save Bookmarks...") only exported the song on screen.** It now exports the
  whole playlist: every song's bookmarks, their media, lanes, and the playlist's order
  and signatures. Format version 2 adds fade in/out and manual order (lost before);
  version 1 files still import.
- **Import wiped data**: `INSERT OR REPLACE` deleted rows first and the foreign-key
  cascades removed the playlist's signatures (so VLC stopped recognising it) and media
  aliases / waveform-cache rows. Import now merges, and maps media (by fingerprint) and
  playlists (by signature or source file) onto records you already have.
- **Playlists weren't recognised across sessions** once edited: playlist items were never
  stored, so adding one song made a new empty bookmark context. Items are stored now;
  an edited playlist is matched automatically (>=95% similar) or you're asked (75–95%).
  Launching from an .m3u remembers that file and names the playlist after it.
- **Seeking right after switching songs landed at the wrong time** (the previous song's
  length was used), and VLC's whole-second lengths skewed every seek by up to ~1 s at
  the end of a track. Seeks now use the target song's length -- the exact decoded one
  from the waveform when it agrees with VLC's -- and `goto_item` waits until VLC has
  actually switched.
- **Blocking network calls on the UI thread**: loop seeks, fade steps (every 40 ms) and
  one-off commands now go through one ordered command queue off the UI thread, so a
  slow VLC can't freeze the window and commands can't overtake each other.
- **A loop pass could be counted twice** when a status poll sampled just before a
  seek-back arrived just after it. Polls issued before the last seek completed are
  ignored.
- **Fades left VLC's volume turned down** after a finite loop or a Stop mid-fade, and
  the next poll then adopted the ducked level as the user's volume.
- The loop boundary timer ignored the playback rate.
- **"Launch a new VLC" could reuse a port VLC was already listening on** (the port probe
  used SO_REUSEADDR, which on Windows succeeds against VLC's own listener -- verified
  against VLC 3.0.23). Also skips ports Windows reserves for Hyper-V/WSL.
- Seeking/Prev-Next Bookmark while previewing another song acted on the playing song;
  the playhead showed the playing song's position on the previewed song's waveform.
  Seeks now switch VLC to the song on screen.
- A non-looping Play Bookmark didn't stop an active loop, which later seeked back.
- ffmpeg could deadlock on damaged files (stderr never drained); each decoded waveform
  stayed in memory for the whole session; preloading read every cached waveform from
  disk on the UI thread; up to one ffmpeg per CPU core ran at once (now 2, the song on
  screen first).
- A file replaced at the same path kept showing the old waveform (fingerprint never
  re-checked).
- `Application.stop()` was never called: a VLC the app launched kept running after
  quitting, and worker threads were abandoned.
- Installed as a package (non-editable), the app found no migrations and started with
  an empty database; a failing migration could leave a half-applied schema.
- VLC's `-1` "not parsed yet" durations, `currentplid -1`, and the Media Library node in
  playlist.xml are handled.
- Playlist > Refresh (F5) did nothing.
- Lua bridge (opt-in), all found by the new live tests against VLC 3.0.23:
  - the script let Lua's garbage collector free its HTTP handler objects, whose
    finalizers unregistered the URLs: VLC answered **404 on random routes**, more often
    the longer a session ran. The handlers are now kept referenced (as VLC's own
    http.lua does);
  - "pause" toggled (resumed an already-paused item); it now only pauses;
  - added the `volume` command (fades and mute failed), status reports volume, and
    "nothing loaded" is a normal status instead of an error (was shown as Offline);
  - the token falls back to VLC's `--http-password` (read with `vlc.var.inherit`; VLC 3
    refuses every request on a handler with an empty password);
  - `goto_item` waits until VLC has switched; `launch_managed_vlc_with_lua_bridge()`
    takes the interface name.

### Known limitations

- Lua bridge (opt-in, not used by default): VLC's `httpd:handler()` still never closes
  a connection, so the client keeps one persistent connection (see README).
- A stop issued in VLC's own window (not this app) doesn't stop an active loop.
- The silent test audio output (`--aout=adummy`) has no volume control, so live tests
  check the volume commands sent rather than VLC's readback.

## 0.1.0

MVP: domain model, SQLite persistence, playlist recognition, FFmpeg waveforms, VLC
adapters, loop controller, undo/redo, project export/import, PySide6 UI. Archived in
[`archive/v0.1.0/`](archive/v0.1.0/).
