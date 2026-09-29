"""Finds and loads libVLC for the in-app player (LibVlcPlaybackAdapter).

python-vlc (`import vlc`) binds to one libVLC library the moment it is imported, so the
library has to be chosen first. Candidates, in order:

1. an explicit directory (``--libvlc-dir``, setting ``libvlc/dir`` or ``BM4VLC_LIBVLC_DIR``);
2. the libVLC bundled with a packaged build (``<app>/vlc``);
3. an installed VLC of the right bitness (Windows registry / Program Files);
4. the system library (Linux ``libvlc.so.5``).

On Windows the library must match Python's bitness: the Qt bindings only exist for
64-bit Python, and a 32-bit VLC (``C:\\Program Files (x86)\\VideoLAN``) can't be loaded
into a 64-bit process. That case gets a clear explanation instead of python-vlc's bare
"not a valid Win32 application".
"""
from __future__ import annotations

import ctypes.util
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bookmark_studio import platform_support

_PE_MACHINES = {0x014C: 32, 0x8664: 64, 0xAA64: 64}


class LibVlcUnavailable(RuntimeError):
    """libVLC can't be used; the message says why and what to do."""


@dataclass(frozen=True)
class LibVlcLocation:
    library: str  # path of libvlc.dll / libvlc.so.5 (or a soname for the system library)
    plugins: str | None  # plugin directory, when not the library's default
    source: str  # human-readable origin, for diagnostics


def python_bits() -> int:
    return struct.calcsize("P") * 8


def pe_bits(dll_path: Path) -> int | None:
    """32 or 64 for a Windows DLL (read from its PE header), None if unreadable."""
    try:
        with open(dll_path, "rb") as handle:
            header = handle.read(4096)
        offset = struct.unpack_from("<I", header, 0x3C)[0]
        if header[offset:offset + 4] != b"PE\0\0":
            return None
        machine = struct.unpack_from("<H", header, offset + 4)[0]
    except (OSError, struct.error):
        return None
    return _PE_MACHINES.get(machine)


def _windows_candidate_dirs() -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key_path in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        install_dir, _type = winreg.QueryValueEx(key, "InstallDir")
                except OSError:
                    continue
                dirs.append((Path(install_dir), "installed VLC (registry)"))
    except ImportError:  # pragma: no cover - Windows only
        pass
    for root in platform_support._windows_program_files():
        dirs.append((root / "VideoLAN" / "VLC", f"installed VLC ({root})"))
    return dirs


def _explicit_dirs(explicit: str | None) -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    if explicit:
        dirs.append((Path(explicit), "configured libVLC directory"))
    env = os.environ.get("BM4VLC_LIBVLC_DIR")
    if env:
        dirs.append((Path(env), "BM4VLC_LIBVLC_DIR"))
    bundle = platform_support.bundled_resource_dir() / "vlc"
    dirs.append((bundle, "libVLC bundled with this build"))
    return dirs


def _dir_location(directory: Path, source: str) -> LibVlcLocation | None:
    if platform_support.IS_WINDOWS:
        dll = directory / "libvlc.dll"
        if dll.is_file():
            plugin_dir = directory / "plugins"
            return LibVlcLocation(str(dll), str(plugin_dir) if plugin_dir.is_dir() else None, source)
        return None
    for name in ("libvlc.so.5", "libvlc.so"):
        for sub in (directory, directory / "lib", directory / "usr" / "lib" / "x86_64-linux-gnu"):
            lib = sub / name
            if lib.is_file():
                plugins: Path | None = None
                for candidate in (sub / "vlc" / "plugins", directory / "plugins"):
                    if candidate.is_dir():
                        plugins = candidate
                        break
                return LibVlcLocation(str(lib), str(plugins) if plugins else None, source)
    return None


def find_libvlc(explicit_dir: str | None = None) -> LibVlcLocation:
    """The first usable libVLC, or LibVlcUnavailable explaining what was found."""
    wrong_bitness: list[str] = []
    candidates = _explicit_dirs(explicit_dir)
    if platform_support.IS_WINDOWS:
        candidates += _windows_candidate_dirs()
    seen: set[str] = set()
    for directory, source in candidates:
        key = str(directory).lower().rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        location = _dir_location(directory, source)
        if location is None:
            continue
        if platform_support.IS_WINDOWS:
            bits = pe_bits(Path(location.library))
            if bits is not None and bits != python_bits():
                wrong_bitness.append(f"{location.library} ({bits}-bit)")
                continue
        return location
    if not platform_support.IS_WINDOWS:
        system = ctypes.util.find_library("vlc")
        if system:
            return LibVlcLocation(system, None, "system libVLC")
    if wrong_bitness:
        raise LibVlcUnavailable(
            f"Found VLC, but it is {'32' if python_bits() == 64 else '64'}-bit: "
            + ", ".join(wrong_bitness)
            + f". The in-app player needs a {python_bits()}-bit VLC. Install the 64-bit VLC from "
            "videolan.org (it can sit next to the 32-bit one), use the packaged build (which "
            "includes libVLC), or keep using the separate VLC window (built-in HTTP mode)."
        )
    raise LibVlcUnavailable(
        "libVLC was not found. Install VLC (Linux: `sudo apt install vlc`; Windows: the 64-bit VLC, "
        "or use the Windows portable build, which includes it), or point --libvlc-dir at a VLC folder."
    )


_loaded: tuple[object, LibVlcLocation] | None = None


def load_vlc_module(explicit_dir: str | None = None) -> Any:
    """Imports python-vlc bound to the libVLC chosen by find_libvlc() (once per process)."""
    global _loaded
    if _loaded is not None:
        return _loaded[0]
    location = find_libvlc(explicit_dir)
    if Path(location.library).is_absolute():
        os.environ["PYTHON_VLC_LIB_PATH"] = location.library
        library_dir = str(Path(location.library).parent)
        if platform_support.IS_WINDOWS and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(library_dir)  # libvlccore.dll lives next to libvlc.dll
    if location.plugins:
        os.environ["PYTHON_VLC_MODULE_PATH"] = location.plugins
        os.environ.setdefault("VLC_PLUGIN_PATH", location.plugins)
    try:
        import vlc  # noqa: PLC0415 - must happen after the environment is prepared
    except ImportError as exc:
        raise LibVlcUnavailable(
            "python-vlc is not installed (`pip install python-vlc`); the in-app player needs it."
        ) from exc
    except OSError as exc:
        raise LibVlcUnavailable(f"libVLC at {location.library} could not be loaded: {exc}") from exc
    if getattr(vlc, "dll", None) is None:
        raise LibVlcUnavailable(f"python-vlc could not bind libVLC at {location.library}")
    _loaded = (vlc, location)
    return vlc


def loaded_location() -> LibVlcLocation | None:
    return _loaded[1] if _loaded is not None else None


def libvlc_available(explicit_dir: str | None = None) -> bool:
    try:
        load_vlc_module(explicit_dir)
    except LibVlcUnavailable:
        return False
    return True


__all__ = [
    "LibVlcLocation",
    "LibVlcUnavailable",
    "find_libvlc",
    "libvlc_available",
    "load_vlc_module",
    "loaded_location",
    "pe_bits",
    "python_bits",
]
