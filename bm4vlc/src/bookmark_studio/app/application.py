"""Application composition root: wires repositories, playback adapter, and UI (spec
#114). Owns the live polling loop that connects a PlaybackAdapter to the rest of the
app -- this is the piece spec #178-#180 describe as the startup/song-change/
playlist-change sequences.
"""
from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path
from uuid import UUID

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QDialog, QMessageBox

from bookmark_studio.app.vlc_launcher import (
    discover_vlc_instances, find_free_http_port, has_unmanaged_vlc_process, launch_managed_vlc,
)
from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.media.resolver import MediaResolver
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.persistence.waveform_repository import WaveformCacheRepository
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playlist.recognition import PlaylistRecognitionService
from bookmark_studio.playlist.synchronizer import PlaylistSynchronizer
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchDialog
from bookmark_studio.ui.main_window import MainWindow
from bookmark_studio.waveform.service import WaveformService

# spec #32 suggests 100-200ms status / 500-1000ms playlist. Verified live these are too
# aggressive for VLC's real Lua httpd: every request that doesn't get a response within
# its timeout forces BridgeClient to reconnect, and each reconnect leaks a socket on
# VLC's side (see bridge_client.py's module docstring -- VLC's httpd never closes its
# end). At spec's suggested cadence, a live session degraded from "connects fine" to
# "completely unresponsive" within about 15-20 seconds of normal use. Slower polling
# directly cuts total request volume and, with it, the absolute leak rate -- this is a
# mitigation that meaningfully extends real session lifetime, not a fix for the
# underlying VLC-side leak, which nothing on this side of the socket can eliminate.
STATUS_POLL_MS = 400
PLAYLIST_POLL_MS = 2000


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
        mute_on_connect: bool = False,
        settings: SettingsService | None = None,
        vlc_path: str | None = None,
        vlc_process: subprocess.Popen | None = None,
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
        # instance). None outside of a real bootstrap.main() run (e.g. in tests that
        # construct Application directly) simply disables that picker.
        self._settings = settings
        self._vlc_path = vlc_path
        self._vlc_process = vlc_process
        # Only true when this Application spawned its own VLC process (see
        # vlc_launcher.launch_managed_vlc): forcing volume to 0 on an existing VLC the
        # user already had open with their own volume set would be an unwelcome
        # surprise. --start-paused (see vlc_launcher._COMMON_ARGS) already stops it
        # from autoplaying; this handles the "muted" half of that same request. Sent
        # once, on the first successful status poll, since the HTTP interface takes a
        # moment to come up after the process starts (same self-healing pattern as the
        # rest of this polling loop -- see STATUS_POLL_MS comment above).
        self._mute_pending = mute_on_connect

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
        self._waveform_orchestrator.waveform_failed.connect(self._on_waveform_failed)

        self._clock = PlaybackClock()
        self._loop_controller = LoopController(adapter, self._clock)

        self.window = MainWindow(self._bookmark_repository, undo_stack=QUndoStack(self))

        self._current_media_id: UUID | None = None
        self._current_vlc_item_id: int | None = None  # the item DISPLAYED in the waveform/bookmark panel
        # The item VLC is actually playing right now -- tracked separately from
        # _current_vlc_item_id so a single-click "preview a different song" (see
        # _on_playlist_item_selected) can show that song's waveform/bookmarks without
        # that being overwritten on the next status poll just because playback moved
        # on. Direct user request: "when a user clicks through the songs, it is
        # instantly visible" (click != play, matching "if the user double clicks ...
        # it would automatically start playing" as the distinct play action).
        self._actually_playing_vlc_item_id: int | None = None
        self._last_playback_state: str = "stopped"
        self._playlist_items: list = []
        self._connected = False
        # Every media_id ever handed to the waveform orchestrator this session, current
        # track or not -- see _preload_playlist_waveforms. Prevents re-dispatching a
        # decode job on every ~2s playlist poll once a track has already been
        # requested once (the orchestrator's own cache lookup would just no-op a
        # repeat request, but skipping it here avoids the pointless dispatch/lookup).
        self._preload_requested: set[UUID] = set()
        # media_id -> display name, built up as _on_playlist_result resolves each
        # item -- reused by _refresh_bookmark_panel so it doesn't need its own extra
        # per-media lookups just to label the bookmark panel's "Song" column.
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
        self.window._playlist_panel.item_double_clicked.connect(self._on_playlist_item_double_clicked)
        self.window._playlist_panel.item_selected.connect(self._on_playlist_item_selected)
        self.window._playlist_panel.follow_vlc_toggled.connect(self._on_follow_vlc_toggled)
        self.window.play_selection_requested.connect(self._on_play_selection_requested)
        self.window.loop_selection_requested.connect(self._on_loop_selection_requested)
        self.window.launch_vlc_requested.connect(self.prompt_vlc_launch_dialog)
        self.window.play_bookmark_requested.connect(self._on_play_bookmark_requested)
        self.window.loop_bookmark_requested.connect(self._on_loop_bookmark_requested)
        self.window.bookmark_reorder_requested.connect(self._on_bookmark_reorder_requested)
        self.window.bookmarks_changed.connect(self._refresh_bookmark_panel)

    # -- launch/attach picker --
    #
    # "add button to launch vlc and a browse button to select desired playlist ...
    # option to select an open vlc instance, a drop box ... alternatively the user
    # would launch a new instance with a browse button" -- direct user request. One
    # dialog (VlcLaunchDialog) and one code path serves both bootstrap.main()'s
    # first-run prompt and this button, so "launch a new instance" behaves identically
    # whether it's the very first thing that happens or a mid-session re-launch.

    def prompt_vlc_launch_dialog(self) -> None:
        if self._settings is None:
            QMessageBox.information(self.window, "Launch VLC", "VLC integration is not available in this session.")
            return
        if self._vlc_path is None:
            QMessageBox.warning(self.window, "Launch VLC", "VLC was not found on this machine.")
            return

        instances = discover_vlc_instances(self._settings)
        unmanaged_running = not instances and has_unmanaged_vlc_process()
        dialog = VlcLaunchDialog(
            instances, self._launch_dialog_media_filter(), parent=self.window,
            unmanaged_vlc_running=unmanaged_running,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        choice = dialog.choice()
        if choice.mode == "attach":
            self._attach_to_vlc(choice.port)
        else:
            self._launch_new_vlc(choice.media_paths)

    @staticmethod
    def _launch_dialog_media_filter() -> str:
        from bookmark_studio.bootstrap import STARTUP_MEDIA_FILTER

        return STARTUP_MEDIA_FILTER

    def _attach_to_vlc(self, port: int) -> None:
        assert self._settings is not None
        adapter = StandardHttpPlaybackAdapter("127.0.0.1", port, self._settings.bridge_token())
        self._swap_adapter(adapter, mute_on_connect=False, new_vlc_process=None)
        self._log.info("Attached to existing VLC instance on port %d", port)

    def _launch_new_vlc(self, media_paths: list[str]) -> None:
        assert self._settings is not None and self._vlc_path is not None
        port = find_free_http_port(self._settings.bridge_port())
        self._settings.add_known_vlc_port(port)
        process = launch_managed_vlc(
            self._vlc_path, media_paths, http_port=port, http_password=self._settings.bridge_token()
        )
        adapter = StandardHttpPlaybackAdapter("127.0.0.1", port, self._settings.bridge_token())
        self._swap_adapter(adapter, mute_on_connect=True, new_vlc_process=process)
        self._log.info("Launched managed VLC (pid %s) on port %d with %d media item(s)",
                        process.pid, port, len(media_paths))

    def _swap_adapter(self, new_adapter: PlaybackAdapter, *, mute_on_connect: bool,
                       new_vlc_process: subprocess.Popen | None) -> None:
        """Retargets this whole running session at a different VLC instance -- used by
        both _attach_to_vlc and _launch_new_vlc. A previously-spawned VLC process (if
        any) is left running when attaching/re-launching: the user may still want it
        open, and closing background processes they didn't ask to close would be an
        unwelcome surprise (see the top-level safety rules on hard-to-reverse actions).
        """
        self._status_timer.stop()
        self._playlist_timer.stop()
        try:
            self._adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass

        self._adapter = new_adapter
        self._vlc_process = new_vlc_process
        self._loop_controller.set_adapter(new_adapter)
        self._mute_pending = mute_on_connect
        self._current_vlc_item_id = None
        self._actually_playing_vlc_item_id = None
        self._current_media_id = None
        self._preload_requested.clear()
        self._song_names_cache.clear()
        self._connected = False
        self.window._transport.set_connected(False)
        # Regression, reported live: "if i close and open a new vlc instance, it does
        # not recognize the change in playlist and applies the previous bookmarks to
        # the next playlist". PlaylistSynchronizer.reset() exists specifically for
        # this ("Forces full recognition on the next snapshot (e.g. VLC restarted,
        # spec #105)") but was never actually called anywhere -- without it,
        # active_playlist_id stayed pointed at the OLD session's playlist, and every
        # bookmark shown/created afterward was scoped to that stale id instead of
        # whatever playlist the new VLC instance is actually playing.
        self._synchronizer.reset()

        try:
            new_adapter.connect()
        except Exception as exc:  # noqa: BLE001 - spec #104: self-heals via polling
            self._log.info("Initial connect after adapter switch failed (will retry via polling): %s", exc)

        self._status_timer.start(STATUS_POLL_MS)
        self._playlist_timer.start(PLAYLIST_POLL_MS)
        self._poll_playlist()

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

    def _playlist_item_for_media(self, media_id: UUID):
        """Maps a bookmark's media_id back to the live VLC playlist item that plays
        it, by resolving each currently-known playlist item's URI the same way
        _on_playlist_result already does (MediaResolver.resolve is idempotent -- an
        already-resolved URI just returns the existing record, no new DB writes).
        """
        for item in self._playlist_items:
            if not item.uri:
                continue
            try:
                media = self._media_resolver.resolve(item.uri)
            except Exception:  # noqa: BLE001 - best-effort lookup, see callers
                continue
            if media.id == media_id:
                return item
        return None

    def _switch_displayed_song_for_bookmark(self, item) -> None:
        """Loads the bookmark's own song into the waveform/breadcrumb/bookmark list
        immediately, instead of waiting for the async VLC command + next status poll
        to come back around. Regression, reported live: "when selecting a bookmarked
        sample to play, the original song and the waveform are not loaded at the
        same time" -- Play/Loop Bookmark only ever told VLC to switch tracks and left
        the waveform to catch up on its own via the ~400ms status poll (and not at
        all if "Follow currently playing VLC song" had been switched off by a
        previous preview click -- see _on_playlist_item_selected). Mirrors the same
        fix already applied to double-click-to-play in the playlist.
        """
        self.window._playlist_panel.set_follow_vlc(True)
        self._current_vlc_item_id = item.vlc_id
        duration_us = int(item.duration_s * 1_000_000) if item.duration_s is not None else None
        self._on_current_item_changed(item.uri, duration_us)

    def _on_play_bookmark_requested(self, bookmark_id: UUID) -> None:
        """"another set below that would play explicitly from the bookmark listing
        itself" -- distinct from Play/Loop Selection (which needs a fresh waveform
        drag) and from the transport bar (which drives the live VLC playlist, not a
        specific saved bookmark).

        Regression, reported live now that the bookmark list spans every song: this
        only ever seeked *whatever VLC currently had loaded*, never switching to the
        bookmark's own song first -- so playing a bookmark that belonged to a
        different song than whatever was already playing silently landed at the
        bookmark's start_us offset inside the WRONG track. goto_item() (VLC's
        pl_play&id=<X>) switches song and starts playing before the seek lands.
        """
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self._switch_displayed_song_for_bookmark(item)
        vlc_id = item.vlc_id if item is not None else None

        def _play() -> None:
            if vlc_id is not None:
                self._adapter.goto_item(vlc_id)
            self._adapter.seek_absolute_us(bookmark.start_us)
            self._adapter.play()

        self._fire_and_forget(_play)

    def _on_loop_bookmark_requested(self, bookmark_id: UUID) -> None:
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None or bookmark.end_us is None:
            return  # a point bookmark has no range to loop

        # Same cross-song fix as _on_play_bookmark_requested. Switches synchronously
        # (not via _fire_and_forget) because LoopController.start() itself already
        # runs its seek/play synchronously on the calling (main) thread -- matches
        # the codebase's existing behavior for _on_loop_selection_requested, and
        # guarantees the song switch has actually landed before the loop's own seek.
        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self._switch_displayed_song_for_bookmark(item)
            self._adapter.goto_item(item.vlc_id)

        from bookmark_studio.domain.loop import LoopSpec

        self._loop_controller.start(
            LoopSpec(
                start_us=bookmark.start_us, end_us=bookmark.end_us,
                repeat_count=bookmark.repeat_count, gap_ms=bookmark.loop_gap_ms,
                completion_action=bookmark.completion_action,
            )
        )

    def _on_bookmark_reorder_requested(self, ordered_bookmark_ids: list) -> None:
        """Direct user request: "the row entries should also be possible to manually
        reorder them moving up/down"."""
        self._bookmark_repository.reorder(ordered_bookmark_ids)
        self._refresh_bookmark_panel()

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
        # Only the VLC process this Application currently owns (spawned via
        # _launch_new_vlc, not attached to) is torn down on app exit -- matches the
        # original bootstrap.main() behavior. An attached-to instance the user already
        # had running, or one left behind by a since-superseded _swap_adapter call, is
        # deliberately left alone (see _swap_adapter's docstring).
        if self._vlc_process is not None and self._vlc_process.poll() is None:
            self._vlc_process.terminate()

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
        try:
            if not self._connected:
                self._connected = True
                self.window._transport.set_connected(True)
            if self._mute_pending:
                self._mute_pending = False
                self._fire_and_forget(lambda: self._adapter.set_volume(0))
            self._clock.update(status)
            self._last_playback_state = status.state
            self.window._waveform_scene.set_playhead_time_us(status.time_us)
            self.window._waveform_view.follow_playhead(status.time_us)
            self.window._transport.set_time(status.time_us, status.duration_us)
            self.window._playlist_panel.set_current_playing(status.current_playlist_item_id)
            self._loop_controller.on_tick()

            if status.current_playlist_item_id != self._actually_playing_vlc_item_id:
                self._actually_playing_vlc_item_id = status.current_playlist_item_id
                self._apply_actually_playing_item_if_following(status)
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            # A bare `except: pass` here would be exactly the kind of silent failure
            # that made a real bug (see _on_playlist_result) invisible for an entire
            # debugging session -- log the full traceback, don't swallow it quietly.
            self._log.exception("status result handling failed")

    def _apply_actually_playing_item_if_following(self, status) -> None:
        if not self.window._playlist_panel.follow_vlc_enabled():
            return  # user is previewing a different song -- see _on_playlist_item_selected
        self._current_vlc_item_id = status.current_playlist_item_id
        # StandardHttpPlaybackAdapter.get_status()'s media_uri comes from VLC's
        # status.json "meta.filename" (see http_fallback._extract_media_uri) -- a bare
        # filename, NOT a resolvable file:// URI. Resolving identity from it
        # (MediaResolver.resolve) silently created a SECOND, disconnected Media row
        # every track change: not matched by canonical_uri to the correctly-resolved
        # playlist entry, and un-fingerprintable (uri_to_local_path returns None for a
        # bare filename), so _preload_playlist_waveforms' correctly-decoded-and-cached
        # pyramid arrived under the *other* media_id and got dropped by
        # _on_waveform_ready's `!= self._current_media_id` check -- confirmed live,
        # this is why "each song's visual waveform" never appeared. It also meant
        # bookmarks created while "current" pointed at this phantom id wouldn't line
        # up with the playlist panel's bookmark counts. The playlist poll
        # (_on_playlist_result, run at most 2s ago) already resolved every item's real
        # URI correctly -- look the current one up there instead, and only fall back
        # to the unreliable status field if the playlist hasn't been polled yet at all.
        media_uri = self._resolve_current_item_uri(status.current_playlist_item_id) or status.media_uri
        self._on_current_item_changed(media_uri, status.duration_us)

    def _on_playlist_item_double_clicked(self, vlc_id: int) -> None:
        """"when i double click a song, i want it to play the song" -- goto_item()
        already sends VLC's pl_play&id=<X>, which does start playback; what actually
        broke the visible feedback is that Qt fires a single-click selection-changed
        event as the first half of a double-click, which ran _on_playlist_item_selected
        and switched off "Follow currently playing VLC song" (see that method) a
        moment before this handler runs -- so the waveform/breadcrumb silently kept
        showing the just-clicked song's PREVIEW instead of advancing to reflect actual
        playback, reading as "double-click doesn't play". Explicitly re-enabling
        follow here means a double-click always ends up watching the track it just
        told VLC to play, even though the status poll that confirms the switch
        happened arrives a moment later.
        """
        self.window._playlist_panel.set_follow_vlc(True)
        self._fire_and_forget(lambda: self._adapter.goto_item(vlc_id))

    def _on_playlist_item_selected(self, vlc_id: int) -> None:
        """Direct user request: "when a user clicks through the songs, [the waveform]
        is instantly visible" -- a single click previews that song's waveform/
        bookmarks (already preloaded, see _preload_playlist_waveforms) without
        commanding VLC to change what it's actually playing; double-click (already
        wired to goto_item) is the separate, explicit "start playing" action. If the
        previewed song isn't the one actually playing, "Follow currently playing VLC
        song" is switched off so the next status poll doesn't yank the view back to
        the live track -- re-checking it snaps back via _on_follow_vlc_toggled.
        """
        item = self._resolve_playlist_item(vlc_id)
        if item is None or not item.uri:
            return
        if vlc_id != self._actually_playing_vlc_item_id:
            self.window._playlist_panel.set_follow_vlc(False)
        self._current_vlc_item_id = vlc_id
        duration_us = int(item.duration_s * 1_000_000) if item.duration_s is not None else None
        self._on_current_item_changed(item.uri, duration_us)

    def _on_follow_vlc_toggled(self, enabled: bool) -> None:
        if not enabled or self._actually_playing_vlc_item_id is None:
            return
        if self._actually_playing_vlc_item_id == self._current_vlc_item_id:
            return  # already showing the actually-playing track
        item = self._resolve_playlist_item(self._actually_playing_vlc_item_id)
        if item is None or not item.uri:
            return
        self._current_vlc_item_id = self._actually_playing_vlc_item_id
        duration_us = int(item.duration_s * 1_000_000) if item.duration_s is not None else None
        self._on_current_item_changed(item.uri, duration_us)

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
            self.window._transport.set_connected(False)
        self._log.debug("status poll failed: %s", message)  # spec #104: never crash the UI

    def _poll_playlist(self) -> None:
        if self._playlist_inflight:
            return
        self._playlist_inflight = True
        self._thread_pool.start(_CallWorker(self._adapter.get_playlist, self._playlist_signals))

    def _on_playlist_result(self, items: object) -> None:
        self._playlist_inflight = False
        try:
            self._playlist_items = list(items)
            resolved: list[tuple[object, object]] = []
            for item in items:
                if not item.uri:
                    continue
                try:
                    resolved.append((item, self._media_resolver.resolve(item.uri)))
                except Exception:  # noqa: BLE001
                    # One unresolvable item (bad path, permissions, an exotic
                    # filename) must not hide every OTHER item from the playlist
                    # panel -- an earlier version let a single failure here abort
                    # the whole list comprehension, so a newly added song with a
                    # problem silently made the panel stop updating at all, forever
                    # (every subsequent poll hit the exact same failure).
                    self._log.exception("failed to resolve playlist item %r", item.uri)

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
                self._song_names_cache[media.id] = media.title or media.filename or item.uri
            self.window._playlist_panel.set_playlist([item for item, _media in resolved], bookmark_counts)
            self._preload_playlist_waveforms(resolved)
            self._refresh_bookmark_panel()
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            self._log.exception("playlist result handling failed")

    def _refresh_bookmark_panel(self) -> None:
        """Pushes every playlist-scoped bookmark across the WHOLE playlist into the
        bookmark list panel -- direct user request: "the bookmarks should all be
        listed for all songs", not just the one currently displayed (that's still
        all load_bookmarks/the waveform ever shows). Called from the ~2s playlist
        poll and immediately after any bookmark mutation (MainWindow.bookmarks_changed)
        or manual reorder, so the list doesn't lag behind edits by a whole poll cycle.
        """
        playlist_id = self._synchronizer.active_playlist_id
        if playlist_id is None:
            self.window.load_all_bookmarks([], {})
            return
        bookmarks = self._bookmark_repository.list_for_playlist(playlist_id)
        self.window.load_all_bookmarks(bookmarks, dict(self._song_names_cache))

    def _preload_playlist_waveforms(self, resolved: list) -> None:
        """Kicks off background decoding for every track in the playlist, not just the
        one currently playing -- direct fix for "make the tool preload the waves for
        faster operation": previously a waveform was only ever requested reactively,
        in _on_current_item_changed, the moment a track actually started playing, so
        switching to a not-yet-visited track always paid the full ffmpeg decode
        latency live. WaveformOrchestrator.request() already de-dupes against its own
        disk cache (see waveform_orchestrator.py), so this is safe to call for tracks
        that were already decoded in a previous session -- those resolve instantly
        from cache and cost nothing.
        """
        for item, media in resolved:
            if media.id in self._preload_requested or not media.fast_fingerprint:
                continue
            local_path = _uri_to_path(media.canonical_uri or item.uri)
            if local_path is None or not local_path.exists():
                continue
            self._preload_requested.add(media.id)
            self._waveform_orchestrator.request(media.id, media.fast_fingerprint, str(local_path))

    def _on_playlist_failed(self, message: str) -> None:
        self._playlist_inflight = False
        self._log.debug("playlist poll failed: %s", message)

    # -- reactions --

    def _on_current_item_changed(self, media_uri: str | None, duration_us: int | None) -> None:
        if not media_uri:
            return
        media = self._media_resolver.resolve(media_uri, duration_us=duration_us)
        self._current_media_id = media.id
        # A drag-selection is just a pair of raw microsecond offsets on the waveform's
        # x-axis, with nothing tying it to a particular track -- reported live as
        # "if i paint an area ... and then swap to another song, the paint is not song
        # specific and ends up showing up on other songs". Worse than a cosmetic
        # leftover: with Bookmark Now/Bookmark Selection now preferring an active
        # selection (see main_window._on_bookmark_now_clicked), a stale selection left
        # over from a previous track could be committed as a bookmark on the NEW
        # track at meaningless start/end times. Clearing it on every track switch --
        # actual playback change, single-click preview, or re-enabling follow -- is
        # what makes a selection actually song-specific.
        self.window._waveform_scene.clear_selection()

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
        # Without this, the view stays at its raw 1ms-per-pixel default zoom, so a
        # multi-minute track shows only its first fraction of a second -- everything
        # else (waveform peaks, the playhead, most bookmarks) is simply off-screen,
        # not actually broken. Confirmed live: reported as "the waveform doesn't show
        # up" / "no moving bar" when the real cause was zoom, not missing rendering.
        self.window._waveform_view.fit_entire_media()

        local_path = _uri_to_path(media.canonical_uri or media_uri)
        if local_path is not None and local_path.exists() and media.fast_fingerprint:
            self._waveform_orchestrator.request(media.id, media.fast_fingerprint, str(local_path))

    def _on_waveform_ready(self, media_id: UUID, pyramid) -> None:
        if media_id != self._current_media_id:
            return  # switched tracks again before this one finished (spec #65)
        duration_us = self.window._waveform_scene._duration_us
        self.window._waveform_scene.set_waveform(pyramid, duration_us)

    def _on_waveform_failed(self, media_id: UUID, message: str) -> None:
        # Previously connected to nothing at all -- a decode failure (bad/missing
        # ffmpeg, an unreadable file, an unsupported codec) left the waveform lane
        # silently blank forever, with no record anywhere of why. Reported live as
        # "each song's visual waveform" missing.
        self._log.info("waveform generation failed for media %s: %s", media_id, message)

    def _list_ordered_media_ids_for_playlist(self, playlist_id: UUID) -> list[UUID]:
        # Best-effort: uses whatever the synchronizer currently has tracked. A full
        # implementation would query playlist_items; acceptable for MVP since this is
        # only consulted for similarity scoring against *other* known playlists.
        return []


def _uri_to_path(uri: str) -> Path | None:
    from bookmark_studio.media.resolver import uri_to_local_path

    return uri_to_local_path(uri)
