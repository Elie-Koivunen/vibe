# VLC Bookmark Studio (bm4vlc)

A playlist-aware visual bookmarking, segment-selection, navigation and
looping system for VLC Media Player. See [PROJECT_SPEC.md](PROJECT_SPEC.md)
for the full design and engineering specification (198 sections), and
[CHANGELOG.md](CHANGELOG.md) for what changed in each version.

Runs on **Windows**, **Linux**, and **WSL (Ubuntu on Windows)**.

## Status

Version 0.2.0. The core is implemented and tested: domain model, SQLite
persistence + migrations, media fingerprint/resolution, playlist
recognition across sessions (similarity scoring over stored playlist
order), the FFmpeg waveform pipeline, three playback adapters (Mock / VLC's
built-in HTTP / the custom Lua bridge), the software loop controller (gaps,
fades, completion actions), undo/redo, project export/import (format v2,
merging), and a PySide6 UI wired end-to-end through a live polling
`Application`. All playback commands run on one ordered queue off the UI
thread.

Tests: 300+ unit/integration tests (offscreen Qt, mock VLC) plus opt-in live
tests against a real VLC (`tests/vlc/`). Verified on Windows 11 (Python 3.12,
VLC 3.0.23) and WSL Ubuntu 24.04 (Python 3.10–3.13, driving the Windows VLC).

**Not built yet** (see PROJECT_SPEC.md #175-176): Segment Queue, Loop
Trainer, clip export, Audacity label import/export, beat detection, video
thumbnails, lane assignment in the UI.

## Setup

You need Python 3.10+, VLC 3.x and (for waveforms) ffmpeg. The app finds VLC
and ffmpeg in their usual places; a path can also be saved in the settings
(`vlc/path`, `ffmpeg/path`).

### Windows

1. Install [VLC](https://www.videolan.org/), [Python](https://www.python.org/)
   (3.10 or newer) and ffmpeg (e.g. `winget install Gyan.FFmpeg`, or unpack it
   to `C:\Program Files\ffmpeg`).
2. In the `bm4vlc` folder:

   ```bat
   py -3 -m venv .venv
   .venv\Scripts\pip install -e .
   ```

3. Double-click `launch_bm4vlc.bat`.

PySide6 installs deep folder trees. If pip fails with *"The filename or
extension is too long"* (Windows' 260-character path limit), put the venv on
a short path instead (`py -3 -m venv C:\v` then `C:\v\Scripts\pip install -e .`;
`launch_bm4vlc.bat` also looks there) or enable Windows long paths.

### Linux (Ubuntu/Debian)

```bash
sudo apt install vlc ffmpeg python3-venv libegl1 libxkbcommon0 libxcb-cursor0
python3 -m venv .venv
.venv/bin/pip install -e .
./launch_bm4vlc.sh
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
logs). The two are separate: a WSL session and a Windows session don't share
bookmarks (use Save Bookmarks / Import Project to move them).

## Tests

```bash
# Linux / WSL
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
# Windows (PowerShell)
$env:QT_QPA_PLATFORM="offscreen"; .venv\Scripts\python -m pytest
```

Live tests start a real, silent, window-less VLC (`-I dummy --aout=adummy`):

```bash
BM4VLC_LIVE_VLC=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/vlc
```

CI (`.github/workflows/bm4vlc-tests.yml` at the repository root) runs the suite
on Windows and Ubuntu 22.04/24.04 with Python 3.10, 3.12 and 3.13.

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
              /            \
  VLC built-in HTTP    Enhanced Lua Bridge
  (default, spec #28)  (opt-in, spec #196)
              \            /
               VLC Media Player
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
archive/               every previous version, unchanged
```

## Non-goals

Not a DAW, not a sample-accurate audio editor, not a replacement media
player. Original media is never modified. See spec #177.
