"""PlaybackAdapter that plays inside this app through libVLC (python-vlc).

Compared with driving a separate VLC window over HTTP:

* time and length come straight from the player in milliseconds (the HTTP interface
  only reports whole seconds), and seeks are by time, not by percentage;
* there is no network, no socket leak, no password, and status reads are cheap enough
  to poll every 100 ms for a smoother playhead;
* the app owns the playlist: media are loaded with ``load()`` (from files or an .m3u)
  instead of being read from a VLC window.

All methods are safe to call from the command-queue and polling threads.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookmark_studio import platform_support
from bookmark_studio.playback.libvlc_loader import instance_failure_hint, load_vlc_module
from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem

GOTO_SETTLE_TIMEOUT_S = 2.0
_SETTLE_POLL_S = 0.02
_PARSE_TIMEOUT_MS = 3000


@dataclass
class _Entry:
    vlc_id: int
    mrl: str
    name: str
    media: Any
    duration_s: float | None = field(default=None)


class LibVlcPlaybackAdapter:
    """In-process playback. `instance_args` are extra libVLC options (tests pass
    ``--aout=adummy`` for silent output)."""

    # The session polls in-process adapters faster (see PlaybackSession).
    status_poll_ms = 100
    playlist_poll_ms = 1000
    manages_playlist = True

    def __init__(self, *, libvlc_dir: str | None = None, instance_args: list[str] | None = None) -> None:
        self._vlc = load_vlc_module(libvlc_dir)
        args = ["--no-video-title-show", "--quiet", *(instance_args or [])]
        self._instance = self._vlc.Instance(args)
        if self._instance is None:
            raise RuntimeError(f"libVLC refused to start with options {args} ({instance_failure_hint()})")
        self._player = self._instance.media_player_new()
        self._list = self._instance.media_list_new()
        self._list_player = self._instance.media_list_player_new()
        self._list_player.set_media_player(self._player)
        self._list_player.set_media_list(self._list)
        self._lock = threading.RLock()
        self._entries: list[_Entry] = []
        self._next_id = 1
        self._volume_pct = 100
        self._volume_pending = False  # set while libVLC has no audio output to apply it to
        self._closed = False

    # -- playlist (owned by the app) --

    def load(self, paths_or_uris: list[str]) -> list[VlcPlaylistItem]:
        """Replaces the playlist. Accepts file paths or URIs (as parse_m3u returns)."""
        with self._lock:
            self._list_player.stop()
            self._list.lock()
            try:
                while self._list.count() > 0:
                    self._list.remove_index(0)
            finally:
                self._list.unlock()
            self._entries = []
            for raw in paths_or_uris:
                self._append_locked(raw)
            return self._items_locked()

    def append(self, paths_or_uris: list[str]) -> None:
        with self._lock:
            for raw in paths_or_uris:
                self._append_locked(raw)

    def _append_locked(self, raw: str) -> None:
        mrl = raw if "://" in raw else Path(raw).resolve().as_uri()
        media = self._instance.media_new(mrl)
        name = Path(platform_support.uri_to_local_path(mrl) or mrl).name if mrl.startswith("file:") else mrl
        entry = _Entry(self._next_id, mrl, name, media)
        self._next_id += 1
        self._list.lock()
        try:
            self._list.add_media(media)
        finally:
            self._list.unlock()
        self._entries.append(entry)
        try:  # durations fill in as libVLC parses (asynchronously)
            media.parse_with_options(self._vlc.MediaParseFlag.local, _PARSE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - older python-vlc: no parse_with_options
            pass

    def _items_locked(self) -> list[VlcPlaylistItem]:
        items = []
        for entry in self._entries:
            if entry.duration_s is None:
                length_ms = entry.media.get_duration()
                if length_ms and length_ms > 0:
                    entry.duration_s = length_ms / 1000.0
            items.append(VlcPlaylistItem(entry.vlc_id, entry.mrl, entry.name, entry.duration_s))
        return items

    def _current_entry_locked(self) -> _Entry | None:
        media = self._player.get_media()
        if media is None:
            return None
        mrl = media.get_mrl()
        for entry in self._entries:
            if entry.media is media or entry.mrl == mrl:
                return entry
        return None

    def _index_of(self, vlc_id: int) -> int:
        for index, entry in enumerate(self._entries):
            if entry.vlc_id == vlc_id:
                return index
        raise ValueError(f"unknown playlist item id {vlc_id}")

    # -- PlaybackAdapter --

    def connect(self) -> None:
        if self._closed:
            raise RuntimeError("the in-app player has been closed")

    def disconnect(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._list_player.stop()
            finally:
                self._list_player.release()
                self._player.release()
                self._list.release()
                self._instance.release()

    def get_status(self) -> PlaybackStatus:
        with self._lock:
            if self._closed:
                raise RuntimeError("the in-app player has been closed")
            state = self._player.get_state()
            entry = self._current_entry_locked()
            time_ms = self._player.get_time()
            length_ms = self._player.get_length()
            if (length_ms is None or length_ms <= 0) and entry is not None and entry.duration_s:
                length_ms = int(entry.duration_s * 1000)
            if self._volume_pending and state == self._vlc.State.Playing:
                self._apply_volume_locked()
            return PlaybackStatus(
                state=self._state_name(state),
                time_us=max(0, int(time_ms)) * 1000 if time_ms and time_ms > 0 else 0,
                position=max(0.0, float(self._player.get_position() or 0.0)),
                rate=float(self._player.get_rate() or 1.0),
                current_playlist_item_id=entry.vlc_id if entry is not None else None,
                duration_us=int(length_ms) * 1000 if length_ms and length_ms > 0 else None,
                media_uri=entry.mrl if entry is not None else None,
                # Nothing but this app can change the in-app player's volume, so the
                # requested level is the truth. (libVLC forgets a volume set before its
                # audio output exists, and some outputs always report 0.)
                volume=round(self._volume_pct * 256 / 100),
            )

    def _state_name(self, state: object) -> str:
        states = self._vlc.State
        if state in (states.Playing, states.Opening, states.Buffering):
            return "playing"
        if state == states.Paused:
            return "paused"
        return "stopped"

    def get_playlist(self) -> list[VlcPlaylistItem]:
        with self._lock:
            return self._items_locked()

    def play(self) -> None:
        with self._lock:
            state = self._player.get_state()
            if state == self._vlc.State.Paused:
                self._player.set_pause(0)
            elif state not in (self._vlc.State.Playing, self._vlc.State.Opening, self._vlc.State.Buffering):
                self._volume_pending = True
                self._list_player.play()

    def pause(self) -> None:
        with self._lock:
            if self._player.get_state() in (self._vlc.State.Playing, self._vlc.State.Buffering):
                self._player.set_pause(1)

    def stop(self) -> None:
        with self._lock:
            self._list_player.stop()

    def next_track(self) -> None:
        with self._lock:
            self._volume_pending = True  # a new input may bring a new audio output
            self._list_player.next()

    def previous_track(self) -> None:
        with self._lock:
            self._volume_pending = True
            self._list_player.previous()

    def goto_item(self, vlc_id: int) -> None:
        with self._lock:
            self._volume_pending = True
            self._list_player.play_item_at_index(self._index_of(vlc_id))
        self._wait_until_seekable(vlc_id)
        with self._lock:
            if self._volume_pending and self._player.get_state() == self._vlc.State.Playing:
                self._apply_volume_locked()

    def _wait_until_seekable(self, vlc_id: int) -> None:
        """play_item_at_index only starts opening the item; seeks sent before its input
        is running are dropped."""
        deadline = time.monotonic() + GOTO_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                entry = self._current_entry_locked()
                ready = (
                    entry is not None and entry.vlc_id == vlc_id
                    and self._player.get_state() in (self._vlc.State.Playing, self._vlc.State.Paused)
                    and (self._player.get_length() or 0) > 0
                )
            if ready:
                return
            time.sleep(_SETTLE_POLL_S)

    def seek_absolute_us(self, time_us: int) -> None:
        with self._lock:
            self._player.set_time(max(0, int(time_us // 1000)))

    def seek_relative_us(self, delta_us: int) -> None:
        with self._lock:
            current = max(0, self._player.get_time() or 0)
            self._player.set_time(max(0, current + int(delta_us // 1000)))

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self._player.set_rate(float(rate))

    def set_volume(self, level: int) -> None:
        """`level` uses VLC's 0-512 scale (256 = 100%); libVLC takes percent (0-200)."""
        percent = max(0, min(200, round(int(level) * 100 / 256)))
        with self._lock:
            self._volume_pct = percent
            self._apply_volume_locked()

    def _apply_volume_locked(self) -> None:
        # audio_set_volume returns -1 while there is no audio output; retried when playing.
        self._volume_pending = self._player.audio_set_volume(self._volume_pct) != 0

    def set_exact_duration(self, vlc_id: int, duration_us: int | None) -> None:
        """No-op: libVLC already reports millisecond-precise lengths."""
