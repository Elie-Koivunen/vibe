# Changelog

All notable changes to VLC Bookmark Studio (bm4vlc). Previous versions are kept in
[`archive/`](archive/) and tagged in git (`bm4vlc-v<version>`).

## 0.3.1 — 2026-09-30

Release-pipeline fixes: the first version with published packages. 0.3.0 was tagged,
but its Linux package failed its own self-test in CI, so no release was published.

### Fixed

- **Linux package: the in-app player could not start.** PyInstaller bundled the build
  machine's `libvlc.so.5`/`libvlccore.so.9` (found through python-vlc's ctypes calls).
  VLC looks for its plugins next to wherever libvlccore was loaded from, found none in
  the bundle and refused to create an instance. libVLC is no longer bundled (Linux
  uses the system VLC, Windows ships a complete `vlc/` folder), and the build fails
  if one slips in.
- **A libVLC that can't be loaded no longer closes the app.** python-vlc calls
  `sys.exit()` during import when the library it is pointed at fails to load (and
  raises `NotImplementedError` on Linux); the loader loads the library itself first
  and reports these as "in-app player unavailable".
- Errors about libVLC refusing to start now name the libvlccore that was loaded and
  the plugin path in use.
- CI: the Linux `mypy` job failed on Windows-only registry code (now behind a
  `sys.platform` check, in one place); mypy runs for both Linux and Windows.

### CI

- A failing release build posts the end of its output as an annotation, readable
  without signing in; changes to `packaging/` on main run a build-only release.
- `actions/checkout@v5`, `actions/setup-python@v6` (Node 24).

## 0.3.0 — 2026-09-29

Features and portability release. Tested on Windows 11 (Python 3.12, VLC 3.0.23 32-bit
and 64-bit), WSL Ubuntu 24.04 (Python 3.10–3.13, Ubuntu's VLC 3.0.20 and the Windows
VLC through interop), and as packaged builds.

### In-app player (libVLC)

- New playback mode: **Play inside this app**. libVLC (through `python-vlc`) plays in
  the app's own process: no VLC window, no port or password, millisecond-exact times
  and lengths (the HTTP interface only reports whole seconds), a 10 Hz playhead.
  Offered in the open-media dialog when libVLC is available; the last choice is
  remembered.
- libVLC discovery (`playback/libvlc_loader.py`): `--libvlc-dir`, `BM4VLC_LIBVLC_DIR`,
  the libVLC bundled with a packaged build, the installed VLC (Windows registry and
  Program Files), the system library (Linux). On Windows the DLL's bitness is read
  from its PE header: a 32-bit VLC next to 64-bit Python gets a clear explanation
  instead of "not a valid Win32 application".

### Packaged builds and releases

- `packaging/`: PyInstaller build of a portable **Windows zip** that bundles a 64-bit
  VLC (in-app player and VLC window) and ffmpeg, and a **Linux AppImage / tar.gz**
  that uses the system's VLC and ffmpeg. `bm4vlc-portable.cmd`/`.sh` keep all data
  next to the program. Every build runs its own self-test before it is archived.
- `.github/workflows/bm4vlc-release.yml` builds both on every `bm4vlc-v*` tag
  (downloads checksum-verified), self-tests them and publishes a GitHub Release.

### Command line

- `bookmark-studio [MEDIA ... | --playlist FILE] [--adapter auto|http|libvlc]
  [--attach [HOST:]PORT] [--port N] [--vlc PATH] [--libvlc-dir DIR] [--ffmpeg PATH]
  [--data-dir DIR] [--sync-dir DIR | --no-sync] [--no-dialog] [--log-level L]
  [--self-test [--require ...] [--self-test-report FILE]] [--version]`.
- `--self-test` checks Qt, the database, sync, ffmpeg, VLC and libVLC (it plays a
  generated tone) and prints a report; used by CI and the packaged builds.
- `--data-dir` (portable mode) keeps the database, waveforms, logs and settings in
  one folder.

### Sync between Windows, WSL and other PCs

- `--sync-dir DIR` (remembered): each installation writes its database to its own
  file in the folder and merges the others' at startup, every minute, on
  File > Sync Now and on exit. The most recent change of each bookmark wins;
  deletions travel as tombstones (migration `005_sync.sql`), and an undo of a
  delete wins over the delete. Songs are matched by content fingerprint and
  playlists by their song order, so Windows (`C:\...`) and WSL (`/mnt/c/...`)
  agree. Unchanged databases are not rewritten (no churn in cloud folders).
- Import: playlists are also matched by their song order re-hashed over the local
  media ids (a playlist from another machine is merged instead of duplicated).
  File > Import Project keeps its meaning: the archive's version wins.

### Changed

- **The HTTP password is no longer on VLC's command line** (where any local process
  could read it): a managed VLC starts with a private config file (owner-only), which
  also switches off VLC's first-run privacy dialog.
- **A loop stops when playback is changed in VLC's own window** (pause, stop or
  another song) instead of resuming playback moments later. Two fresh samples are
  needed, outside a grace period after the loop's own seeks.
- **Waveforms are decoded as a stream**: memory no longer grows with the length of
  the file (was ~115 MB per hour of audio, twice), and a long file's waveform is
  shown while it decodes, about once a second.
- Code health: `Application` split into `PlaybackSession` (adapter, polling, command
  queue) and `PlaylistContext` (playlist recognition); `MainWindow` has a public API
  instead of Application reaching into its widgets; history-style comments rewritten
  as present-tense reasons; `ruff` and `mypy --strict` (Qt widget code: relaxed
  annotations) pass and run in CI.
- CI also runs the live tests against a real VLC (Windows and Ubuntu) and lints.
- Version bumped to 0.3.0; `python-vlc` is now a dependency.

### Fixed

- **The Lua bridge could not load in Linux VLC builds**: `goto` is a reserved word
  in Lua 5.2. Found by the first live test against Ubuntu's VLC.
- A packaged Linux build no longer hands its bundled libraries to the system's
  `vlc`/`ffmpeg` it starts (`LD_LIBRARY_PATH` is restored for child processes).
- The in-app player reports the volume it was set to (libVLC forgets a volume set
  before its audio output exists, and some outputs always report 0, which would
  have made fades treat the user's volume as silent).
- Launch/attach failures are shown in a message instead of escaping a Qt slot.

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
