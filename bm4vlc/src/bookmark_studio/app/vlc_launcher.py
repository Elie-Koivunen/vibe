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
import subprocess
from pathlib import Path

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
