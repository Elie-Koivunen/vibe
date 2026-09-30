"""Every operating-system difference the app cares about, in one place.

Supported setups:

* Windows, native Python, Windows VLC.
* Linux, native Python, Linux VLC.
* WSL (Linux Python inside Windows Subsystem for Linux), driving either a Linux VLC
  installed inside WSL (preferred: native paths, 127.0.0.1 networking) or the Windows
  VLC via WSL interop. The Windows VLC case needs path translation both ways
  (``/mnt/c/...`` <-> ``C:\\...``) and, under WSL's default NAT networking, a host
  address other than 127.0.0.1 (see :func:`vlc_http_hosts`).

Module-level flags are read through the module (``platform_support.IS_WINDOWS``) by
callers, so tests can monkeypatch them to exercise another platform's code path.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import socket
import subprocess
import sys
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

IS_WINDOWS = sys.platform == "win32"

APP_DIR_NAME = "VLCBookmarkStudio"

_WINDOWS_DRIVE_RE = re.compile(r"^/?([A-Za-z]):(?:[\\/](.*))?$")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-z])(?:/(.*))?$")


# -- platform detection --


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    """True when this Linux process runs inside Windows Subsystem for Linux."""
    if IS_WINDOWS or not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii").lower()
    except OSError:
        return False


def is_windows_executable(path: str | None) -> bool:
    """A Windows program (vlc.exe/ffmpeg.exe), including one reached from WSL through /mnt/c."""
    return bool(path) and str(path).lower().endswith(".exe")


def no_console_window_kwargs() -> dict[str, Any]:
    """subprocess kwargs that stop a console app (ffmpeg.exe, tasklist.exe) from
    flashing a window when spawned from windowless pythonw.exe."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if IS_WINDOWS and flag:
        return {"creationflags": flag}
    return {}


def is_frozen() -> bool:
    """True inside a packaged (PyInstaller) build."""
    return bool(getattr(sys, "frozen", False))


def restore_child_library_path() -> None:
    """A packaged Linux build runs with LD_LIBRARY_PATH pointing at its bundled libraries;
    the programs it starts (the system's vlc and ffmpeg) must not load those instead of
    their own. PyInstaller keeps the user's original value in LD_LIBRARY_PATH_ORIG."""
    if not is_frozen() or IS_WINDOWS:
        return
    original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        os.environ["LD_LIBRARY_PATH"] = original
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def bundled_resource_dir() -> Path:
    """Folder holding files shipped with a packaged build (libVLC in ``vlc/``, ffmpeg in
    ``ffmpeg/``): next to the executable. In a source checkout: ``<repo>/build-resources``
    (normally absent)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2] / "build-resources"


# -- per-user directories --

# Environment settings are VLC_BOOKMARK_STUDIO_<NAME>; the names from before 0.4.0
# (BM4VLC_<NAME>, when the app was called bm4vlc) still work.
ENV_PREFIX = "VLC_BOOKMARK_STUDIO_"
_LEGACY_ENV_PREFIX = "BM4VLC_"
DATA_DIR_ENV = ENV_PREFIX + "DATA_DIR"


def env_setting(name: str) -> str | None:
    """VLC_BOOKMARK_STUDIO_<name>, else the pre-0.4 BM4VLC_<name>; None if neither is set."""
    return os.environ.get(ENV_PREFIX + name) or os.environ.get(_LEGACY_ENV_PREFIX + name) or None


def user_data_dir() -> Path:
    """Where the database, waveform cache and logs live.

    Windows: %LOCALAPPDATA%\\VLCBookmarkStudio. Linux/WSL: $XDG_DATA_HOME/VLCBookmarkStudio
    (default ~/.local/share/VLCBookmarkStudio). ``--data-dir`` (the
    VLC_BOOKMARK_STUDIO_DATA_DIR environment variable) overrides both.
    """
    override = env_setting("DATA_DIR")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def vlc_user_config_dir(vlc_path: str | None = None) -> Path:
    """The directory ``vlc.config.configdir()`` returns for the given VLC binary.

    Windows VLC: %APPDATA%\\vlc (also when driven from WSL, where %APPDATA% is looked up
    through interop). Linux VLC: $XDG_CONFIG_HOME/vlc (default ~/.config/vlc).
    """
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable is not set")
        return Path(appdata) / "vlc"
    if is_wsl() and is_windows_executable(vlc_path):
        appdata = windows_env_from_wsl("APPDATA")
        if appdata is None:
            raise RuntimeError("could not read %APPDATA% from Windows through WSL interop")
        return Path(windows_to_wsl_path(appdata)) / "vlc"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vlc"


def vlc_user_lua_dir(vlc_path: str | None = None) -> Path:
    """Where VLC looks for user Lua scripts (``<dir>/intf/<name>.lua``).

    Windows VLC: %APPDATA%\\vlc\\lua. Linux VLC: $XDG_DATA_HOME/vlc/lua
    (default ~/.local/share/vlc/lua).
    """
    if IS_WINDOWS or (is_wsl() and is_windows_executable(vlc_path)):
        return vlc_user_config_dir(vlc_path) / "lua"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "vlc" / "lua"


# -- WSL <-> Windows path translation --


def windows_to_wsl_path(windows_path: str) -> str:
    """``C:\\Music\\a.mp3`` -> ``/mnt/c/Music/a.mp3``;
    ``\\\\wsl.localhost\\<distro>\\home\\x`` -> ``/home/x``. Anything else is returned unchanged."""
    text = windows_path.replace("\\", "/")
    match = _WINDOWS_DRIVE_RE.match(text)
    if match:
        rest = match.group(2) or ""
        return f"/mnt/{match.group(1).lower()}/{rest}".rstrip("/") if rest else f"/mnt/{match.group(1).lower()}"
    for prefix in ("//wsl.localhost/", "//wsl$/"):
        if text.lower().startswith(prefix):
            remainder = text[len(prefix):]
            _distro, _sep, inner = remainder.partition("/")
            return "/" + inner
    return windows_path


def wsl_to_windows_path(linux_path: str) -> str:
    """``/mnt/c/Music/a.mp3`` -> ``C:\\Music\\a.mp3``. Paths inside the Linux filesystem
    become ``\\\\wsl.localhost\\<distro>\\...`` (via ``wslpath -w`` when available)."""
    match = _WSL_MOUNT_RE.match(linux_path)
    if match:
        rest = (match.group(2) or "").replace("/", "\\")
        return f"{match.group(1).upper()}:\\{rest}"
    wslpath = shutil.which("wslpath")
    if wslpath:
        try:
            result = subprocess.run(
                [wslpath, "-w", linux_path], capture_output=True, text=True, timeout=5, check=True
            )
            converted = result.stdout.strip()
            if converted:
                return converted
        except (OSError, subprocess.SubprocessError):
            pass
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
    return f"\\\\wsl.localhost\\{distro}" + linux_path.replace("/", "\\")


def windows_env_from_wsl(name: str) -> str | None:
    """Reads a Windows environment variable (e.g. APPDATA) from inside WSL."""
    cmd = shutil.which("cmd.exe") or "/mnt/c/Windows/System32/cmd.exe"
    try:
        result = subprocess.run(
            [cmd, "/d", "/c", f"echo %{name}%"], capture_output=True, text=True, timeout=10,
            cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if not value or value == f"%{name}%":
        return None
    return value


def path_for_program(local_path: str, program: str | None) -> str:
    """Converts a path this process can open into one `program` can open.

    Only differs from `local_path` when this process runs in WSL and `program` is a
    Windows executable (vlc.exe/ffmpeg.exe reached through /mnt/c).
    """
    if _URL_SCHEME_RE.match(local_path):
        return local_path
    if is_wsl() and is_windows_executable(program):
        return wsl_to_windows_path(local_path)
    return local_path


# -- file:// URIs --


def uri_to_local_path(uri: str) -> Path | None:
    """Converts a file:// URI (as VLC reports it) into a path this process can open.

    Handles, besides the plain case: a Windows VLC's ``file:///C:/...`` URIs seen from
    WSL (-> ``/mnt/c/...``), ``file://wsl.localhost/<distro>/...`` URIs, and Windows UNC
    shares (``file://server/share/...``) on Windows. Returns None for non-file URIs and
    for locations this platform cannot reach.
    """
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "file":
        return None
    host = parsed.netloc
    if IS_WINDOWS:
        if host and host.lower() != "localhost":
            return Path("\\\\" + host + url2pathname(parsed.path))
        return Path(url2pathname(parsed.path))

    path = unquote(parsed.path)
    if host and host.lower() not in ("localhost",):
        if host.lower() in ("wsl.localhost", "wsl$") and is_wsl():
            _distro, _sep, inner = path.lstrip("/").partition("/")
            return Path("/" + inner)
        return None
    drive = _WINDOWS_DRIVE_RE.match(path)
    if drive:
        if is_wsl():
            return Path(windows_to_wsl_path(path.lstrip("/")))
        return None
    return Path(path)


def path_to_uri(path: Path) -> str:
    """Canonical file:// URI for a local path, resolved but not lower-cased (spec #70)."""
    return path.resolve(strict=False).as_uri()


def normalize_playlist_entry(entry: str, base_dir: Path) -> str:
    """Turns one line of an .m3u file into a path/URL usable on this platform.

    Absolute Windows paths (``C:\\Music\\a.mp3``) become ``/mnt/c/Music/a.mp3`` under WSL;
    backslash-separated relative paths written on Windows resolve correctly on Linux.
    URLs (any ``scheme://``) pass through untouched.
    """
    if _URL_SCHEME_RE.match(entry):
        return entry
    if not IS_WINDOWS:
        if _WINDOWS_DRIVE_RE.match(entry) or entry.startswith("\\\\"):
            return windows_to_wsl_path(entry) if is_wsl() else entry
        entry = entry.replace("\\", "/")
        candidate = Path(entry)
    else:
        candidate = Path(entry)
        if PureWindowsPath(entry).is_absolute():
            return str(candidate)
    return str(candidate if candidate.is_absolute() else (base_dir / candidate).resolve())


# -- locating VLC and ffmpeg --


def _windows_program_files() -> list[Path]:
    roots: list[Path] = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    roots += [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def windows_registry_vlc_dirs() -> list[Path]:
    """InstallDir of every VLC registered in the Windows registry (64- and 32-bit
    views, machine and user); empty elsewhere."""
    if sys.platform != "win32":  # also tells type checkers winreg is Windows-only
        return []
    import winreg

    dirs: list[Path] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    install_dir, _type = winreg.QueryValueEx(key, "InstallDir")
            except OSError:
                continue
            dirs.append(Path(install_dir))
    return dirs


def _vlc_from_windows_registry() -> str | None:
    if not IS_WINDOWS:
        return None
    for install_dir in windows_registry_vlc_dirs():
        candidate = install_dir / "vlc.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def vlc_candidates() -> list[str]:
    """Likely VLC locations for this platform, most preferred first (existence not checked)."""
    candidates: list[str] = []
    if IS_WINDOWS:
        registry = _vlc_from_windows_registry()
        if registry:
            candidates.append(registry)
        on_path = shutil.which("vlc")
        if on_path:
            candidates.append(on_path)
        candidates += [str(root / "VideoLAN" / "VLC" / "vlc.exe") for root in _windows_program_files()]
        # A packaged build carries a portable VLC; used when none is installed.
        candidates.append(str(bundled_resource_dir() / "vlc" / "vlc.exe"))
        return candidates

    on_path = shutil.which("vlc")
    if on_path:
        candidates.append(on_path)
    candidates += [
        "/usr/bin/vlc",
        "/usr/local/bin/vlc",
        "/snap/bin/vlc",
        "/var/lib/flatpak/exports/bin/org.videolan.VLC",
        str(Path.home() / ".local/share/flatpak/exports/bin/org.videolan.VLC"),
    ]
    if is_wsl():
        # Fallback only: a Linux VLC inside WSL is simpler (native paths and networking).
        candidates += [
            "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe",
            "/mnt/c/Program Files (x86)/VideoLAN/VLC/vlc.exe",
        ]
    return candidates


def find_vlc(saved_path: str | None = None) -> str | None:
    """A saved path first (if it still exists), then the platform's usual locations."""
    if saved_path and Path(saved_path).is_file():
        return saved_path
    for candidate in vlc_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


def ffmpeg_candidates() -> list[str]:
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    # A packaged build may ship its own ffmpeg; it wins over whatever is installed.
    candidates: list[str] = [str(bundled_resource_dir() / "ffmpeg" / exe)]
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        userprofile = os.environ.get("USERPROFILE")
        candidates += [str(root / "ffmpeg" / "bin" / "ffmpeg.exe") for root in _windows_program_files()]
        candidates += [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"]
        if local:
            candidates.append(str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"))
        if userprofile:
            candidates.append(str(Path(userprofile) / "scoop" / "shims" / "ffmpeg.exe"))
        return candidates
    candidates += ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"]
    if is_wsl():
        candidates += ["/mnt/c/Program Files/ffmpeg/bin/ffmpeg.exe", "/mnt/c/ffmpeg/bin/ffmpeg.exe"]
    return candidates


def find_ffmpeg(saved_path: str | None = None) -> str | None:
    if saved_path and Path(saved_path).is_file():
        return saved_path
    for candidate in ffmpeg_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


# -- processes --


def _linux_process_names(proc_root: Path) -> list[str]:
    names: list[str] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return names
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            names.append((entry / "comm").read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            continue
    return names


def _tasklist_has_vlc(tasklist: str) -> bool:
    try:
        result = subprocess.run(
            [tasklist, "/FI", "IMAGENAME eq vlc.exe"],
            capture_output=True, text=True, timeout=5, **no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "vlc.exe" in (result.stdout or "").lower()


def vlc_process_running(*, proc_root: Path = Path("/proc")) -> bool:
    """True if any VLC process is running (whether or not this app can talk to it).

    Windows: ``tasklist``. Linux: scans /proc for a process named ``vlc``. WSL: both,
    since the VLC window may belong to either side.
    """
    if IS_WINDOWS:
        return _tasklist_has_vlc("tasklist")
    if any(name == "vlc" for name in _linux_process_names(proc_root)):
        return True
    if is_wsl():
        tasklist = shutil.which("tasklist.exe") or "/mnt/c/Windows/System32/tasklist.exe"
        if Path(tasklist).exists():
            return _tasklist_has_vlc(tasklist)
    return False


def _windows_tool(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def windows_pids_from_wsl(image_name: str, command_line_contains: str) -> list[int]:
    """Windows PIDs (seen from WSL) of `image_name` processes whose command line
    contains the given text -- e.g. the unique ``--http-port=N`` of a VLC this app
    launched, so the user's own VLC windows are never matched."""
    if not re.fullmatch(r"[\w.-]+", image_name) or "'" in command_line_contains:
        raise ValueError("unsafe process filter")
    powershell = _windows_tool(
        "powershell.exe", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{image_name}'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{command_line_contains}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def terminate_windows_process_from_wsl(image_name: str, command_line_contains: str) -> int:
    """Ends matching Windows processes from inside WSL. Needed because terminating the
    Linux-side handle of a Windows program started through WSL interop only stops the
    interop proxy -- the Windows process (e.g. vlc.exe) keeps running. Returns how many
    were ended."""
    taskkill = _windows_tool("taskkill.exe", "/mnt/c/Windows/System32/taskkill.exe")
    pids = windows_pids_from_wsl(image_name, command_line_contains)
    for pid in pids:
        for args in ([taskkill, "/PID", str(pid)], [taskkill, "/PID", str(pid), "/F"]):
            try:
                subprocess.run(args, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if pid not in windows_pids_from_wsl(image_name, command_line_contains):
                break
    return len(pids)


# -- networking --


def _default_gateway_from_proc(route_file: Path = Path("/proc/net/route")) -> str | None:
    try:
        lines = route_file.read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            gateway_hex = fields[2]
            try:
                raw = bytes.fromhex(gateway_hex)
            except ValueError:
                continue
            return socket.inet_ntoa(raw[::-1])
    return None


@lru_cache(maxsize=1)
def wsl_networking_mode() -> str:
    """'mirrored' or 'nat' (WSL's default). Uses ``wslinfo`` (WSL 2.3+) when present."""
    wslinfo = shutil.which("wslinfo")
    if wslinfo:
        try:
            result = subprocess.run([wslinfo, "--networking-mode"], capture_output=True, text=True, timeout=5)
            mode = result.stdout.strip().lower()
            if mode in ("mirrored", "nat", "virtioproxy", "none"):
                return mode
        except (OSError, subprocess.SubprocessError):
            pass
    return "nat"


def vlc_http_hosts(vlc_path: str | None) -> tuple[str, str]:
    """(address VLC should bind its HTTP interface to, address this app connects to).

    Always loopback, except a Windows VLC driven from WSL under NAT networking: there
    the Windows host is only reachable at the WSL virtual switch's gateway address, so
    VLC binds to exactly that address (reachable from WSL and the host, not the LAN).
    """
    if is_wsl() and is_windows_executable(vlc_path) and wsl_networking_mode() != "mirrored":
        gateway = _default_gateway_from_proc()
        if gateway:
            return gateway, gateway
    return "127.0.0.1", "127.0.0.1"


def tcp_port_state(host: str, port: int, *, timeout_s: float = 0.2) -> str:
    """'open' (something accepted the connection), 'closed' (actively refused), or
    'unknown' (timed out: a full backlog, or a firewall dropping the probe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout_s)
        try:
            code = probe.connect_ex((host, port))
        except OSError:
            return "unknown"
    if code == 0:
        return "open"
    if code in _REFUSED_ERRNOS:
        return "closed"
    return "unknown"


_REFUSED_ERRNOS = {getattr(errno, name) for name in ("ECONNREFUSED", "WSAECONNREFUSED") if hasattr(errno, name)}
_REFUSED_ERRNOS.add(10061)  # WSAECONNREFUSED, as connect_ex reports it on Windows


def tcp_port_in_use(host: str, port: int, *, timeout_s: float = 0.2) -> bool:
    """True unless host:port actively refused a connection (a timeout counts as in use)."""
    return tcp_port_state(host, port, timeout_s=timeout_s) != "closed"


def tcp_port_bindable(host: str, port: int) -> bool:
    """True if this process could bind host:port right now.

    A plain bind, deliberately WITHOUT SO_REUSEADDR: on Windows that option lets a
    second socket bind a port another program is already listening on (VLC's own
    listener uses SO_REUSEADDR), so the old probe reported VLC's busy port as free --
    verified against a real VLC 3.0.23. A plain bind fails against such a listener on
    both Windows and Linux, and (unlike SO_EXCLUSIVEADDRUSE) isn't thrown off by
    unrelated client sockets lingering in TIME_WAIT.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True
