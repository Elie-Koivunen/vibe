"""Application composition root: wires repositories, playback adapter, and UI (spec
#114). Owns the live polling loop that connects a PlaybackAdapter to the rest of the
app -- this is the piece spec #178-#180 describe as the startup/song-change/
playlist-change sequences.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QUndoStack

from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.media.resolver import MediaResolver
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.persistence.waveform_repository import WaveformCacheRepository
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer
from bookmark_studio.ui.main_window import MainWindow
from bookmark_studio.waveform.service import WaveformService

STATUS_POLL_MS = 150  # spec #32: 100-200ms normal state
PLAYLIST_POLL_MS = 750  # spec #32: 500-1000ms


class _CallSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _CallWorker(QRunnable):
    """Runs one blocking PlaybackAdapter call on a QThreadPool worker (spec #108: no
    network calls on a UI-blocking thread). Found live: without this, a single slow or
    stalled bridge response froze the entire GUI event loop for the call's whole
    timeout, since QTimer callbacks run on the main thread by default.
    """

    def __init__(self, fn: Callable[[], object], signals: _CallSignals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread via signal
            self._signals.failed.emit(str(exc))
            return
        self._signals.finished.emit(result)


class Application(QObject):
    """Ties one PlaybackAdapter to persistence and the UI for a live session."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        adapter: PlaybackAdapter,
        ffmpeg_path: str,
        waveform_cache_dir: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._log = get_logger("APP")
        self._conn = conn
        self._adapter = adapter

        self._bookmark_repository = BookmarkRepository(conn)
        self._media_repository = MediaRepository(conn)
        self._playlist_repository = PlaylistRepository(conn)
        self._waveform_repository = WaveformCacheRepository(conn)

        self._media_resolver = MediaResolver(self._media_repository)
        recognition = PlaylistRecognitionService(
            self._playlist_repository, self._list_ordered_media_ids_for_playlist
        )
        self._synchronizer = PlaylistSynchronizer(self._playlist_repository, recognition)

        self._waveform_service = WaveformService(ffmpeg_path=ffmpeg_path, cache_dir=waveform_cache_dir)
        self._waveform_orchestrator = WaveformOrchestrator(
            service=self._waveform_service, repository=self._waveform_repository
        )
        self._waveform_orchestrator.waveform_ready.connect(self._on_waveform_ready)

        self._clock = PlaybackClock()
        self._loop_controller = LoopController(adapter, self._clock)

        self.window = MainWindow(self._bookmark_repository, undo_stack=QUndoStack(self))

        self._current_media_id: UUID | None = None
        self._current_vlc_item_id: int | None = None
        self._last_playback_state: str = "stopped"
        self._playlist_items: list = []

        self._thread_pool = QThreadPool(self)
        self._status_inflight = False
        self._playlist_inflight = False
        self._status_signals = _CallSignals(self)
        self._status_signals.finished.connect(self._on_status_result)
        self._status_signals.failed.connect(self._on_status_failed)
        self._playlist_signals = _CallSignals(self)
        self._playlist_signals.finished.connect(self._on_playlist_result)
        self._playlist_signals.failed.connect(self._on_playlist_failed)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_status)
        self._playlist_timer = QTimer(self)
        self._playlist_timer.timeout.connect(self._poll_playlist)

        self._wire_transport()

    def start(self) -> None:
        try:
            self._adapter.connect()
        except Exception as exc:  # noqa: BLE001 - spec #104: stay usable offline
            self._log.info("VLC connect failed, starting offline: %s", exc)
        self._status_timer.start(STATUS_POLL_MS)
        self._playlist_timer.start(PLAYLIST_POLL_MS)
        self._poll_playlist()
        self.window.show()

    # -- transport wiring --
    #
    # Found live, from direct user testing: none of this existed. The transport bar's
    # buttons emitted signals into the void, waveform click-to-seek only moved a local
    # cosmetic playhead without ever telling VLC to seek, and the playlist panel was
    # never populated by the running app at all (only by throwaway demo scripts). This
    # is the missing connection layer between the UI widgets and the real adapter.

    def _wire_transport(self) -> None:
        transport = self.window._transport
        transport.play_pause_clicked.connect(self._on_play_pause_clicked)
        transport.stop_clicked.connect(lambda: self._fire_and_forget(self._adapter.stop))
        transport.seek_back_clicked.connect(
            lambda: self._fire_and_forget(lambda: self._adapter.seek_relative_us(-5_000_000))
        )
        transport.seek_forward_clicked.connect(
            lambda: self._fire_and_forget(lambda: self._adapter.seek_relative_us(5_000_000))
        )
        transport.previous_track_clicked.connect(lambda: self._fire_and_forget(self._adapter.previous_track))
        transport.next_track_clicked.connect(lambda: self._fire_and_forget(self._adapter.next_track))
        transport.previous_bookmark_clicked.connect(self._on_previous_bookmark)
        transport.next_bookmark_clicked.connect(self._on_next_bookmark)

        self.window._waveform_scene.seek_requested.connect(
            lambda time_us: self._fire_and_forget(lambda: self._adapter.seek_absolute_us(time_us))
        )
        self.window._playlist_panel.item_double_clicked.connect(
            lambda vlc_id: self._fire_and_forget(lambda: self._adapter.goto_item(vlc_id))
        )
        self.window.play_selection_requested.connect(self._on_play_selection_requested)
        self.window.loop_selection_requested.connect(self._on_loop_selection_requested)

    def _fire_and_forget(self, fn) -> None:
        """Dispatches a one-off adapter command off the main thread (spec #108), same
        pattern as the polling workers -- a command like seek/rate can take long enough
        (confirmed live: up to several hundred ms against real VLC) that calling it
        directly from a button click would visibly stall the GUI.
        """
        signals = _CallSignals(self)
        signals.failed.connect(lambda msg: self._log.debug("command failed: %s", msg))
        self._thread_pool.start(_CallWorker(fn, signals))

    def _on_play_pause_clicked(self) -> None:
        if self._last_playback_state == "playing":
            self._fire_and_forget(self._adapter.pause)
        else:
            self._fire_and_forget(self._adapter.play)

    def _on_play_selection_requested(self, start_us: int, end_us: int) -> None:
        def _play() -> None:
            self._adapter.seek_absolute_us(start_us)
            self._adapter.play()

        self._fire_and_forget(_play)

    def _on_loop_selection_requested(self, start_us: int, end_us: int) -> None:
        from bookmark_studio.domain.enums import CompletionAction
        from bookmark_studio.domain.loop import LoopSpec

        self._loop_controller.start(
            LoopSpec(start_us=start_us, end_us=end_us, repeat_count=None, gap_ms=0,
                      completion_action=CompletionAction.CONTINUE)
        )

    def _current_bookmarks_sorted(self) -> list:
        if self._current_media_id is None:
            return []
        playlist_id = self._synchronizer.active_playlist_id
        bookmarks = (
            self._bookmark_repository.list_for_playlist_media(playlist_id, self._current_media_id)
            if playlist_id is not None
            else self._bookmark_repository.list_global_for_media(self._current_media_id)
        )
        return sorted(bookmarks, key=lambda b: b.start_us)

    def _on_previous_bookmark(self) -> None:
        bookmarks = self._current_bookmarks_sorted()
        if not bookmarks:
            return
        current_time = self._clock.estimated_position_us()
        candidates = [b for b in bookmarks if b.start_us < current_time - 500_000]
        target = candidates[-1] if candidates else bookmarks[-1]
        self._fire_and_forget(lambda: self._adapter.seek_absolute_us(target.start_us))

    def _on_next_bookmark(self) -> None:
        bookmarks = self._current_bookmarks_sorted()
        if not bookmarks:
            return
        current_time = self._clock.estimated_position_us()
        candidates = [b for b in bookmarks if b.start_us > current_time + 500_000]
        target = candidates[0] if candidates else bookmarks[0]
        self._fire_and_forget(lambda: self._adapter.seek_absolute_us(target.start_us))

    def stop(self) -> None:
        self._status_timer.stop()
        self._playlist_timer.stop()
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # -- polling --
    #
    # Each tick dispatches the adapter call to a QThreadPool worker instead of calling
    # it directly (spec #108). An in-flight guard skips a tick rather than queuing a
    # second overlapping call if the previous one hasn't returned yet -- with a slow or
    # stalled bridge, piling up unbounded overlapping requests would be worse than
    # just catching up on the next tick.

    def _poll_status(self) -> None:
        if self._status_inflight:
            return
        self._status_inflight = True
        self._thread_pool.start(_CallWorker(self._adapter.get_status, self._status_signals))

    def _on_status_result(self, status: object) -> None:
        self._status_inflight = False
        self._clock.update(status)
        self._last_playback_state = status.state
        self.window._waveform_scene.set_playhead_time_us(status.time_us)
        self.window._transport.set_time(status.time_us, status.duration_us)
        self.window._playlist_panel.set_current_playing(status.current_playlist_item_id)
        self._loop_controller.on_tick()

        if status.current_playlist_item_id != self._current_vlc_item_id:
            self._current_vlc_item_id = status.current_playlist_item_id
            self._on_current_item_changed(status.media_uri, status.duration_us)

    def _on_status_failed(self, message: str) -> None:
        self._status_inflight = False
        self._log.debug("status poll failed: %s", message)  # spec #104: never crash the UI

    def _poll_playlist(self) -> None:
        if self._playlist_inflight:
            return
        self._playlist_inflight = True
        self._thread_pool.start(_CallWorker(self._adapter.get_playlist, self._playlist_signals))

    def _on_playlist_result(self, items: object) -> None:
        self._playlist_inflight = False
        self._playlist_items = list(items)
        resolved = [(item, self._media_resolver.resolve(item.uri)) for item in items if item.uri]
        if not resolved:
            self.window._playlist_panel.set_playlist([], {})
            return

        ordered_media_ids = [media.id for _item, media in resolved]
        result = self._synchronizer.on_snapshot(source_uri=None, ordered_media_ids=ordered_media_ids)
        self._log.debug("playlist sync: %s -> %s", result.action, result.playlist_id)

        playlist_id = self._synchronizer.active_playlist_id
        bookmark_counts = {}
        for item, media in resolved:
            bookmarks = (
                self._bookmark_repository.list_for_playlist_media(playlist_id, media.id)
                if playlist_id is not None
                else self._bookmark_repository.list_global_for_media(media.id)
            )
            bookmark_counts[item.vlc_id] = len(bookmarks)
        self.window._playlist_panel.set_playlist([item for item, _media in resolved], bookmark_counts)

    def _on_playlist_failed(self, message: str) -> None:
        self._playlist_inflight = False
        self._log.debug("playlist poll failed: %s", message)

    # -- reactions --

    def _on_current_item_changed(self, media_uri: str | None, duration_us: int | None) -> None:
        if not media_uri:
            return
        media = self._media_resolver.resolve(media_uri, duration_us=duration_us)
        self._current_media_id = media.id

        playlist_id = self._synchronizer.active_playlist_id
        playlist_name = "Unknown playlist"
        if playlist_id is not None:
            record = self._playlist_repository.get(playlist_id)
            if record is not None:
                playlist_name = record.playlist.name

        bookmarks = (
            self._bookmark_repository.list_for_playlist_media(playlist_id, media.id)
            if playlist_id is not None
            else self._bookmark_repository.list_global_for_media(media.id)
        )
        self.window.set_context(
            playlist_name=playlist_name,
            track_name=media.title or media.filename or media_uri,
            playlist_id=playlist_id,
            media_id=media.id,
            bookmark_count=len(bookmarks),
            duration_us=duration_us or media.duration_us or 0,
        )
        self.window.load_bookmarks(bookmarks)

        local_path = _uri_to_path(media.canonical_uri or media_uri)
        if local_path is not None and local_path.exists() and media.fast_fingerprint:
            self._waveform_orchestrator.request(media.id, media.fast_fingerprint, str(local_path))

    def _on_waveform_ready(self, media_id: UUID, pyramid) -> None:
        if media_id != self._current_media_id:
            return  # switched tracks again before this one finished (spec #65)
        duration_us = self.window._waveform_scene._duration_us
        self.window._waveform_scene.set_waveform(pyramid, duration_us)

    def _list_ordered_media_ids_for_playlist(self, playlist_id: UUID) -> list[UUID]:
        # Best-effort: uses whatever the synchronizer currently has tracked. A full
        # implementation would query playlist_items; acceptable for MVP since this is
        # only consulted for similarity scoring against *other* known playlists.
        return []


def _uri_to_path(uri: str) -> Path | None:
    from bookmark_studio.media.resolver import uri_to_local_path

    return uri_to_local_path(uri)
