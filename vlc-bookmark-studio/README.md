# VLC Bookmark Studio

<img src="src/bookmark_studio/resources/icon.svg" alt="VLC Bookmark Studio logo" width="96" align="right">

A playlist-aware visual bookmarking, segment-selection, navigation and
looping system for VLC Media Player. See [PROJECT_SPEC.md](PROJECT_SPEC.md)
for the full design and engineering specification (198 sections), and
[CHANGELOG.md](CHANGELOG.md) for what changed in each version.

Runs on **Windows**, **Linux**, and **WSL (Ubuntu on Windows)**.

## Status

Version 0.6.0. The core is implemented and tested: domain model, SQLite
persistence + migrations, media fingerprint/resolution, playlist
recognition across sessions (similarity scoring over stored playlist
order), the FFmpeg waveform pipeline (streamed, shown while it decodes), four
playback adapters (Mock / VLC's built-in HTTP / the custom Lua bridge / the
in-app libVLC player), the software loop controller (gaps, fades, completion
actions, stopping when playback is changed in VLC's own window), undo/redo,
project export/import (format v2, merging), folder sync between machines, and a
PySide6 UI wired end-to-end through a live polling `Application`. All playback
commands run on one ordered queue off the UI thread.

Tests: 360+ unit/integration tests (offscreen Qt, mock VLC) plus opt-in live
tests against a real VLC and libVLC (`tests/vlc/`). Verified on Windows 11
(Python 3.12; VLC 3.0.23 32-bit over HTTP and 64-bit in-app) and WSL Ubuntu
24.04 (Python 3.10–3.13; Ubuntu's VLC 3.0.20, and the Windows VLC through
interop). `ruff` and `mypy --strict` pass on the whole package.

**Not built yet** (see PROJECT_SPEC.md #175-176): Segment Queue, Loop
Trainer, clip export, Audacity label import/export, beat detection, video
thumbnails, lane assignment in the UI.

## Packaged builds (no Python needed)

Each release on GitHub (tag `vlc-bookmark-studio-v<version>`) has:

- **Windows:** `vlc-bookmark-studio-<version>-windows-x64.zip`. Unzip anywhere and
  run `VLCBookmarkStudio.exe`. It contains its own 64-bit VLC (in-app player *and* a
  VLC window) and ffmpeg, so nothing else has to be installed.
  `VLCBookmarkStudio-portable.cmd` keeps the database, settings and logs in a `data`
  folder next to it (e.g. on a USB stick); `VLCBookmarkStudio-cli.exe` is the same
  program with a console, for `--help` and `--self-test`.
- **Linux:** `vlc-bookmark-studio-<version>-x86_64.AppImage` (one file: `chmod +x`,
  run) or the `.tar.gz` folder. They use the system's VLC and ffmpeg
  (`sudo apt install vlc ffmpeg`).

`vlc-bookmark-studio --self-test` (Windows: `VLCBookmarkStudio-cli.exe --self-test`)
checks Qt, the database, sync, ffmpeg, VLC and libVLC and prints what it found.

Up to 0.3.1 the project was called **bm4vlc**; those releases and tags
(`bm4vlc-v<version>`) keep that name. Your data and settings carry over, and the
old `BM4VLC_*` environment variables still work.

## Setup (from source)

You need Python 3.10+, VLC 3.x and (for waveforms) ffmpeg. The app finds VLC
and ffmpeg in their usual places; a path can also be saved in the settings
(`vlc/path`, `ffmpeg/path`) or given on the command line (`--vlc`, `--ffmpeg`).

### Windows

1. Install [VLC](https://www.videolan.org/), [Python](https://www.python.org/)
   (3.10 or newer) and ffmpeg (e.g. `winget install Gyan.FFmpeg`, or unpack it
   to `C:\Program Files\ffmpeg`).
2. In the `vlc-bookmark-studio` folder:

   ```bat
   py -3 -m venv .venv
   .venv\Scripts\pip install -e .
   ```

3. Double-click `launch_vlc_bookmark_studio.bat`.

The in-app player needs a **64-bit** VLC (Python is 64-bit, and a 64-bit
process can't load the 32-bit `libvlc.dll` from `C:\Program Files (x86)`). The
64-bit VLC installs next to a 32-bit one; or point `--libvlc-dir` at an unpacked
`vlc-3.x.y-win64.zip`. With only a 32-bit VLC, the app says so and keeps using a
separate VLC window.

PySide6 installs deep folder trees. If pip fails with *"The filename or
extension is too long"* (Windows' 260-character path limit), put the venv on
a short path instead (`py -3 -m venv C:\v` then `C:\v\Scripts\pip install -e .`;
`launch_vlc_bookmark_studio.bat` also looks there) or enable Windows long paths.

### Linux (Ubuntu/Debian)

```bash
sudo apt install vlc ffmpeg python3-venv libegl1 libxkbcommon0 libxcb-cursor0
python3 -m venv .venv
.venv/bin/pip install -e .
./launch_vlc_bookmark_studio.sh
```

### WSL (Ubuntu on Windows 11)

The app runs as a Linux program inside WSL and shows its window through WSLg.

- **Simplest:** install VLC inside WSL (`sudo apt install vlc ffmpeg`) and
  follow the Linux steps. Audio plays through WSLg; paths and networking are
  native.
- **Using the Windows VLC** (no VLC installed in WSL): the app finds
  `C:\Program Files\VideoLAN\VLC\vlc.exe` automatically, translates file paths
  both ways (`/mnt/c/...` <-> `C:\...`, WSL files via `\\wsl.localhost\...`),
  and, under WSL's default NAT networking, has VLC listen on the WSL virtual
  switch address (reachable from WSL and Windows only). Windows Firewall must
  allow VLC (VLC's installer normally adds that rule; otherwise Windows asks the
  first time). With `networkingMode=mirrored` in `.wslconfig`, plain
  127.0.0.1 is used.

Data lives in `%LOCALAPPDATA%\VLCBookmarkStudio` on Windows and
`~/.local/share/VLCBookmarkStudio` on Linux/WSL (database, waveform cache,
logs), or wherever `--data-dir` says. A WSL session and a Windows session have
separate databases; keep them in step with a sync folder (below).

## Playing: a VLC window or inside the app

**Launch VLC / Open Media...** (Ctrl+O) offers:

- **Play inside this app**: libVLC plays in the app's own process. No VLC
  window, no network port or password, millisecond-exact times and lengths,
  a smoother playhead. Needs libVLC (see Windows above; Linux: `apt install vlc`).
- **Launch a new VLC window**: a VLC driven over its HTTP interface, as before.
  The HTTP password is kept in a private VLC config file (owner-only), not on
  VLC's command line where other local processes could read it.
- **Attach** to a VLC this app started earlier.

If you pause, stop or change the song in VLC's own window while a bookmark is
looping, the loop stops instead of resuming playback.

### The window

- **Above the waveform:** the playback buttons (previous bookmark, previous track,
  stop, play/pause, next track, next bookmark) centred, the volume on the left and the
  position and song length on the right. Seeking by 5 seconds is on the Left/Right
  arrow keys (Playback menu).
- **Beside the waveform:** View (Zoom −, Zoom +, Fit), Bookmark (Bookmark now,
  Bookmark selection) and Selection (Play selection, Clear selection).
- **Below the waveform:** the selection -- start, end and length -- while you drag
  one out ([ and ] set its start and end at the playhead).
- **Beside the bookmark list, two tabs:** **Bookmark** holds the selected bookmark's
  settings (name, times, loop, fades, tags, notes); **Volume & EQ** the player's volume
  and equalizer. Click the volume readout above the waveform to jump to it. The open
  tab, the window size and the panel sizes come back on the next start.

**Volume:** the VOL fader works like a DJ mixer's channel fader (0–125 %, the amber
mark is 100 %): drag it, click where it should go, scroll, or use the arrow keys (Page
Up/Down: 10 %); a double-click returns to 100 %. It follows the player's volume,
including fades. Playing or looping a bookmark raises a quieter (or muted) player to
85 %; a louder setting is kept.

**Equalizer:** VLC's 10-band equalizer with a preamp and VLC's 18 presets (Flat, Rock,
Club, Dance, ...), ±20 dB per band. Tick *Equalizer* to switch it on; while it is off
the faders are greyed out. Pick a preset or drag the faders (a double-click returns a
fader to neutral: 0 dB for a band, +12 dB for the preamp, which is VLC's neutral
level). The settings belong to the player, not to a bookmark: they are remembered and
applied to whichever player you use -- the in-app player or a VLC window (through its
HTTP interface; they stay across song changes). The band frequencies follow the player:
60 Hz ... 16 kHz in a VLC window, 31 Hz ... 16 kHz in the in-app player. A switched-off
equalizer leaves a VLC window's own equalizer setting alone.

**Quit** (the button next to Launch VLC…, File > Quit or Ctrl+Q) saves whatever is
still being typed in the Inspector, the window layout and a final sync file, closes the
VLC this app launched (one you attached to keeps running), and exits. Bookmarks
themselves are saved the moment you change them. Closing the window does the same.

## Command line

```text
vlc-bookmark-studio [MEDIA ... | --playlist FILE.m3u] [options]

  --adapter {auto,http,libvlc}  VLC window over HTTP, or the in-app player
                                (auto: the last choice)
  --attach [HOST:]PORT          attach to a running VLC's HTTP interface
  --port PORT                   first HTTP port to try for a launched VLC
  --vlc PATH | --ffmpeg PATH    use these executables (this run only)
  --libvlc-dir DIR              VLC folder whose libVLC the in-app player uses
  --data-dir DIR                database, waveforms, logs and settings in DIR
  --sync-dir DIR                sync bookmarks through DIR (remembered); --no-sync
  --no-dialog                   don't show the open-media dialog at startup
  --log-level LEVEL             DEBUG, INFO, WARNING, ERROR
  --self-test [--require vlc,libvlc,ffmpeg] [--self-test-report FILE]
  --version
```

## Sync between Windows, WSL and other PCs

Start each installation once with the same folder, e.g. from Windows
`--sync-dir C:\Users\me\Bookmarks` and from WSL
`--sync-dir /mnt/c/Users/me/Bookmarks` (a Dropbox/OneDrive/Syncthing folder or a
network share works too). The folder is remembered.

Each installation writes its whole database to its own file
(`vlc-bookmark-studio-sync-<machine>.vlcbmk`; files named `bm4vlc-sync-...` by
0.3.x are read too) and merges the others' at startup, every
minute, on **File > Sync Now** and on exit. For each bookmark the most recent
change wins, and deletions reach the other side. A song is recognised by its
content, so `C:\Music\a.mp3` and `/mnt/c/Music/a.mp3` are the same song, and a
playlist by its song order. Lanes and playlist orders are only added, never
overwritten, by another machine.

## Tests

```bash
# Linux / WSL
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
# Windows (PowerShell)
$env:QT_QPA_PLATFORM="offscreen"; .venv\Scripts\python -m pytest
```

Live tests start a real, silent, window-less VLC (`-I dummy --aout=adummy`) and
a silent in-app player (set `VLC_BOOKMARK_STUDIO_LIBVLC_DIR` to a VLC folder if libVLC isn't
found by itself):

```bash
VLC_BOOKMARK_STUDIO_LIVE_VLC=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/vlc
```

Lint and types: `ruff check src tests packaging` and `mypy` (configured in
`pyproject.toml`).

CI (`.github/workflows/vlc-bookmark-studio-tests.yml` at the repository root) runs the suite
on Windows and Ubuntu 22.04/24.04 with Python 3.10, 3.12 and 3.13, the live
tests against a real VLC on Windows and Ubuntu, and ruff + mypy.
`vlc-bookmark-studio-release.yml` builds the packages on every `vlc-bookmark-studio-v*` tag, runs their
self-test and publishes them as a GitHub Release.

### Building the packages yourself

```bash
pip install -e ".[packaging]"
# Windows: bundle an unpacked 64-bit VLC and an ffmpeg.exe
python packaging/build.py --vlc-dir C:\path\to\vlc-3.0.23 --ffmpeg C:\path\to\ffmpeg.exe
# Linux: tar.gz, plus an AppImage (needs appimagetool)
python packaging/build.py --appimage
```

Output goes to `dist/`; each build runs its own `--self-test` first.

## The Lua bridge vs. VLC's built-in HTTP interface

Spec #196 calls for the custom Lua bridge as the primary connection,
with VLC's built-in HTTP interface as a coarser fallback (spec #28).
Live testing reversed that. `vlc.httpd():handler()` — what the Lua
bridge is built on — **never closes its side of a connection after
responding**, confirmed via `netstat`: every request leaked one socket
into `CLOSE_WAIT` on VLC's side. At the app's polling cadence that was
enough to degrade a real session from "works" to "VLC refuses every
new connection" within about 15-20 seconds. Client-side mitigations
(reusing one persistent connection, widening timeouts, slowing polling)
cut the leak rate but never to zero. (Testing for 0.2.0 found and fixed a
separate bridge bug that made it look even flakier: the script let Lua's
garbage collector unregister its HTTP handlers, so VLC answered 404 on random
routes. With that fixed, one persistent connection serves every request.)

VLC's *built-in* HTTP interface is different, more mature code and
does not share the leak: verified live, 100 requests over ~45 seconds
of realistic polling left **zero** leaked sockets, using a plain
`requests.Session()`. `bootstrap.select_playback_adapter()` and
`app/vlc_launcher.py`'s `launch_managed_vlc()` default to this adapter.
Its only weakness, whole-second time/length values, is covered by deriving
time from VLC's fractional position and the exact decoded media length.
The Lua bridge remains available (`launch_managed_vlc_with_lua_bridge()`)
for its microsecond seek precision.

## Verified live against real VLC

`vlc/bookmarkstudio.lua` was iteratively debugged against a real,
locally installed VLC 3.0.23 (not just written and assumed correct).
Bugs caught this way, several directly contradicting spec #196's own
claims about VLC's Lua API:

1. `obj:method and obj:method()` is invalid Lua (colon-call syntax
   needs immediate parens) — rejected at script-load time.
2. `vlc.getenv(...)` does not exist in VLC's Lua API. Config comes from
   a file at `vlc.config.configdir()` instead (resolves to `%APPDATA%\vlc`
   on Windows, `~/.config/vlc` on Linux), with VLC's own `--http-password`
   as a fallback (read with `vlc.var.inherit`; `vlc.config.get` only sees
   the saved config file). VLC 3 refuses every request (403) on a handler
   with an empty password.
3. `vlc.httpd()` does not let a script pick its own host/port — it
   binds according to VLC's own `--http-host`/`--http-port` startup
   flags (default, unset: **all interfaces**, port 8080). The launcher
   always passes `--http-host` explicitly (spec #20).
4. **`vlc.httpd():handler()`'s response is not RFC 7230 compliant** —
   it sends a bare status line straight into the body with no
   header-terminating blank line, no `Content-Length`, and never
   closes the connection. `playback/bridge_client.py` reads over a raw
   socket with an idle-gap heuristic.
5. `vlc.player.seek_by_time_absolute` does not exist in VLC 3.0.23's
   Lua API (`vlc.player` is `nil`). Seeking is `vlc.var.set(input, "time", us)`.
6. `vlc.playlist.get("playlist", false)` has no `.current` field. The
   bridge matches the playing input item's URI against each playlist
   child's `.path` instead.
7. The objects returned by `vlc.httpd()` and `httpd:handler()` must stay
   referenced (globals), or Lua's garbage collector unregisters the URLs and
   VLC answers 404 on random routes.
8. `vlc.playlist.pause()` toggles; the bridge's `pause` only pauses a playing
   item.
9. `goto` is a reserved word from Lua 5.2 on, which Linux VLC builds use: the
   script failed to load there (found in 0.3.0 by the first live test against
   Ubuntu's VLC). It now uses `vlc.playlist.gotoitem`.

Also verified live for 0.2.0 (see `tests/vlc/test_live_vlc.py`): precise
seeking right after switching songs, a gap + fade-out loop running to its
completion action, the free-port probe skipping VLC's own port, and the whole
Application playing a bookmark in another song.

## Architecture

```text
PySide6 UI  +  Domain Logic  +  SQLite
                    |
   ordered command queue (off the UI thread)
                    |
                   Playback Adapter
         /                 |                  \
 VLC built-in HTTP   Enhanced Lua Bridge   In-app libVLC
 (spec #28)          (opt-in, spec #196)   (python-vlc)
         \                 |                  /
               VLC Media Player / libVLC
```

VLC performs playback; Python performs the product logic. OS differences
(paths, directories, discovery, WSL interop) live in
`src/bookmark_studio/platform_support.py`. Full rationale in PROJECT_SPEC.md
sections 2, 18-30, 196.

## Layout

```text
src/bookmark_studio/   application package (see PROJECT_SPEC.md #116)
vlc/bookmarkstudio.lua thin VLC Lua HTTP bridge (spec #18-#27)
migrations/            SQLite schema migrations (spec #126), also packaged into the wheel
tests/                 unit (offscreen Qt) and vlc (live, opt-in)
packaging/             PyInstaller spec, build script, third-party notices
src/bookmark_studio/resources/icon.svg  the logo (window, taskbar, dialogs, executables)
archive/               every previous version, unchanged
```

## Non-goals

Not a DAW, not a sample-accurate audio editor, not a replacement media
player. Original media is never modified. See spec #177.
