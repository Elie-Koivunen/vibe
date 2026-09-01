"""Launches a managed VLC instance with the bookmarkstudio.lua bridge wired up
(spec #152-#157). Not itself in the spec's file-layout table, but required since
bookmarkstudio.lua's own comments explain it depends on this for correct, secure
--http-host/--http-port flags (see vlc/bookmarkstudio.lua's Security section).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def vlc_user_config_dir() -> Path:
    """The directory vlc.config.configdir() resolves to (verified live: %APPDATA%\\vlc)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA environment variable is not set")
    return Path(appdata) / "vlc"


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


def launch_managed_vlc(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    token: str,
) -> subprocess.Popen:
    """Spawns VLC with the bridge active. Never omits --http-host: VLC's own default
    (all interfaces, port 8080) is exactly the spec #20 violation this flag prevents --
    confirmed live that a bare `vlc.httpd()` call binds "* port 8080" with no host flag.
    """
    write_bridge_config(token)
    args = [
        vlc_path,
        "--extraintf=luaintf",
        "--lua-intf=bookmarkstudio",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        # Lessons carried over from the sibling buzz2vlc project's live-VLC testing
        # (same author, same VLC quirks -- see that project's PROJECT_SPEC.md):
        "--start-paused",       # avoids an autoplay blip while still expanding the
                                 # playlist into individual tracks (buzz2vlc bug #2)
        "--ignore-config",      # stop VLC's saved window geometry fighting our own UI
        "--no-qt-privacy-ask",  # --ignore-config alone reintroduces VLC's first-run
                                 # privacy dialog on every launch (buzz2vlc bug #13);
                                 # this flag is required alongside it, not instead of it
        *media_paths,
    ]
    return subprocess.Popen(args)
