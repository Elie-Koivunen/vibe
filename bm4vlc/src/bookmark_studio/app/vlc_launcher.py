"""Launches a managed VLC instance (spec #152-#157).

Uses VLC's built-in HTTP interface (`--extraintf=http`, spec #28) as the default, NOT
the custom Lua bridge (`bookmarkstudio.lua`) spec #196 originally called for as primary.
That reversal is deliberate and verified live, not a style choice: `vlc.httpd():handler()`
(what the custom Lua bridge is built on) never closes its side of a connection after
responding, leaking a socket on every single request; at normal polling cadence a real
session degraded from "works" to "VLC refuses new connections at all" within a few
minutes even after every client-side mitigation this codebase could apply (connection
reuse, wide timeouts, slow polling -- see bridge_client.py and app/application.py).
VLC's *built-in* HTTP interface is different, more mature code and does not share this
bug: verified live, 100 requests over ~45 seconds of realistic ~400ms polling left
*zero* leaked sockets, using a completely standard `requests.Session()` with no raw-
socket workaround needed at all (also unlike the Lua bridge's non-RFC-compliant
responses). `launch_managed_vlc_with_lua_bridge()` is kept for anyone who wants the
Lua bridge's microsecond seek precision and is willing to trade reliability for it, but
it is no longer what a normal launch uses.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bookmark_studio.settings.settings_service import SettingsService

_COMMON_ARGS = [
    # Lessons carried over from the sibling buzz2vlc project's live-VLC testing
    # (same author, same VLC quirks -- see that project's PROJECT_SPEC.md):
    "--start-paused",       # avoids an autoplay blip while still expanding the
                             # playlist into individual tracks (buzz2vlc bug #2)
    "--ignore-config",      # stop VLC's saved window geometry fighting our own UI
    "--no-qt-privacy-ask",  # --ignore-config alone reintroduces VLC's first-run
                             # privacy dialog on every launch (buzz2vlc bug #13);
                             # this flag is required alongside it, not instead of it
]


def vlc_user_config_dir() -> Path:
    """The directory vlc.config.configdir() resolves to (verified live: %APPDATA%\\vlc)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA environment variable is not set")
    return Path(appdata) / "vlc"


def has_unmanaged_vlc_process() -> bool:
    """True if a vlc.exe process is running right now, regardless of whether this app
    can talk to it. Purely informational -- there is no way to attach to it if it
    wasn't started with --extraintf=http (see discover_vlc_instances): VLC's HTTP
    remote-control interface can only be enabled at process launch via that flag (or a
    persistent choice under VLC's own Preferences > Interface > Main interfaces >
    Web), never toggled onto an already-running instance from the outside. Used only
    to give the "no instances found" dialog state an honest explanation instead of
    looking like a bug -- confirmed live: a VLC window the user had open by
    double-clicking a file has no HTTP interface at all, so it correctly never shows
    up in discover_vlc_instances(), which was reported as "it doesn't recognize
    preopen existing vlc instances".
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq vlc.exe"],
            capture_output=True, text=True, timeout=5,
            # tasklist.exe is a console app; spawning one from windowless pythonw.exe
            # without this would flash a visible console window every time the
            # launch dialog opens (same root cause as the ffmpeg preload windows --
            # see ffmpeg_decoder.py).
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "vlc.exe" in result.stdout.lower()


def find_free_http_port(preferred: int, *, max_attempts: int = 50) -> int:
    """Picks a port for a new managed VLC instance, starting at `preferred` (the
    configured default) and walking upward until one is free -- needed so a second
    "launch a new VLC instance" doesn't try to bind the same port an already-running
    managed instance holds. A bind-then-close probe has an inherent TOCTOU race
    (the port could be taken again before VLC itself binds it a moment later), but
    that's an acceptable, narrow window for a locally-launched desktop app.
    """
    port = preferred
    for _ in range(max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                port += 1
                continue
        return port
    raise RuntimeError(f"no free port found starting from {preferred}")


def parse_m3u(playlist_path: Path) -> list[str]:
    """Extracts media entries from an .m3u/.m3u8 file (spec #EXTM3U lines and comments
    ignored, blank lines skipped). Relative entries are resolved against the playlist
    file's own directory, matching how VLC and other players interpret them.
    """
    base_dir = playlist_path.parent
    entries: list[str] = []
    for raw_line in playlist_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://", "file://")):
            entries.append(line)
            continue
        candidate = Path(line)
        entries.append(str(candidate if candidate.is_absolute() else (base_dir / candidate).resolve()))
    return entries


def resolve_startup_media(selected_paths: list[str]) -> list[str]:
    """Turns whatever the user picked in the startup file dialog into the flat list of
    media paths VLC's command line expects. A single .m3u/.m3u8 selection expands to
    its contents; anything else (one or more media files) passes through as-is.
    """
    if len(selected_paths) == 1 and selected_paths[0].lower().endswith((".m3u", ".m3u8")):
        return parse_m3u(Path(selected_paths[0]))
    return list(selected_paths)


@dataclass
class VlcInstance:
    """One reachable VLC HTTP endpoint, for the launch/attach picker dialog."""

    port: int
    label: str


def discover_vlc_instances(settings: "SettingsService") -> list[VlcInstance]:
    """Probes every port this app might have a live VLC instance on: the configured
    default plus any it's remembered launching before (settings.known_vlc_ports,
    populated by the "launch a new instance" flow -- see find_free_http_port). This is
    how the "select an open VLC instance" dropdown gets populated; a "raw" VLC the user
    started by double-clicking a file (no --extraintf=http) can never show up here,
    since there's no HTTP interface on it for this app to reach at all.

    Self-healing: a known port that no longer answers is dropped from settings so the
    registry doesn't grow stale entries across restarts. The current default bridge
    port is always re-probed but never removed from settings even if unreachable,
    since it isn't a "known extra" port to begin with -- it's the baseline default.
    """
    from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter

    known = settings.known_vlc_ports()
    candidate_ports = sorted({settings.bridge_port(), *known})
    instances: list[VlcInstance] = []
    for port in candidate_ports:
        adapter = StandardHttpPlaybackAdapter("127.0.0.1", port, settings.bridge_token())
        try:
            adapter.connect()
            playlist = adapter.get_playlist()
            status = adapter.get_status()
        except Exception:  # noqa: BLE001 - not reachable, not a real instance
            if port in known:
                settings.remove_known_vlc_port(port)
            continue
        finally:
            adapter.disconnect()

        label = f"127.0.0.1:{port} — {len(playlist)} item(s) in playlist"
        if status.media_uri:
            now_playing = status.media_uri.rsplit("/", 1)[-1]
            label += f", now: {now_playing}"
        instances.append(VlcInstance(port=port, label=label))
    return instances


def launch_managed_vlc(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    http_password: str,
) -> subprocess.Popen:
    """Spawns VLC with its built-in HTTP interface active (spec #28). Never omits
    --http-host: VLC's own default (all interfaces, port 8080) would violate spec #20.
    """
    args = [
        vlc_path,
        "--extraintf=http",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        f"--http-password={http_password}",
        *_COMMON_ARGS,
        *media_paths,
    ]
    return subprocess.Popen(args)


# -- Lua bridge (bookmarkstudio.lua): kept available, not the default -- see module
# docstring. Anyone opting into it should read that reasoning first.


def bridge_config_path() -> Path:
    return vlc_user_config_dir() / "bookmarkstudio_bridge.conf"


def write_bridge_config(token: str) -> None:
    """Writes the token bookmarkstudio.lua reads on load (never passed on the command
    line in plaintext, per spec #155)."""
    path = bridge_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"token={token}\n", encoding="utf-8")


def bridge_script_install_path() -> Path:
    return vlc_user_config_dir() / "lua" / "intf" / "bookmarkstudio.lua"


def install_bridge_script(source: Path) -> None:
    """Copies vlc/bookmarkstudio.lua into VLC's user Lua interface directory (spec #153)."""
    destination = bridge_script_install_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def launch_managed_vlc_with_lua_bridge(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    token: str,
) -> subprocess.Popen:
    """Opt-in alternative to launch_managed_vlc(): the custom Lua bridge's microsecond
    seek precision, at the cost of the connection-leak reliability problem described
    in this module's docstring. write_bridge_config() and install_bridge_script() must
    both have been called first.
    """
    args = [
        vlc_path,
        "--extraintf=luaintf",
        "--lua-intf=bookmarkstudio",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        *_COMMON_ARGS,
        *media_paths,
    ]
    return subprocess.Popen(args)
