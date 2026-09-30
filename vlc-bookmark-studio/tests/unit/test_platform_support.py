"""Windows / Linux / WSL differences (bookmark_studio.platform_support)."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from bookmark_studio import platform_support as ps
from bookmark_studio.app.vlc_launcher import build_managed_vlc_args, find_free_http_port, parse_m3u

ON_WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(ON_WINDOWS, reason="POSIX path semantics")
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows path semantics")


@pytest.fixture()
def as_wsl(monkeypatch):
    monkeypatch.setattr(ps, "IS_WINDOWS", False)
    monkeypatch.setattr(ps, "is_wsl", lambda: True)


@pytest.fixture()
def as_plain_linux(monkeypatch):
    monkeypatch.setattr(ps, "IS_WINDOWS", False)
    monkeypatch.setattr(ps, "is_wsl", lambda: False)


# -- path translation --


def test_windows_to_wsl_path() -> None:
    assert ps.windows_to_wsl_path(r"C:\Music\a b.mp3") == "/mnt/c/Music/a b.mp3"
    assert ps.windows_to_wsl_path("D:/x/y.flac") == "/mnt/d/x/y.flac"
    assert ps.windows_to_wsl_path("C:") == "/mnt/c"
    assert ps.windows_to_wsl_path(r"\\wsl.localhost\Ubuntu-24.04\home\me\s.mp3") == "/home/me/s.mp3"
    assert ps.windows_to_wsl_path("/already/linux") == "/already/linux"


def test_wsl_to_windows_path_for_mounted_drives() -> None:
    assert ps.wsl_to_windows_path("/mnt/c/Music/a b.mp3") == "C:\\Music\\a b.mp3"
    assert ps.wsl_to_windows_path("/mnt/d") == "D:\\"


@posix_only
def test_uri_from_windows_vlc_maps_to_mnt_under_wsl(as_wsl) -> None:
    assert ps.uri_to_local_path("file:///C:/Music/a%20b.mp3") == Path("/mnt/c/Music/a b.mp3")
    assert ps.uri_to_local_path("file://wsl.localhost/Ubuntu-24.04/home/me/x.mp3") == Path("/home/me/x.mp3")


@posix_only
def test_windows_drive_uri_is_unreachable_on_plain_linux(as_plain_linux) -> None:
    assert ps.uri_to_local_path("file:///C:/Music/a.mp3") is None
    assert ps.uri_to_local_path("file:///home/me/a%20b.mp3") == Path("/home/me/a b.mp3")
    assert ps.uri_to_local_path("file://localhost/home/me/a.mp3") == Path("/home/me/a.mp3")
    assert ps.uri_to_local_path("file://nas/share/a.mp3") is None


def test_non_file_uri_has_no_local_path() -> None:
    assert ps.uri_to_local_path("https://example.com/a.mp3") is None


@windows_only
def test_windows_uris_including_unc_shares() -> None:
    assert ps.uri_to_local_path("file:///C:/Music/a%20b.mp3") == Path(r"C:\Music\a b.mp3")
    assert ps.uri_to_local_path("file://nas/share/a.mp3") == Path(r"\\nas\share\a.mp3")


def test_path_to_uri_roundtrip(tmp_path: Path) -> None:
    media = tmp_path / "a b ä.mp3"
    media.write_bytes(b"x")
    assert ps.uri_to_local_path(ps.path_to_uri(media)) == media.resolve()


def test_path_for_program_translates_only_for_windows_exes_under_wsl(as_wsl) -> None:
    assert ps.path_for_program("/mnt/c/Music/a.mp3", "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe") == "C:\\Music\\a.mp3"
    assert ps.path_for_program("/mnt/c/Music/a.mp3", "/usr/bin/vlc") == "/mnt/c/Music/a.mp3"
    assert ps.path_for_program("https://x/y.mp3", "vlc.exe") == "https://x/y.mp3"


def test_managed_vlc_args_translate_media_for_windows_vlc_under_wsl(as_wsl) -> None:
    args = build_managed_vlc_args(
        "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe", ["/mnt/c/Music/a.mp3"],
        http_port=43119, http_password="pw", http_host="172.26.112.1",
    )
    assert args[-1] == "C:\\Music\\a.mp3"
    assert "--http-host=172.26.112.1" in args


# -- playlists --


@posix_only
def test_playlist_entries_written_on_windows_resolve_on_linux(tmp_path: Path, as_plain_linux) -> None:
    (tmp_path / "music").mkdir()
    playlist = tmp_path / "list.m3u"
    playlist.write_text("music\\track one.flac\nhttp://radio/stream\n", encoding="utf-8")
    assert parse_m3u(playlist) == [str((tmp_path / "music" / "track one.flac").resolve()), "http://radio/stream"]


@posix_only
def test_absolute_windows_entries_map_to_mnt_under_wsl(tmp_path: Path, as_wsl) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text("C:\\Music\\a.mp3\n", encoding="utf-8")
    assert parse_m3u(playlist) == ["/mnt/c/Music/a.mp3"]


def test_cp1252_playlist_is_not_mangled(tmp_path: Path) -> None:
    """A Windows-written .m3u in the ANSI code page used to be decoded as UTF-8 with
    replacement characters, so every non-ASCII filename pointed nowhere."""
    playlist = tmp_path / "list.m3u"
    playlist.write_bytes("Björk – Jóga.mp3\n".encode("cp1252"))
    (entry,) = parse_m3u(playlist)
    assert entry.endswith("Björk – Jóga.mp3")


def test_other_url_schemes_pass_through(tmp_path: Path) -> None:
    playlist = tmp_path / "list.m3u"
    playlist.write_text("rtsp://cam/live\nsmb://nas/a.mp3\n", encoding="utf-8")
    assert parse_m3u(playlist) == ["rtsp://cam/live", "smb://nas/a.mp3"]


# -- ports --


def test_port_used_by_a_reuseaddr_listener_is_not_reported_free() -> None:
    """VLC listens with SO_REUSEADDR. The old probe also set it, and on Windows that
    let the probe bind VLC's live port and call it free (verified against VLC 3.0.23),
    so a second managed VLC was launched onto the same port."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(128)  # VLC accepts connections; a tiny never-accepted backlog would fill up
    port = holder.getsockname()[1]
    try:
        assert ps.tcp_port_in_use("127.0.0.1", port)
        assert find_free_http_port(port) != port
        if not ON_WINDOWS:  # only used from WSL (a Linux client); Windows clients see "refused" ~2s late
            assert find_free_http_port(port, bind_host=None) != port  # connect test alone
    finally:
        holder.close()


# -- processes / networking --


def test_vlc_process_scan_on_linux(tmp_path: Path, as_plain_linux) -> None:
    (tmp_path / "101").mkdir()
    (tmp_path / "101" / "comm").write_text("bash\n")
    (tmp_path / "self").mkdir()
    assert ps.vlc_process_running(proc_root=tmp_path) is False
    (tmp_path / "202").mkdir()
    (tmp_path / "202" / "comm").write_text("vlc\n")
    assert ps.vlc_process_running(proc_root=tmp_path) is True


def test_default_gateway_parsing(tmp_path: Path) -> None:
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t0170A8C0\t0003\t0\t0\t0\t00000000\n"
        "eth0\t0070A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\n"
    )
    assert ps._default_gateway_from_proc(route) == "192.168.112.1"


def test_vlc_http_hosts(monkeypatch, as_wsl) -> None:
    exe = "/mnt/c/Program Files/VideoLAN/VLC/vlc.exe"
    monkeypatch.setattr(ps, "_default_gateway_from_proc", lambda *a, **k: "172.26.112.1")
    monkeypatch.setattr(ps, "wsl_networking_mode", lambda: "nat")
    assert ps.vlc_http_hosts(exe) == ("172.26.112.1", "172.26.112.1")
    assert ps.vlc_http_hosts("/usr/bin/vlc") == ("127.0.0.1", "127.0.0.1")
    monkeypatch.setattr(ps, "wsl_networking_mode", lambda: "mirrored")
    assert ps.vlc_http_hosts(exe) == ("127.0.0.1", "127.0.0.1")


def test_no_console_flag_only_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(ps, "IS_WINDOWS", False)
    assert ps.no_console_window_kwargs() == {}


# -- directories and discovery --


@posix_only
def test_user_data_dir_honours_xdg(monkeypatch, tmp_path: Path, as_plain_linux) -> None:
    monkeypatch.delenv(ps.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert ps.user_data_dir() == tmp_path / "VLCBookmarkStudio"


@windows_only
def test_user_data_dir_uses_localappdata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ps.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ps.user_data_dir() == tmp_path / "VLCBookmarkStudio"


def test_data_dir_option_overrides_the_platform_location(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ps.DATA_DIR_ENV, str(tmp_path / "portable"))
    assert ps.user_data_dir() == tmp_path / "portable"


@posix_only
def test_linux_vlc_lua_dir(monkeypatch, tmp_path: Path, as_plain_linux) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert ps.vlc_user_lua_dir("/usr/bin/vlc") == tmp_path / "data" / "vlc" / "lua"
    assert ps.vlc_user_config_dir("/usr/bin/vlc") == tmp_path / "config" / "vlc"


def test_saved_vlc_path_wins(tmp_path: Path) -> None:
    fake = tmp_path / ("vlc.exe" if ON_WINDOWS else "vlc")
    fake.write_bytes(b"")
    assert ps.find_vlc(str(fake)) == str(fake)
    assert ps.find_ffmpeg(str(fake)) == str(fake)


def test_missing_saved_path_falls_back_to_discovery(tmp_path: Path) -> None:
    result = ps.find_vlc(str(tmp_path / "gone" / "vlc"))
    assert result is None or Path(result).is_file()


def test_vlc_password_is_in_a_private_config_file_not_on_the_command_line(tmp_path) -> None:
    """The HTTP password used to be passed as --http-password=..., readable by any local
    process. It now lives in a per-launch config file (owner-only on Linux), which also
    carries the Qt-only privacy option -- in a config file an unknown option is ignored,
    while on the command line it stops a VLC without the Qt interface from starting."""
    from bookmark_studio.app.vlc_launcher import build_managed_vlc_args

    args = build_managed_vlc_args("/usr/bin/vlc", ["a.mp3"], http_port=47123, http_password="s3cret")
    assert not any("s3cret" in arg for arg in args)
    assert not any(arg.startswith("--no-qt") or arg == "--ignore-config" for arg in args)
    config_arg = next(arg for arg in args if arg.startswith("--config="))
    config = Path(config_arg.split("=", 1)[1])
    text = config.read_text(encoding="utf-8")
    assert "http-password=s3cret" in text and "qt-privacy-ask=0" in text
    assert str(config).startswith(os.environ[ps.DATA_DIR_ENV])
    if not ON_WINDOWS:
        assert config.stat().st_mode & 0o777 == 0o600
