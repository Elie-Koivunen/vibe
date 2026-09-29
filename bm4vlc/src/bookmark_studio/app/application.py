"""Application composition root: wires repositories, playback adapter, and UI (spec
#114). Owns the live polling loop that connects a PlaybackAdapter to the rest of the
app -- this is the piece spec #178-#180 describe as the startup/song-change/
playlist-change sequences.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QDialog, QMessageBox

from bookmark_studio import platform_support
from bookmark_studio.app.vlc_launcher import (
    discover_vlc_instances, find_free_http_port, has_unmanaged_vlc_process, launch_managed_vlc,
    terminate_managed_vlc,
)
from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
from bookmark_studio.domain.enums import CompletionAction
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.domain.selection import Selection
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.media.resolver import MediaResolver, uri_to_local_path
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.persistence.waveform_repository import WaveformCacheRepository
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.command_queue import CommandExecutor, ThreadedCommandQueue
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer, SyncAction
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchChoice, VlcLaunchDialog
from bookmark_studio.ui.main_window import MainWindow
from bookmark_studio.waveform.service import WaveformService

# spec #32 suggests 100-200ms status / 500-1000ms playlist. Verified live these are too
# aggressive for VLC's real Lua httpd: every request that doesn't get a response within
# its timeout forces BridgeClient to reconnect, and each reconnect leaks a socket on
# VLC's side (see bridge_client.py's module docstring -- VLC's httpd never closes its
# end). Slower polling directly cuts total request volume and, with it, the absolute
# leak rate.
STATUS_POLL_MS = 400
PLAYLIST_POLL_MS = 2000
# A status poll issued less than this long after a seek/goto/play command finished may
# still describe the old position (VLC applies seeks asynchronously), so it's skipped.
# Without this, a poll sampled just before a loop's seek-back but delivered just after
# it re-triggered the loop boundary: one real pass was counted twice.
STALE_SAMPLE_MARGIN_NS = 150_000_000

AskPlaylistMatch = Callable[[str, float], bool]


class _CallSignals(QObject):
    finished = Signal(object)  # (issued_monotonic_ns, result)
    failed = Signal(str)


class _CallWorker(QRunnable):
    """Runs one blocking PlaybackAdapter read (a status/playlist poll) on a QThreadPool
    worker (spec #108: no network calls on a UI-blocking thread). Emits the monotonic
    time the request was *issued* with the result, so stale samples can be recognised.
    """

    def __init__(self, fn: Callable[[], object], signals: _CallSignals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        issued_ns = time.monotonic_ns()
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - reported to the main thread via signal
            self._signals.failed.emit(str(exc))
            return
        self._signals.finished.emit((issued_ns, result))


def _unpack_sample(payload: object) -> tuple[int, object]:
    """Poll results arrive as (issued_ns, value); direct callers (tests) may pass the
    bare value, which is treated as fresh."""
    if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], int):
        return payload[0], payload[1]
    return time.monotonic_ns(), payload


class Application(QObject):
    """Ties one PlaybackAdapter to persistence and the UI for a live session."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        adapter: PlaybackAdapter,
        ffmpeg_path: str,
        waveform_cache_dir: Path,
        mute_on_connect: bool = False,
        settings: SettingsService | None = None,
        vlc_path: str | None = None,
        vlc_process: subprocess.Popen | None = None,
        command_executor: CommandExecutor | None = None,
        ask_playlist_match: AskPlaylistMatch | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._log = get_logger("APP")
        self._conn = conn
        self._adapter = adapter
        # Needed for the "Launch VLC..." picker (prompt_vlc_launch_dialog): which VLC
        # binary to spawn, where to persist/discover known instance ports, and the
        # subprocess handle of whichever instance THIS app most recently spawned (so
        # stop() can clean it up -- never set for an attached-to, not-spawned-by-us
        # instance). None outside of a real bootstrap.main() run simply disables that
        # picker.
        self._settings = settings
        self._vlc_path = vlc_path
        self._vlc_process = vlc_process
        self._vlc_port: int | None = None  # HTTP port of the VLC this app launched (WSL cleanup)
        # Only true when this Application spawned its own VLC process: forcing volume to
        # 0 on an existing VLC the user already had open would be an unwelcome surprise.
        # Sent once, on the first successful status poll.
        self._mute_pending = mute_on_connect
        self._stopped = False

        self._bookmark_repository = BookmarkRepository(conn)
        self._media_repository = MediaRepository(conn)
        self._playlist_repository = PlaylistRepository(conn)
        self._waveform_repository = WaveformCacheRepository(conn)

        self._media_resolver = MediaResolver(self._media_repository)
        recognition = PlaylistRecognitionService(
            self._playlist_repository, self._playlist_repository.list_item_media_ids
        )
        self._synchronizer = PlaylistSynchronizer(self._playlist_repository, recognition)
        self._ask_playlist_match = ask_playlist_match or self._ask_playlist_match_dialog
        self._source_uri: str | None = None  # .m3u this session's VLC was launched from
        self._asking_playlist_match = False
        self._declined_matches: set[tuple[UUID, tuple[UUID, ...]]] = set()

        self._waveform_service = WaveformService(ffmpeg_path=ffmpeg_path, cache_dir=waveform_cache_dir)
        self._waveform_orchestrator = WaveformOrchestrator(
            service=self._waveform_service, repository=self._waveform_repository
        )
        self._waveform_orchestrator.waveform_ready.connect(self._on_waveform_ready)
        self._waveform_orchestrator.waveform_failed.connect(self._on_waveform_failed)
        self._waveform_orchestrator.duration_known.connect(self._on_duration_known)
        self._exact_duration_by_media: dict[UUID, int] = {}

        # Every command to VLC goes through one ordered queue, off the UI thread.
        self._commands: CommandExecutor = command_executor or ThreadedCommandQueue(self)
        self._clock = PlaybackClock()
        self._loop_controller = LoopController(adapter, self._clock, executor=self._commands)
        self._loop_controller.bookmark_navigation_requested.connect(self._on_loop_navigation_requested)
        self._loop_controller.loop_completed.connect(self._on_loop_completed)
        self._loop_controller.loop_failed.connect(lambda msg: self._log.info("loop stopped: %s", msg))

        self.window = MainWindow(self._bookmark_repository, undo_stack=QUndoStack(self))

        self._current_media_id: UUID | None = None
        self._current_vlc_item_id: int | None = None  # the item DISPLAYED in the waveform/bookmark panel
        # The item VLC is actually playing right now -- tracked separately from
        # _current_vlc_item_id so a single-click "preview a different song" (see
        # _on_playlist_item_selected) can show that song's waveform/bookmarks without
        # that being overwritten on the next status poll just because playback moved on.
        self._actually_playing_vlc_item_id: int | None = None
        self._last_playback_state: str = "stopped"
        self._playlist_items: list = []
        # The last playlist snapshot, resolved: (VlcPlaylistItem, Media) pairs. Polls
        # that return an identical playlist skip all resolution and database work.
        self._resolved: list[tuple[object, object]] = []
        self._last_snapshot_key: tuple | None = None
        self._connected = False
        # True only between _on_loop_selection_requested (the waveform's own Play
        # button) and whatever ends that ad-hoc loop, so dragging the SAME selection's
        # edges live-updates what's actually looping; see _on_waveform_selection_changed.
        self._selection_loop_active = False
        self._active_loop_bookmark_id: UUID | None = None
        # Every media_id already handed to the waveform prefetcher this session.
        self._preload_requested: set[UUID] = set()
        # media_id -> display name, for the bookmark panel's "Song" column.
        self._song_names_cache: dict[UUID, str] = {}

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
        self._connect_adapter(self._adapter)
        self._status_timer.start(STATUS_POLL_MS)
        self._playlist_timer.start(PLAYLIST_POLL_MS)
        self._poll_playlist()
        self.window.show()

    # -- commands --

    def _submit(self, fn: Callable[[], object], *, fences: tuple[str, ...] = ("position",),
                on_done: Callable[[object], None] | None = None) -> None:
        """Queues one playback command (spec #108: never on the UI thread)."""
        self._commands.submit(
            fn, fences=fences, on_done=on_done,
            on_error=lambda exc: self._log.debug("command failed: %s", exc),
        )

    def _fire_and_forget(self, fn: Callable[[], object]) -> None:
        self._submit(fn)

    def _connect_adapter(self, adapter: PlaybackAdapter) -> None:
        def _connect() -> None:
            try:
                adapter.connect()
            except Exception as exc:  # noqa: BLE001 - spec #104: stay usable offline
                self._log.info("VLC connect failed (will keep retrying via polling): %s", exc)

        self._submit(_connect, fences=())

    # -- transport wiring --

    def _wire_transport(self) -> None:
        transport = self.window._transport
        transport.play_pause_clicked.connect(self._on_play_pause_clicked)
        transport.stop_clicked.connect(self._on_stop_clicked)
        transport.seek_back_clicked.connect(lambda: self._seek_relative(-5_000_000))
        transport.seek_forward_clicked.connect(lambda: self._seek_relative(5_000_000))
        transport.previous_track_clicked.connect(lambda: self._submit_adapter_call("previous_track"))
        transport.next_track_clicked.connect(lambda: self._submit_adapter_call("next_track"))
        transport.previous_bookmark_clicked.connect(self._on_previous_bookmark)
        transport.next_bookmark_clicked.connect(self._on_next_bookmark)

        self.window._waveform_scene.seek_requested.connect(self._seek_displayed)
        self.window._waveform_scene.selection_changed.connect(self._on_waveform_selection_changed)
        self.window._playlist_panel.item_double_clicked.connect(self._on_playlist_item_double_clicked)
        self.window._playlist_panel.item_selected.connect(self._on_playlist_item_selected)
        self.window._playlist_panel.follow_vlc_toggled.connect(self._on_follow_vlc_toggled)
        self.window.loop_selection_requested.connect(self._on_loop_selection_requested)
        self.window.launch_vlc_requested.connect(self.prompt_vlc_launch_dialog)
        self.window.play_bookmark_requested.connect(self._on_play_bookmark_requested)
        self.window.loop_bookmark_requested.connect(self._on_loop_bookmark_requested)
        self.window.bookmark_reorder_requested.connect(self._on_bookmark_reorder_requested)
        self.window.bookmarks_changed.connect(self._refresh_bookmark_views)
        self.window.bookmark_song_display_requested.connect(self._on_bookmark_song_display_requested)
        self.window.project_imported.connect(self._on_project_imported)
        # Playlist > Refresh (F5) used to be connected to nothing.
        self.window.playlist_refresh_requested.connect(lambda *_args: self._force_playlist_refresh())

    def _submit_adapter_call(self, method_name: str) -> None:
        adapter = self._adapter
        self._submit(lambda: getattr(adapter, method_name)())

    def _seek_relative(self, delta_us: int) -> None:
        adapter = self._adapter
        self._submit(lambda: adapter.seek_relative_us(delta_us))

    # -- launch/attach picker --
    #
    # "add button to launch vlc and a browse button to select desired playlist ...
    # option to select an open vlc instance, a drop box ... alternatively the user
    # would launch a new instance with a browse button" -- direct user request. One
    # dialog (VlcLaunchDialog) and one code path serves both bootstrap.main()'s
    # first-run prompt and this button.

    def prompt_vlc_launch_dialog(self) -> None:
        if self._settings is None:
            QMessageBox.information(self.window, "Launch VLC", "VLC integration is not available in this session.")
            return
        if self._vlc_path is None:
            QMessageBox.warning(self.window, "Launch VLC", "VLC was not found on this machine.")
            return

        instances = discover_vlc_instances(self._settings, vlc_path=self._vlc_path)
        unmanaged_running = not instances and has_unmanaged_vlc_process()
        dialog = VlcLaunchDialog(
            instances, self._launch_dialog_media_filter(), parent=self.window,
            unmanaged_vlc_running=unmanaged_running,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        choice = dialog.choice()
        if choice.mode == "attach":
            self._attach_to_vlc(choice.port, host=choice.host)
        else:
            self._launch_new_vlc(choice)

    @staticmethod
    def _launch_dialog_media_filter() -> str:
        from bookmark_studio.bootstrap import STARTUP_MEDIA_FILTER

        return STARTUP_MEDIA_FILTER

    def _attach_to_vlc(self, port: int, *, host: str = "127.0.0.1") -> None:
        assert self._settings is not None
        adapter = StandardHttpPlaybackAdapter(host, port, self._settings.bridge_token())
        self._swap_adapter(adapter, mute_on_connect=False, new_vlc_process=None)
        self._log.info("Attached to existing VLC instance on %s:%d", host, port)

    def _launch_new_vlc(self, choice: VlcLaunchChoice | list[str]) -> None:
        assert self._settings is not None and self._vlc_path is not None
        if not isinstance(choice, VlcLaunchChoice):  # older callers passed the media list
            choice = VlcLaunchChoice(mode="launch", media_paths=list(choice))
        bind_host, connect_host = platform_support.vlc_http_hosts(self._vlc_path)
        # A Windows VLC driven from WSL listens on the Windows side, where a Linux-side
        # bind test proves nothing; the connect test still catches a busy port.
        windows_vlc_from_wsl = platform_support.is_wsl() and platform_support.is_windows_executable(self._vlc_path)
        port = find_free_http_port(
            self._settings.bridge_port(), connect_host=connect_host,
            bind_host=None if windows_vlc_from_wsl else bind_host,
        )
        self._settings.add_known_vlc_port(port)
        process = launch_managed_vlc(
            self._vlc_path, choice.media_paths, http_port=port,
            http_password=self._settings.bridge_token(), http_host=bind_host,
        )
        adapter = StandardHttpPlaybackAdapter(connect_host, port, self._settings.bridge_token())
        self._swap_adapter(adapter, mute_on_connect=True, new_vlc_process=process, source_uri=choice.source_uri)
        self._vlc_port = port
        self._log.info("Launched managed VLC (pid %s) on %s:%d with %d media item(s)",
                       process.pid, connect_host, port, len(choice.media_paths))

    def _swap_adapter(self, new_adapter: PlaybackAdapter, *, mute_on_connect: bool,
                      new_vlc_process: subprocess.Popen | None, source_uri: str | None = None) -> None:
        """Retargets this whole running session at a different VLC instance -- used by
        both _attach_to_vlc and _launch_new_vlc. A previously-spawned VLC process (if
        any) is left running when attaching/re-launching: the user may still want it
        open, and closing background processes they didn't ask to close would be an
        unwelcome surprise.
        """
        self._status_timer.stop()
        self._playlist_timer.stop()
        self._commands.clear_pending()
        self._loop_controller.set_adapter(new_adapter)
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass

        self._adapter = new_adapter
        self._vlc_process = new_vlc_process
        self._vlc_port = None
        self._source_uri = source_uri
        self._mute_pending = mute_on_connect
        self._current_vlc_item_id = None
        self._actually_playing_vlc_item_id = None
        self._current_media_id = None
        self._selection_loop_active = False
        self._active_loop_bookmark_id = None
        self._preload_requested.clear()
        self._song_names_cache.clear()
        self._resolved = []
        self._last_snapshot_key = None
        self._connected = False
        self.window.set_connected(False)
        # Regression, reported live: "if i close and open a new vlc instance, it does
        # not recognize the change in playlist and applies the previous bookmarks to
        # the next playlist" -- the synchronizer must forget the old session's playlist.
        self._synchronizer.reset()

        self._connect_adapter(new_adapter)
        self._status_timer.start(STATUS_POLL_MS)
        self._playlist_timer.start(PLAYLIST_POLL_MS)
        self._poll_playlist()

    # -- play / pause / stop --

    def _on_play_pause_clicked(self) -> None:
        # Direct user report: "if i play a bookmark in loop or otherwise, then pause or
        # stop... a few seconds later, it starts playing on its own" -- the loop's
        # boundary timer must be stopped along with playback.
        adapter = self._adapter
        # The state is updated right away (not on the next poll) so a quick second
        # press toggles back instead of repeating the same command.
        if self._last_playback_state == "playing":
            self._stop_loop()
            self._submit(adapter.pause)
            self._last_playback_state = "paused"
        else:
            self._submit(adapter.play)
            self._last_playback_state = "playing"

    def _on_stop_clicked(self) -> None:
        self._stop_loop()
        self._submit(self._adapter.stop)
        self._last_playback_state = "stopped"  # a following Play/Pause must play, not pause

    def _stop_loop(self) -> None:
        self._loop_controller.stop()
        self._selection_loop_active = False
        self._active_loop_bookmark_id = None

    # -- the displayed song vs. the playing song --

    def _displayed_item(self):
        return self._resolve_playlist_item(self._current_vlc_item_id)

    def _goto_for_displayed(self) -> Callable[[], None] | None:
        """If the song on screen isn't the one VLC is playing, a callable that switches
        VLC to it (run first, in the same queued command as the seek that follows)."""
        displayed = self._current_vlc_item_id
        if displayed is None or displayed == self._actually_playing_vlc_item_id:
            return None
        adapter = self._adapter
        # Commands run in order, so from now on VLC *will* be playing `displayed`.
        # Waiting for the next status poll to say so left a window (found live, from WSL)
        # in which a second command -- e.g. looping a bookmark of the previous song --
        # skipped its own switch and ran on the wrong song.
        self._actually_playing_vlc_item_id = displayed
        return lambda: adapter.goto_item(displayed)

    def _seek_displayed(self, time_us: int) -> None:
        """Waveform click / playhead drag / bookmark navigation: seek within the song
        that's on screen. Previously this always seeked whatever VLC was playing, even
        while a different song was being previewed -- landing in the wrong song."""
        goto = self._goto_for_displayed()
        adapter = self._adapter
        if goto is not None:
            self.window._playlist_panel.set_follow_vlc(True, notify=False)

        def _seek() -> None:
            if goto is not None:
                goto()
            adapter.seek_absolute_us(time_us)

        self._submit(_seek, on_done=lambda _r: self._clock.note_seek(time_us))

    def _playlist_item_for_media(self, media_id: UUID):
        """Maps a bookmark's media_id to the live VLC playlist item that plays it, from
        the last resolved snapshot (no per-call file/database work). Prefers the item
        that's playing or displayed when the same media appears twice."""
        matches = [item for item, media in self._resolved if media.id == media_id]
        if not matches:
            return None
        for preferred in (self._actually_playing_vlc_item_id, self._current_vlc_item_id):
            for item in matches:
                if item.vlc_id == preferred:
                    return item
        return matches[0]

    def _show_item(self, item) -> None:
        """Makes `item` the displayed song (waveform, bookmarks, breadcrumb)."""
        if item.vlc_id == self._current_vlc_item_id and self._current_media_id is not None:
            return
        self._current_vlc_item_id = item.vlc_id
        self._on_current_item_changed(item.uri, _item_duration_us(item))

    def _switch_displayed_song_for_bookmark(self, item) -> None:
        """Loads the bookmark's own song into the waveform/breadcrumb/bookmark list
        immediately, instead of waiting for the async VLC command + next status poll."""
        self.window._playlist_panel.set_follow_vlc(True, notify=False)
        self._show_item(item)

    # -- selection / bookmark playback --

    def _on_loop_selection_requested(self, start_us: int, end_us: int) -> None:
        """Direct user request: "once i highlight an area and press play, i expect it to
        play the highlighted area of the song in the waveform" -- if the waveform shows
        a previewed song, VLC switches to it first (same queued command, so the seek
        can't overtake the switch)."""
        before = self._goto_for_displayed()
        if before is not None:
            self.window._playlist_panel.set_follow_vlc(True, notify=False)
        self._selection_loop_active = True
        self._active_loop_bookmark_id = None
        self._loop_controller.start(
            LoopSpec(start_us=start_us, end_us=end_us, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE),
            before=before,
        )

    def _on_waveform_selection_changed(self, selection: object) -> None:
        """Direct follow-up request: "when i press play to listen to the selection, i
        want the ability to adjust the selection by dragging the sides" -- while the
        waveform's own Play button is looping a raw selection, dragging an edge
        restarts the loop with the new bounds."""
        if not self._selection_loop_active:
            return
        if not isinstance(selection, Selection):
            self._selection_loop_active = False
            self._loop_controller.stop()
            return
        self._loop_controller.start(
            LoopSpec(start_us=selection.start_us, end_us=selection.end_us, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE)
        )

    def _on_bookmark_song_display_requested(self, bookmark_id: UUID) -> None:
        """Direct user request: "when i select a bookmarking, i want it to automatically
        select the song from the playlist above and display its waveform along with the
        bookmarks" -- previews the bookmark's song without playing anything."""
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self.window._playlist_panel.select_item(item.vlc_id)

    def _on_play_bookmark_requested(self, bookmark_id: UUID) -> None:
        """Play Bookmark / double-click on a bookmark row. A loop-enabled segment loops
        with its own settings (bookmarks default to loop-enabled); anything else is a
        one-shot seek+play. Switches VLC to the bookmark's song first when needed."""
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        if bookmark.loop_enabled and bookmark.end_us is not None:
            self._on_loop_bookmark_requested(bookmark_id)
            return
        self._stop_loop()
        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self._switch_displayed_song_for_bookmark(item)
        goto = self._goto_for_displayed() if item is not None else None
        adapter = self._adapter
        start_us = bookmark.start_us

        def _play() -> None:
            if goto is not None:
                goto()
            adapter.seek_absolute_us(start_us)
            adapter.play()

        self._submit(_play, on_done=lambda _r: self._clock.note_seek(start_us, playing=True))

    def _on_loop_bookmark_requested(self, bookmark_id: UUID) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None or bookmark.end_us is None:
            return  # a point bookmark has no range to loop

        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self._switch_displayed_song_for_bookmark(item)
        before = self._goto_for_displayed() if item is not None else None
        self._selection_loop_active = False
        self._active_loop_bookmark_id = bookmark.id
        self._loop_controller.start(
            LoopSpec(
                start_us=bookmark.start_us, end_us=bookmark.end_us,
                repeat_count=bookmark.repeat_count, gap_ms=bookmark.loop_gap_ms,
                completion_action=bookmark.completion_action,
                fade_in_ms=bookmark.fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
            ),
            before=before,
        )

    def _on_loop_completed(self, _action: object) -> None:
        self._selection_loop_active = False

    def _on_loop_navigation_requested(self, action: object) -> None:
        """A finished loop whose "After loop" setting is Next/Previous Bookmark: play the
        neighbouring bookmark of the same song (by start time) with its own settings.
        LoopController announced this but nothing listened before, so those settings
        silently did nothing."""
        current_id = self._active_loop_bookmark_id
        if action is CompletionAction.NEXT_SEGMENT_QUEUE_ITEM:
            self._log.info("Segment Queue is not available yet; continuing playback")
            return
        if current_id is None or action not in (CompletionAction.NEXT_BOOKMARK, CompletionAction.PREVIOUS_BOOKMARK):
            return
        current = self._bookmark_repository.get(current_id)
        if current is None:
            return
        siblings = sorted(
            self._bookmark_repository.list_for_context(current.playlist_id, current.media_id),
            key=lambda b: (b.start_us, str(b.id)),
        )
        ids = [b.id for b in siblings]
        if current.id not in ids:
            return
        index = ids.index(current.id) + (1 if action is CompletionAction.NEXT_BOOKMARK else -1)
        if 0 <= index < len(siblings):
            target_id = siblings[index].id
            # Deferred: we're inside LoopController's own signal emission.
            QTimer.singleShot(0, lambda: self._on_play_bookmark_requested(target_id))

    def _on_bookmark_reorder_requested(self, ordered_bookmark_ids: list) -> None:
        """Direct user request: "the row entries should also be possible to manually
        reorder them moving up/down"."""
        self._bookmark_repository.reorder(ordered_bookmark_ids)
        self._refresh_bookmark_views()

    def _current_bookmarks_sorted(self) -> list:
        if self._current_media_id is None:
            return []
        bookmarks = self._bookmark_repository.list_for_context(
            self._synchronizer.active_playlist_id, self._current_media_id
        )
        return sorted(bookmarks, key=lambda b: b.start_us)

    def _displayed_position_us(self) -> int:
        if self._current_vlc_item_id is not None and self._current_vlc_item_id == self._actually_playing_vlc_item_id:
            return self._clock.estimated_position_us()
        return self.window._waveform_scene.playhead_time_us()

    def _on_previous_bookmark(self) -> None:
        bookmarks = self._current_bookmarks_sorted()
        if not bookmarks:
            return
        current_time = self._displayed_position_us()
        candidates = [b for b in bookmarks if b.start_us < current_time - 500_000]
        target = candidates[-1] if candidates else bookmarks[-1]
        self._seek_displayed(target.start_us)

    def _on_next_bookmark(self) -> None:
        bookmarks = self._current_bookmarks_sorted()
        if not bookmarks:
            return
        current_time = self._displayed_position_us()
        candidates = [b for b in bookmarks if b.start_us > current_time + 500_000]
        target = candidates[0] if candidates else bookmarks[0]
        self._seek_displayed(target.start_us)

    def stop(self) -> None:
        """Shuts the session down (connected to QApplication.aboutToQuit)."""
        if self._stopped:
            return
        self._stopped = True
        self._status_timer.stop()
        self._playlist_timer.stop()
        # Put VLC's volume back if a fade had moved it, then let queued commands finish.
        self._loop_controller.stop(restore_volume=True)
        shutdown = getattr(self._commands, "shutdown", None)
        if shutdown is not None:
            shutdown(drain_timeout_s=1.5)
        self._waveform_orchestrator.cancel_all()
        self._waveform_orchestrator.wait_for_idle(3000)
        self._thread_pool.waitForDone(3000)
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
        # Only the VLC process this Application currently owns (spawned via
        # _launch_new_vlc, not attached to) is closed on exit. An attached-to instance
        # the user already had running, or one left behind by an earlier re-launch, is
        # deliberately left alone (see _swap_adapter's docstring).
        if self._vlc_process is not None:
            try:
                terminate_managed_vlc(self._vlc_process, self._vlc_path, self._vlc_port)
            except Exception:  # noqa: BLE001 - never block quitting
                self._log.exception("could not close the managed VLC")

    # -- polling --
    #
    # Each tick dispatches the adapter call to a QThreadPool worker instead of calling
    # it directly (spec #108). An in-flight guard skips a tick rather than queuing a
    # second overlapping call if the previous one hasn't returned yet.

    def _poll_status(self) -> None:
        if self._status_inflight:
            return
        self._status_inflight = True
        self._thread_pool.start(_CallWorker(self._adapter.get_status, self._status_signals))

    def _is_stale(self, issued_ns: int) -> bool:
        return issued_ns < self._commands.fence_ns("position") + STALE_SAMPLE_MARGIN_NS

    def _on_status_result(self, payload: object) -> None:
        self._status_inflight = False
        issued_ns, status = _unpack_sample(payload)
        try:
            if not self._connected:
                self._connected = True
                self.window.set_connected(True)
            if self._mute_pending:
                self._mute_pending = False
                adapter = self._adapter
                self._submit(lambda: adapter.set_volume(0), fences=("volume",))
            # Direct user request: "add options to fade in and fade out when playing
            # back" -- fades ramp to/from whatever the user's real volume is.
            self._loop_controller.set_target_volume(status.volume, sampled_at_ns=issued_ns)
            if self._is_stale(issued_ns):
                return  # sampled before our last seek/goto/play took effect
            self._clock.update(status)
            self._last_playback_state = status.state
            self.window._transport.set_time(status.time_us, status.duration_us)
            # Direct follow-up request: "if playback is active, then the highlight is
            # green, otherwise, blue".
            self.window._playlist_panel.set_current_playing(
                status.current_playlist_item_id, is_playing=status.state == "playing"
            )
            # The playhead shows the playing song's position only while that song is
            # the one on screen; a previewed song keeps its own playhead.
            if self._current_vlc_item_id is not None and self._current_vlc_item_id == status.current_playlist_item_id:
                self.window._waveform_scene.set_playhead_time_us(status.time_us)
                self.window._waveform_view.follow_playhead(status.time_us)
            # The core boundary-timer loop mechanism deliberately does NOT compare
            # LoopController.state against status.state (an earlier attempt misfired
            # and killed ordinary loops). A stop issued entirely outside this app
            # (VLC's own window) is a known, accepted gap.
            self._loop_controller.on_tick()

            if status.current_playlist_item_id != self._actually_playing_vlc_item_id:
                self._actually_playing_vlc_item_id = status.current_playlist_item_id
                self._apply_actually_playing_item_if_following(status)
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            self._log.exception("status result handling failed")

    def _apply_actually_playing_item_if_following(self, status) -> None:
        if not self.window._playlist_panel.follow_vlc_enabled():
            return  # user is previewing a different song -- see _on_playlist_item_selected
        item = self._resolve_playlist_item(status.current_playlist_item_id)
        if item is not None and item.uri:
            self._show_item(item)
            return
        # StandardHttpPlaybackAdapter.get_status()'s media_uri is often a bare filename
        # (status.json "meta.filename"), not a resolvable URI -- only use it when the
        # playlist hasn't been polled yet at all.
        if status.media_uri:
            self._current_vlc_item_id = status.current_playlist_item_id
            self._on_current_item_changed(status.media_uri, status.duration_us)

    def _on_playlist_item_double_clicked(self, vlc_id: int) -> None:
        """"when i double click a song, i want it to play the song" -- from the start
        ("it should start playing it from the beginning, to the end"), stopping any
        active loop, with the view following it."""
        self._stop_loop()
        item = self._resolve_playlist_item(vlc_id)
        self.window._playlist_panel.set_follow_vlc(True, notify=False)
        if item is not None and item.uri:
            self._show_item(item)
        self._actually_playing_vlc_item_id = vlc_id  # see _goto_for_displayed
        adapter = self._adapter

        def _play_from_start() -> None:
            adapter.goto_item(vlc_id)
            adapter.seek_absolute_us(0)
            adapter.play()

        self._submit(_play_from_start, on_done=lambda _r: self._clock.note_seek(0, playing=True))

    def _on_playlist_item_selected(self, vlc_id: int) -> None:
        """Direct user request: "when a user clicks through the songs, [the waveform]
        is instantly visible" -- a single click previews that song's waveform/
        bookmarks without commanding VLC; double-click is the separate "start playing"
        action. Previewing a song other than the playing one switches "Follow" off."""
        item = self._resolve_playlist_item(vlc_id)
        if item is None or not item.uri:
            return
        if vlc_id != self._actually_playing_vlc_item_id:
            self.window._playlist_panel.set_follow_vlc(False)
        self._show_item(item)

    def _on_follow_vlc_toggled(self, enabled: bool) -> None:
        if not enabled or self._actually_playing_vlc_item_id is None:
            return
        if self._actually_playing_vlc_item_id == self._current_vlc_item_id:
            return  # already showing the actually-playing track
        item = self._resolve_playlist_item(self._actually_playing_vlc_item_id)
        if item is None or not item.uri:
            return
        self._show_item(item)

    def _resolve_playlist_item(self, vlc_id: int | None):
        if vlc_id is None:
            return None
        return next((i for i in self._playlist_items if i.vlc_id == vlc_id), None)

    def _resolve_current_item_uri(self, vlc_id: int | None) -> str | None:
        item = self._resolve_playlist_item(vlc_id)
        return item.uri if item is not None else None

    def _on_status_failed(self, message: str) -> None:
        self._status_inflight = False
        if self._connected:
            self._connected = False
            self.window.set_connected(False)
        self._log.debug("status poll failed: %s", message)  # spec #104: never crash the UI

    def _poll_playlist(self) -> None:
        if self._playlist_inflight:
            return
        self._playlist_inflight = True
        self._thread_pool.start(_CallWorker(self._adapter.get_playlist, self._playlist_signals))

    def _force_playlist_refresh(self) -> None:
        """Playlist > Refresh (F5): re-resolve and re-recognise on the next poll."""
        self._last_snapshot_key = None
        self._poll_playlist()

    def _on_playlist_result(self, payload: object) -> None:
        self._playlist_inflight = False
        _issued_ns, items = _unpack_sample(payload)
        if self._asking_playlist_match:
            return  # a "same playlist?" question is open; decide after the answer
        try:
            items = list(items)
            self._playlist_items = items
            snapshot_key = tuple((i.vlc_id, i.uri, i.name, i.duration_s) for i in items)
            if snapshot_key == self._last_snapshot_key:
                return  # unchanged playlist: nothing to resolve, sync or redraw
            resolved: list[tuple[object, object]] = []
            for item in items:
                if not item.uri:
                    continue
                try:
                    resolved.append((item, self._media_resolver.resolve(item.uri)))
                except Exception:  # noqa: BLE001
                    # One unresolvable item (bad path, permissions, an exotic filename)
                    # must not hide every OTHER item from the playlist panel.
                    self._log.exception("failed to resolve playlist item %r", item.uri)
            self._resolved = resolved

            if not resolved:
                self.window._playlist_panel.set_playlist([], {})
                self._last_snapshot_key = snapshot_key
                return

            ordered_media_ids = [media.id for _item, media in resolved]
            result = self._synchronizer.on_snapshot(source_uri=self._source_uri, ordered_media_ids=ordered_media_ids)
            self._log.debug("playlist sync: %s -> %s", result.action, result.playlist_id)
            if result.action == SyncAction.ASK_USER:
                self._resolve_ask_user(result, ordered_media_ids)

            for item, media in resolved:
                self._song_names_cache[media.id] = media.title or media.filename or item.uri
            self._push_exact_durations()
            self._refresh_bookmark_views()
            self._preload_playlist_waveforms(resolved)
            self._last_snapshot_key = snapshot_key
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            self._log.exception("playlist result handling failed")

    # -- playlist recognition: "is this the same playlist as before?" (spec #12) --

    def _resolve_ask_user(self, result, ordered_media_ids: list[UUID]) -> None:
        candidate_id = result.candidate_id
        key = (candidate_id, tuple(ordered_media_ids))
        accept = False
        if candidate_id is not None and key not in self._declined_matches:
            record = self._playlist_repository.get(candidate_id)
            name = record.playlist.name if record is not None else "a previous playlist"
            self._asking_playlist_match = True
            try:
                accept = bool(self._ask_playlist_match(name, float(result.candidate_score or 0.0)))
            finally:
                self._asking_playlist_match = False
        if accept and candidate_id is not None:
            self._synchronizer.accept_ask_user_match(candidate_id, ordered_media_ids)
        else:
            self._declined_matches.add(key)
            self._synchronizer.create_ad_hoc(source_uri=self._source_uri, ordered_media_ids=ordered_media_ids)

    def _ask_playlist_match_dialog(self, playlist_name: str, score: float) -> bool:
        answer = QMessageBox.question(
            self.window, "Same playlist?",
            f"The VLC playlist looks like “{playlist_name}” ({score:.0%} similar), "
            "but it has changed.\n\nUse that playlist's bookmarks?",
        )
        return answer == QMessageBox.Yes

    def _on_project_imported(self) -> None:
        """An import may have added the signatures the live playlist should match."""
        self._synchronizer.reset()
        self._force_playlist_refresh()

    # -- bookmark views --

    def _refresh_bookmark_views(self) -> None:
        """Pushes every playlist-scoped bookmark across the WHOLE playlist into the
        bookmark list panel ("the bookmarks should all be listed for all songs") and the
        per-song counts into the playlist panel. Runs when the playlist changes and
        after any bookmark edit -- both panels ignore refreshes that change nothing."""
        playlist_id = self._synchronizer.active_playlist_id
        counts: dict[int, int] = {}
        if playlist_id is not None:
            bookmarks = self._bookmark_repository.list_for_playlist(playlist_id)
            per_media: dict[UUID, int] = {}
            for bookmark in bookmarks:
                per_media[bookmark.media_id] = per_media.get(bookmark.media_id, 0) + 1
            for item, media in self._resolved:
                counts[item.vlc_id] = per_media.get(media.id, 0)
            self.window.load_all_bookmarks(bookmarks, dict(self._song_names_cache))
        else:
            for item, media in self._resolved:
                counts[item.vlc_id] = len(self._bookmark_repository.list_global_for_media(media.id))
            self.window.load_all_bookmarks([], {})
        self.window._playlist_panel.set_playlist([item for item, _media in self._resolved], counts)

    # Kept under its old name: tests and older callers refer to it.
    _refresh_bookmark_panel = _refresh_bookmark_views

    def _preload_playlist_waveforms(self, resolved: list) -> None:
        """Makes sure every track in the playlist has a cached waveform ("make the tool
        preload the waves for faster operation"), without loading cached ones -- see
        WaveformOrchestrator.prefetch."""
        for item, media in resolved:
            if media.id in self._preload_requested or not media.fast_fingerprint:
                continue
            local_path = uri_to_local_path(media.canonical_uri or item.uri)
            if local_path is None or not local_path.exists():
                continue
            self._preload_requested.add(media.id)
            self._waveform_orchestrator.prefetch(media.id, media.fast_fingerprint, str(local_path))

    def _on_playlist_failed(self, message: str) -> None:
        self._playlist_inflight = False
        self._log.debug("playlist poll failed: %s", message)

    # -- reactions --

    def _on_current_item_changed(self, media_uri: str | None, duration_us: int | None) -> None:
        if not media_uri:
            return
        media = self._media_resolver.resolve(media_uri, duration_us=duration_us)
        self._current_media_id = media.id
        # A drag-selection is a pair of raw offsets with nothing tying it to a track --
        # reported live as "the paint is not song specific". Cleared on every switch.
        self.window._waveform_scene.clear_selection()

        playlist_id = self._synchronizer.active_playlist_id
        playlist_name = "Unknown playlist"
        if playlist_id is not None:
            record = self._playlist_repository.get(playlist_id)
            if record is not None:
                playlist_name = record.playlist.name

        bookmarks = self._bookmark_repository.list_for_context(playlist_id, media.id)
        exact = self._exact_duration_by_media.get(media.id)
        self.window.set_context(
            playlist_name=playlist_name,
            track_name=media.title or media.filename or media_uri,
            playlist_id=playlist_id,
            media_id=media.id,
            bookmark_count=len(bookmarks),
            duration_us=exact or (duration_us if duration_us and duration_us > 0 else None) or media.duration_us or 0,
        )
        self.window.load_bookmarks(bookmarks)
        playing_here = self._current_vlc_item_id is not None and self._current_vlc_item_id == self._actually_playing_vlc_item_id
        self.window._waveform_scene.set_playhead_time_us(self._clock.estimated_position_us() if playing_here else 0)
        # Without this, the view stays at its raw 1ms-per-pixel default zoom, so a
        # multi-minute track shows only its first fraction of a second.
        self.window._waveform_view.fit_entire_media()

        local_path = uri_to_local_path(media.canonical_uri or media_uri)
        if local_path is not None and local_path.exists() and media.fast_fingerprint:
            self._waveform_orchestrator.request(media.id, media.fast_fingerprint, str(local_path))

    def _on_waveform_ready(self, media_id: UUID, pyramid) -> None:
        if media_id != self._current_media_id:
            return  # switched tracks again before this one finished (spec #65)
        # The decoded length is exact; VLC's is whole seconds (the waveform used to be
        # clipped to it).
        duration_us = pyramid.duration_us or self.window._waveform_scene._duration_us
        self.window._waveform_scene.set_waveform(pyramid, duration_us)
        if self.window._waveform_view._fit_mode:
            self.window._waveform_view.fit_entire_media()  # keep "fit" exact; a manual zoom is left alone

    def _on_duration_known(self, media_id: UUID, duration_us: int) -> None:
        """The exact decoded length of a track (VLC's HTTP interface only reports whole
        seconds): handed to the adapter so its position<->time conversions and seeks
        line up with the waveform."""
        if not duration_us or duration_us <= 0:
            return
        self._exact_duration_by_media[media_id] = int(duration_us)
        for item, media in self._resolved:
            if media.id == media_id:
                self._adapter.set_exact_duration(item.vlc_id, int(duration_us))

    def _push_exact_durations(self) -> None:
        for item, media in self._resolved:
            duration = self._exact_duration_by_media.get(media.id)
            if duration:
                self._adapter.set_exact_duration(item.vlc_id, duration)

    def _on_waveform_failed(self, media_id: UUID, message: str) -> None:
        # A decode failure (bad/missing ffmpeg, an unreadable file, an unsupported codec)
        # would otherwise leave the waveform lane silently blank.
        self._log.info("waveform generation failed for media %s: %s", media_id, message)

    def _list_ordered_media_ids_for_playlist(self, playlist_id: UUID) -> list[UUID]:
        return self._playlist_repository.list_item_media_ids(playlist_id)


def _item_duration_us(item) -> int | None:
    duration_s = getattr(item, "duration_s", None)
    if duration_s is None or duration_s <= 0:
        return None
    return int(duration_s * 1_000_000)


def _uri_to_path(uri: str) -> Path | None:
    return uri_to_local_path(uri)
