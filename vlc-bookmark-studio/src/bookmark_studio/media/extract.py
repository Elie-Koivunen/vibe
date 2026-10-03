"""Extract: bookmarks saved as audio files of their own, cut from the song with FFmpeg
(argv-array only, never a shell -- spec #58).

A file is named after the song, the bookmark and its range, e.g.
``Groove One - drop - 00-00-12.000 to 00-00-21.500.mp3``, in one of the open formats
below. A name some file systems couldn't store (symbols such as emoji, characters Windows
forbids, a reserved or overlong name) is replaced by one that starts with the bookmark's
own (random, unique) name and keeps what can be kept of the song's -- see file_name_for.
Which formats an FFmpeg can write depends on how it was built, so the encoders are asked
first (available_formats)."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from bookmark_studio import platform_support
from bookmark_studio.domain.timecode import format_timecode


@dataclass(frozen=True)
class AudioFormat:
    key: str
    label: str
    extension: str
    encoder: str
    # (label, FFmpeg arguments) -- the first is the default
    qualities: tuple[tuple[str, tuple[str, ...]], ...]


FORMATS: tuple[AudioFormat, ...] = (
    AudioFormat("mp3", "MP3", "mp3", "libmp3lame", (
        ("320 kbps", ("-b:a", "320k")),
        ("256 kbps", ("-b:a", "256k")),
        ("192 kbps", ("-b:a", "192k")),
        ("128 kbps", ("-b:a", "128k")),
        ("Variable, best (V0)", ("-q:a", "0")),
    )),
    AudioFormat("ogg", "Ogg Vorbis", "ogg", "libvorbis", (
        ("Quality 8 (about 256 kbps)", ("-q:a", "8")),
        ("Quality 6 (about 192 kbps)", ("-q:a", "6")),
        ("Quality 4 (about 128 kbps)", ("-q:a", "4")),
    )),
    AudioFormat("opus", "Opus", "opus", "libopus", (
        ("256 kbps", ("-b:a", "256k")),
        ("160 kbps", ("-b:a", "160k")),
        ("96 kbps", ("-b:a", "96k")),
    )),
    AudioFormat("flac", "FLAC (lossless)", "flac", "flac", (
        ("Lossless", ("-compression_level", "8")),
    )),
    AudioFormat("wav", "WAV (uncompressed)", "wav", "pcm_s16le", (
        ("16-bit", ()),
        ("24-bit", ("-c:a", "pcm_s24le")),
    )),
)
FORMATS_BY_KEY = {fmt.key: fmt for fmt in FORMATS}

_ENCODER_LINE = re.compile(r"^\s*[A-Z.]{6}\s+(\S+)")
_encoders_cache: dict[str, frozenset[str]] = {}
_encoders_lock = threading.Lock()
_STDERR_TAIL_LINES = 30


def parse_encoders(output: str) -> frozenset[str]:
    """The encoder names in `ffmpeg -encoders` output (lines like " A....D flac  FLAC ...")."""
    names = set()
    for line in output.splitlines():
        match = _ENCODER_LINE.match(line)
        # (the legend above the list, " A..... = Audio", isn't an encoder)
        if match and line.strip()[0] == "A" and match.group(1) != "=":
            names.add(match.group(1))
    return frozenset(names)


def available_formats(ffmpeg_path: str | None) -> set[str]:
    """The keys of the FORMATS this FFmpeg can write (none without an FFmpeg)."""
    if not ffmpeg_path:
        return set()
    with _encoders_lock:
        encoders = _encoders_cache.get(ffmpeg_path)
    if encoders is None:
        try:
            result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-encoders"], stdin=subprocess.DEVNULL, capture_output=True,
                timeout=15, **platform_support.no_console_window_kwargs(),
            )
            encoders = parse_encoders(result.stdout.decode("utf-8", errors="replace"))
        except (OSError, subprocess.SubprocessError):
            encoders = frozenset()
        with _encoders_lock:
            _encoders_cache[ffmpeg_path] = encoders
    return {fmt.key for fmt in FORMATS if fmt.encoder in encoders}


# -- file names --

_FORBIDDEN = set('<>:"/\\|?*')  # Windows (and FAT/exFAT/NTFS) refuse these; "/" everywhere
# Letters, digits and marks of any script, and this punctuation, every common file system
# stores (NTFS, FAT32/exFAT, ext4, APFS/HFS+, network shares). Symbols such as emoji or
# box drawing, private-use and unassigned code points, control characters: not.
_PUNCTUATION = set(" -_.,;'!()[]{}&+=@#%~$^`")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME_BYTES = 200  # ext4 allows 255 bytes; room left for "(2)" and a long folder
MAX_SONG_CHARS = 60  # of the song's name in a replacement name
_SPACES = re.compile(r"\s+")


def _portable_char(ch: str) -> bool:
    if ch in _FORBIDDEN:
        return False
    if ch in _PUNCTUATION:
        return True
    if ord(ch) > 0xFFFF:
        return False  # outside the Basic Multilingual Plane: emoji, rare scripts
    return unicodedata.category(ch)[0] in ("L", "N", "M")


def name_problem(name: str) -> str | None:
    """Why `name` couldn't be stored by some common file system, or None if it can."""
    if not name.strip():
        return "it is empty"
    if any(not _portable_char(ch) for ch in name):
        bad = "".join(sorted({ch for ch in name if not _portable_char(ch)}))[:8]
        return f"it has characters some systems can't store ({bad!r})"
    if unicodedata.normalize("NFC", name) != name:
        return "its accents are stored in a form macOS and Windows disagree on"
    if name != name.strip() or name.endswith("."):
        return "it starts or ends with a space or ends with a dot"
    if name.split(".")[0].strip().upper() in _RESERVED:
        return "it is a name Windows reserves"
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return "it is too long"
    return None


def salvage(text: str, max_chars: int = MAX_SONG_CHARS) -> str:
    """What can be kept of `text` for a file name: its storable characters."""
    kept = "".join(ch for ch in unicodedata.normalize("NFC", text) if _portable_char(ch))
    kept = _SPACES.sub(" ", kept).strip(" .")
    kept = kept[:max_chars].rstrip(" .")
    return "" if kept.split(".")[0].strip().upper() in _RESERVED else kept


def file_timecode(time_us: int) -> str:
    """00-00-12.000: a timecode without the colons file names can't have."""
    total_ms = max(0, int(time_us)) // 1000
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}.{millis:03d}"


def output_file_name(song: str, bookmark_name: str, start_us: int, end_us: int, extension: str,
                     repeats: int = 1) -> str:
    """Song - bookmark - start to end.ext ("... x4.ext" when the range repeats 4 times)"""
    times = f" x{repeats}" if repeats > 1 else ""
    return f"{song} - {bookmark_name} - {file_timecode(start_us)} to {file_timecode(end_us)}{times}.{extension}"


def file_name_for(job: ExtractJob, extension: str, repeats: int = 1) -> tuple[str, str | None]:
    """The file's name, and why it isn't the usual one (None when it is). The usual one --
    song, bookmark, range -- unless some file system couldn't store it; then the
    bookmark's own name (random and unique; its id's first characters if that won't do
    either) goes first, followed by what can be kept of the song's name, and the range."""
    assert job.end_us is not None
    usual = output_file_name(job.song, job.name, job.start_us, job.end_us, extension, repeats)
    problem = name_problem(usual)
    if problem is None:
        return usual, None
    prefix = job.name if name_problem(job.name) is None and len(job.name) <= MAX_SONG_CHARS else str(job.bookmark_id)[:8]
    if name_problem(prefix) is not None:
        prefix = "bookmark"
    parts = [prefix, salvage(job.song), f"{file_timecode(job.start_us)} to {file_timecode(job.end_us)}"]
    times = f" x{repeats}" if repeats > 1 else ""
    return " - ".join(part for part in parts if part) + f"{times}.{extension}", problem


def unique_path(folder: Path, file_name: str) -> Path:
    """`folder/file_name`, or "name (2).ext", "name (3).ext"... if that is taken: an
    extraction never overwrites a file."""
    candidate = folder / file_name
    stem, suffix = os.path.splitext(file_name)
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


# -- one extraction --

MAX_REPEATS = 99
# Formats that carry a song's cover picture along (ID3 / FLAC pictures).
COVER_FORMATS = frozenset({"mp3", "flac"})


@dataclass
class ExtractJob:
    """One bookmark to save. `source` is None when its song's file can't be found."""

    bookmark_id: object
    song: str
    name: str
    start_us: int
    end_us: int | None
    source: Path | None
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    problem: str | None = field(default=None)
    title: str | None = None  # the song's title, for the file's own title tag
    loop_enabled: bool = False
    repeat_count: int | None = None  # None: "Forever"
    gap_ms: int = 0

    def can_extract(self) -> bool:
        return self.problem is None and self.end_us is not None and self.source is not None

    def why_not(self) -> str | None:
        if self.problem:
            return self.problem
        if self.end_us is None:
            return "a point bookmark has no range"
        if self.source is None:
            return "the song's file was not found"
        return None

    def repeats(self) -> int:
        """How often the range plays as the bookmark loops: its Repeat count. Loop off, or
        "Forever" (no count to go by): once."""
        if not self.loop_enabled or not self.repeat_count:
            return 1
        return max(1, min(MAX_REPEATS, self.repeat_count))


def _seconds(time_us: int) -> str:
    return f"{time_us / 1_000_000:.6f}"


def tool_name() -> str:
    from bookmark_studio import __version__

    return f"VLC Bookmark Studio {__version__}"


def file_tags(job: ExtractJob, repeats: int) -> dict[str, str]:
    """What the file says about itself, on top of the song's own tags (copied as they
    are): the title names the bookmark, the comment where it came from and what made it."""
    assert job.end_us is not None
    title = job.title or job.song
    times = f" x{repeats}" if repeats > 1 else ""
    return {
        "title": f"{title} - {job.name}",
        "encoded_by": tool_name(),
        "comment": (f"Bookmark “{job.name}”, {format_timecode(job.start_us)} to "
                    f"{format_timecode(job.end_us)}{times}, of “{title}”. Extracted with {tool_name()}."),
    }


def clean_tags(tags: dict[str, str] | list[tuple[str, str]] | tuple[tuple[str, str], ...] | None) -> dict[str, str]:
    """The user's own tags, as FFmpeg can take them: a name without "=" or control
    characters (well-known names in lower case, so they land in the right fields), a value
    without control characters; empty ones dropped."""
    pairs = list(tags.items()) if isinstance(tags, dict) else list(tags or ())
    cleaned: dict[str, str] = {}
    for key, value in pairs:
        key = "".join(ch for ch in str(key) if ch != "=" and unicodedata.category(ch)[0] != "C").strip()
        value = "".join(ch for ch in str(value) if ch == "\n" or unicodedata.category(ch)[0] != "C").strip()
        if not key or not value:
            continue
        cleaned[key.lower() if key.lower() in KNOWN_TAGS else key] = value
    return cleaned


# Tag names FFmpeg maps onto each format's own fields (ID3 frames, Vorbis comments...).
KNOWN_TAGS = ("title", "artist", "album_artist", "album", "genre", "date", "track", "disc", "composer",
              "performer", "publisher", "copyright", "comment", "description", "grouping", "language",
              "lyrics", "encoded_by")


def build_extract_args(ffmpeg_path: str, job: ExtractJob, output: Path, fmt: AudioFormat,
                       quality_index: int = 0, *, apply_fades: bool = False, repeats: int = 1,
                       cover: bool = False, tags: dict[str, str] | None = None) -> list[str]:
    """FFmpeg's arguments for one file. The range is read once; `repeats` > 1 plays it that
    many times, with the bookmark's gap as silence in between. Fades (the bookmark's own
    lengths) go over the whole file: in at its start, out at its end. The song's tags are
    kept -- its stream tags too, which is where Ogg and Opus keep them -- and its cover
    picture when `cover` (MP3 and FLAC only). `tags`, the user's own, come last: they win
    over the song's and the file's."""
    assert job.end_us is not None and job.source is not None
    duration_us = job.end_us - job.start_us
    repeats = max(1, min(MAX_REPEATS, repeats))
    gap_s = max(0, job.gap_ms) / 1000 if repeats > 1 else 0.0
    total_s = repeats * duration_us / 1_000_000 + (repeats - 1) * gap_s
    _label, quality_args = fmt.qualities[max(0, min(quality_index, len(fmt.qualities) - 1))]
    args = [
        ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-n",
        # Seeking on the input is fast, and exact when re-encoding; -t there reads just
        # the range (once, however often it repeats).
        "-ss", _seconds(job.start_us), "-t", _seconds(duration_us),
        # Under WSL a Windows ffmpeg.exe needs C:\... paths, not /mnt/c/...
        "-i", platform_support.path_for_program(str(job.source), ffmpeg_path),
    ]
    graph: list[str] = []
    audio = "[0:a:0]"
    if repeats > 1:
        graph.append(audio + f"asplit={repeats}" + "".join(f"[r{i}]" for i in range(repeats)))
        parts = []
        for i in range(repeats):
            if gap_s > 0 and i < repeats - 1:
                graph.append(f"[r{i}]apad=pad_dur={gap_s:.3f}[p{i}]")
                parts.append(f"[p{i}]")
            else:
                parts.append(f"[r{i}]")
        graph.append("".join(parts) + f"concat=n={repeats}:v=0:a=1[joined]")
        audio = "[joined]"
    fades = []
    if apply_fades:
        if job.fade_in_ms > 0:
            fades.append(f"afade=t=in:st=0:d={min(job.fade_in_ms / 1000, total_s):.3f}")
        if job.fade_out_ms > 0:
            fade_out_s = min(job.fade_out_ms / 1000, total_s)
            fades.append(f"afade=t=out:st={total_s - fade_out_s:.3f}:d={fade_out_s:.3f}")
    if fades:
        graph.append(audio + ",".join(fades) + "[out]")
        audio = "[out]"
    if graph:
        args += ["-filter_complex", ";".join(graph), "-map", audio]
    else:
        args += ["-map", "0:a:0"]
    if cover and fmt.key in COVER_FORMATS:
        args += ["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    # The song's tags: its stream's (Ogg, Opus) and its file's, into the file's.
    args += ["-map_metadata", "0:s:a:0", "-map_metadata", "0"]
    for key, value in {**file_tags(job, repeats), **clean_tags(tags)}.items():
        args += ["-metadata", f"{key}={value}", "-metadata:s:a:0", f"{key}={value}"]
    args += ["-c:a", fmt.encoder, *quality_args]
    args.append(platform_support.path_for_program(str(output), ffmpeg_path))
    return args


def has_cover_picture(ffmpeg_path: str, source: Path) -> bool:
    """Whether the song carries a cover picture (an "attached pic" stream)."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-nostdin", "-i", platform_support.path_for_program(str(source), ffmpeg_path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, **platform_support.no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"(attached pic)" in result.stderr


class ExtractCancelled(Exception):
    pass


def extract(ffmpeg_path: str, job: ExtractJob, folder: Path, fmt: AudioFormat, quality_index: int = 0, *,
            apply_fades: bool = False, repeats: int = 1, tags: dict[str, str] | None = None,
            cancelled: threading.Event | None = None) -> Path:
    """Writes one bookmark's audio into `folder` and returns the file. It is written under
    a temporary name first and renamed when complete, so a failed or cancelled extraction
    leaves no half-written file behind. A cover picture that won't go along is left out
    rather than failing the file."""
    if not job.can_extract():
        raise ValueError(job.why_not() or "nothing to extract")
    assert job.end_us is not None and job.source is not None
    repeats = max(1, min(MAX_REPEATS, repeats))
    final = unique_path(folder, file_name_for(job, fmt.extension, repeats)[0])
    cover = fmt.key in COVER_FORMATS and has_cover_picture(ffmpeg_path, job.source)
    try:
        return _run(ffmpeg_path, job, folder, final, fmt, quality_index, apply_fades=apply_fades,
                    repeats=repeats, cover=cover, tags=tags, cancelled=cancelled)
    except RuntimeError:
        if not cover:
            raise
        return _run(ffmpeg_path, job, folder, final, fmt, quality_index, apply_fades=apply_fades,
                    repeats=repeats, cover=False, tags=tags, cancelled=cancelled)


def _run(ffmpeg_path: str, job: ExtractJob, folder: Path, final: Path, fmt: AudioFormat, quality_index: int, *,
         apply_fades: bool, repeats: int, cover: bool, tags: dict[str, str] | None,
         cancelled: threading.Event | None) -> Path:
    partial = unique_path(folder, f"{final.stem}.partial.{fmt.extension}")
    args = build_extract_args(ffmpeg_path, job, partial, fmt, quality_index, apply_fades=apply_fades,
                              repeats=repeats, cover=cover, tags=tags)
    process = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        **platform_support.no_console_window_kwargs(),
    )
    tail: deque[bytes] = deque(maxlen=_STDERR_TAIL_LINES)

    def drain() -> None:
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            tail.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            if cancelled is not None and cancelled.wait(0.1):
                process.kill()
                process.wait()
                raise ExtractCancelled()
            if cancelled is None:
                process.wait()
        reader.join(timeout=2)
        if process.returncode != 0:
            message = b"".join(tail).decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or f"ffmpeg exited {process.returncode}")
        partial.replace(final)
        return final
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()
        if partial.exists():
            partial.unlink()  # our own unfinished output (a fresh name, see unique_path)
