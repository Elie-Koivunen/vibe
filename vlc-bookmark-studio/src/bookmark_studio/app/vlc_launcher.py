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

Cross-platform: Windows, Linux, and WSL (with either a Linux VLC inside WSL or the
Windows vlc.exe through interop) -- see platform_support.py for the details.
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bookmark_studio import platform_support

if TYPE_CHECKING:
    from bookmark_studio.settings.settings_service import SettingsService

# --start-paused avoids an autoplay blip while VLC still expands the playlist into
# individual tracks (a lesson from the sibling buzz2vlc project).
_COMMON_ARGS = ["--start-paused"]


def private_vlc_config_path(http_port: int) -> Path:
    return platform_support.user_data_dir() / "vlc" / f"vlcrc-{http_port}"


def write_private_vlc_config(http_port: int, http_password: str) -> Path:
    """The VLC config file a managed VLC is started with (``--config=<file>``) instead of
    the user's own vlcrc.

    It carries the HTTP password, so the password never appears on VLC's command line
    (where any local process could read it), and turns off VLC's first-run privacy dialog.
    Options in a config file that no installed plugin knows are ignored with a warning,
    unlike the same options on the command line, which stop VLC from starting (e.g. the Qt
    option on a Linux VLC without the Qt interface). Owner-only on Linux; regenerated for
    every launch.
    """
    path = private_vlc_config_path(http_port)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Written by VLC Bookmark Studio for the VLC it launches on this port;\n"
        "# regenerated on every launch.\n"
        "[qt]\n"
        "qt-privacy-ask=0\n"
        "[lua]\n"
        f"http-password={http_password}\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    if not platform_support.IS_WINDOWS:
        os.chmod(path, 0o600)
    return path


# Discovery probes run in parallel with short timeouts so opening the launch dialog
# never stalls the UI for long, however many stale ports are remembered.
_DISCOVERY_TIMEOUT_S = 0.6


def vlc_user_config_dir(vlc_path: str | None = None) -> Path:
    """The directory vlc.config.configdir() resolves to (Windows: %APPDATA%\\vlc,
    Linux: ~/.config/vlc)."""
    return platform_support.vlc_user_config_dir(vlc_path)


def has_unmanaged_vlc_process() -> bool:
    """True if a VLC process is running right now, regardless of whether this app can
    talk to it. Purely informational -- there is no way to attach to it if it wasn't
    started with --extraintf=http (see discover_vlc_instances): VLC's HTTP remote-control
    interface can only be enabled at process launch via that flag (or a persistent choice
    under VLC's own Preferences > Interface > Main interfaces > Web), never toggled onto
    an already-running instance from the outside. Used only to give the "no instances
    found" dialog state an honest explanation instead of looking like a bug.
    """
    return platform_support.vlc_process_running()


def find_free_http_port(preferred: int, *, max_attempts: int = 200, connect_host: str = "127.0.0.1",
                        bind_host: str | None = "127.0.0.1") -> int:
    """Picks a port for a new managed VLC instance, starting at `preferred` and walking
    upward until one is free -- so a second "launch a new VLC instance" doesn't collide
    with a port an already-running managed instance holds.

    When VLC runs on this machine's own network stack (`bind_host` set), a port is free
    if a plain bind succeeds (see platform_support.tcp_port_bindable); nothing can be
    listening on a port we could bind. The earlier SO_REUSEADDR probe reported a port
    VLC was actively listening on as free on Windows (verified against a real VLC
    3.0.23), so two VLCs ended up sharing one port. Ports Windows reserves for
    Hyper-V/WSL (``netsh int ipv4 show excludedportrange``) fail the bind and are
    skipped too.

    `bind_host=None` is for a Windows VLC driven from WSL, whose sockets live on the
    Windows side where a Linux bind proves nothing: there a connection test decides.
    Only a port that accepts a connection counts as busy -- Windows Firewall silently
    drops probes to closed ports across the WSL boundary (verified live), so "no answer"
    can't be told apart from "free".
    """
    port = preferred
    for _ in range(max_attempts):
        if bind_host is not None:
            free = platform_support.tcp_port_bindable(bind_host, port)
        else:
            free = platform_support.tcp_port_state(connect_host, port) != "open"
        if free:
            return port
        port += 1
    raise RuntimeError(f"no free port found starting from {preferred}")


def terminate_managed_vlc(process: subprocess.Popen[bytes], vlc_path: str | None, http_port: int | None) -> None:
    """Closes a VLC this app launched. Under WSL with the Windows vlc.exe, ending the
    Linux-side process handle doesn't reach vlc.exe (it only stops WSL's interop proxy),
    so the Windows process is found by its unique --http-port and ended there."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    if http_port is not None and platform_support.is_wsl() and platform_support.is_windows_executable(vlc_path):
        platform_support.terminate_windows_process_from_wsl("vlc.exe", f"--http-port={http_port}")


def _decode_playlist_text(raw: bytes) -> str:
    """.m3u files written on Windows are often cp1252, not UTF-8 -- decoding them as
    UTF-8 with replacement silently corrupted every non-ASCII filename (so VLC could
    not open those entries). Try UTF-8 strictly first, then the Windows code page."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_m3u(playlist_path: Path) -> list[str]:
    """Extracts media entries from an .m3u/.m3u8 file (#EXTM3U lines and comments
    ignored, blank lines skipped). Relative entries are resolved against the playlist
    file's own directory, matching how VLC and other players interpret them. Windows
    paths inside a playlist are translated when running under WSL (see
    platform_support.normalize_playlist_entry)."""
    base_dir = playlist_path.parent
    entries: list[str] = []
    for raw_line in _decode_playlist_text(playlist_path.read_bytes()).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(platform_support.normalize_playlist_entry(line, base_dir))
    return entries


def is_playlist_file(path: str) -> bool:
    return path.lower().endswith((".m3u", ".m3u8"))


def resolve_startup_media(selected_paths: list[str]) -> list[str]:
    """Turns whatever the user picked in the startup file dialog into the flat list of
    media paths VLC's command line expects. A single .m3u/.m3u8 selection expands to
    its contents; anything else (one or more media files) passes through as-is.
    """
    if len(selected_paths) == 1 and is_playlist_file(selected_paths[0]):
        return parse_m3u(Path(selected_paths[0]))
    return list(selected_paths)


def startup_playlist_source_uri(selected_paths: list[str]) -> str | None:
    """file:// URI of the .m3u the user launched with, if any -- lets playlist
    recognition (spec #10 step 1) find the same bookmark project again next time even
    after the playlist's contents have been edited."""
    if len(selected_paths) == 1 and is_playlist_file(selected_paths[0]):
        return platform_support.path_to_uri(Path(selected_paths[0]))
    return None


@dataclass
class VlcInstance:
    """One reachable VLC HTTP endpoint, for the launch/attach picker dialog."""

    port: int
    label: str
    host: str = "127.0.0.1"


def _probe_instance(host: str, port: int, token: str) -> VlcInstance | None:
    from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter

    adapter = StandardHttpPlaybackAdapter(host, port, token, timeout_scale=_DISCOVERY_TIMEOUT_S / 0.5)
    try:
        adapter.connect()
        playlist = adapter.get_playlist()
        status = adapter.get_status()
    except Exception:  # noqa: BLE001 - not reachable, not a real instance
        return None
    finally:
        adapter.disconnect()
    label = f"{host}:{port} — {len(playlist)} item(s) in playlist"
    if status.media_uri:
        now_playing = status.media_uri.rsplit("/", 1)[-1]
        label += f", now: {now_playing}"
    return VlcInstance(port=port, label=label, host=host)


def discover_vlc_instances(settings: "SettingsService", *, vlc_path: str | None = None) -> list[VlcInstance]:
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
    _bind_host, connect_host = platform_support.vlc_http_hosts(vlc_path)
    known = settings.known_vlc_ports()
    candidate_ports = sorted({settings.bridge_port(), *known})
    token = settings.bridge_token()
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(candidate_ports)))) as pool:
        results = list(pool.map(lambda p: _probe_instance(connect_host, p, token), candidate_ports))

    instances: list[VlcInstance] = []
    for port, instance in zip(candidate_ports, results):
        if instance is None:
            if port in known:
                settings.remove_known_vlc_port(port)
            continue
        instances.append(instance)
    return instances


def build_managed_vlc_args(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    http_password: str,
    http_host: str = "127.0.0.1",
    extra_args: list[str] | None = None,
) -> list[str]:
    """VLC's command line. The password goes into the private config file (see
    write_private_vlc_config), never onto the command line."""
    config = write_private_vlc_config(http_port, http_password)
    return [
        vlc_path,
        f"--config={platform_support.path_for_program(str(config), vlc_path)}",
        "--extraintf=http",
        f"--http-host={http_host}",
        f"--http-port={http_port}",
        *_COMMON_ARGS,
        *(extra_args or []),
        *[platform_support.path_for_program(path, vlc_path) for path in media_paths],
    ]


def _popen_vlc(args: list[str]) -> subprocess.Popen[bytes]:
    # VLC writes a lot to stdout/stderr on Linux; don't let it spill into the terminal
    # this app was started from (or block on a full pipe). A new session keeps a
    # Ctrl+C aimed at this app from also killing the user's VLC.
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if not platform_support.IS_WINDOWS:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def launch_managed_vlc(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    http_password: str,
    http_host: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Spawns VLC with its built-in HTTP interface active (spec #28). Never omits
    --http-host: VLC's own default (all interfaces, port 8080) would violate spec #20.
    `http_host` defaults to platform_support.vlc_http_hosts() (loopback everywhere
    except a Windows VLC driven from WSL under NAT networking).
    """
    bind_host = http_host or platform_support.vlc_http_hosts(vlc_path)[0]
    return _popen_vlc(
        build_managed_vlc_args(
            vlc_path, media_paths, http_port=http_port, http_password=http_password,
            http_host=bind_host, extra_args=extra_args,
        )
    )


# -- Lua bridge (bookmarkstudio.lua): kept available, not the default -- see module
# docstring. Anyone opting into it should read that reasoning first.


def bridge_config_path(vlc_path: str | None = None) -> Path:
    return vlc_user_config_dir(vlc_path) / "bookmarkstudio_bridge.conf"


def write_bridge_config(token: str, vlc_path: str | None = None) -> None:
    """Writes the token bookmarkstudio.lua reads on load (never passed on the command
    line in plaintext, per spec #155)."""
    path = bridge_config_path(vlc_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"token={token}\n", encoding="utf-8")


def bridge_script_install_path(vlc_path: str | None = None) -> Path:
    return platform_support.vlc_user_lua_dir(vlc_path) / "intf" / "bookmarkstudio.lua"


def install_bridge_script(source: Path, vlc_path: str | None = None) -> None:
    """Copies vlc/bookmarkstudio.lua into VLC's user Lua interface directory (spec #153)."""
    destination = bridge_script_install_path(vlc_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def launch_managed_vlc_with_lua_bridge(
    vlc_path: str,
    media_paths: list[str],
    *,
    http_port: int,
    token: str,
    lua_intf: str = "bookmarkstudio",
    http_host: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Opt-in alternative to launch_managed_vlc(): the custom Lua bridge's microsecond
    seek precision, at the cost of the connection-leak reliability problem described
    in this module's docstring. install_bridge_script() must have been called first. The
    token reaches the bridge through the private config file (its --http-password
    fallback), or through write_bridge_config().
    """
    bind_host = http_host or platform_support.vlc_http_hosts(vlc_path)[0]
    config = write_private_vlc_config(http_port, token)
    args = [
        vlc_path,
        f"--config={platform_support.path_for_program(str(config), vlc_path)}",
        "--extraintf=luaintf",
        f"--lua-intf={lua_intf}",
        f"--http-host={bind_host}",
        f"--http-port={http_port}",
        *_COMMON_ARGS,
        *(extra_args or []),
        *[platform_support.path_for_program(path, vlc_path) for path in media_paths],
    ]
    return _popen_vlc(args)
