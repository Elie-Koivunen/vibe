"""Application: the composition root (spec #114).

Wires three parts together and to the window's public API:

* PlaybackSession (app/session.py) -- the connection to the player, its ordered command
  queue and the status/playlist polls;
* PlaylistContext (app/playlist_context.py) -- which media and which bookmark playlist
  the player's playlist corresponds to;
* the bookmark/loop logic in this class: what's on screen vs. what's playing, playing and
  looping bookmarks and selections, and the waveform for the song on screen.

The player is either a VLC window driven over HTTP (launched by this app or attached
to) or the in-app libVLC player.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from bookmark_studio import platform_support
from bookmark_studio.app.playlist_context import AskPlaylistMatch, PlaylistContext
from bookmark_studio.app.session import PlaybackSession, unpack_sample
from bookmark_studio.app.vlc_launcher import (
    discover_vlc_instances,
    find_free_http_port,
    has_unmanaged_vlc_process,
    launch_managed_vlc,
    terminate_managed_vlc,
)
from bookmark_studio.app.waveform_orchestrator import WaveformOrchestrator
from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.equalizer import VLC_BANDS_HZ, EqualizerSettings
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.domain.selection import Selection
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.media.resolver import uri_to_local_path
from bookmark_studio.persistence.bookmark_repository import BookmarkRepository
from bookmark_studio.persistence.media_repository import MediaRepository
from bookmark_studio.persistence.playlist_repository import PlaylistRepository
from bookmark_studio.persistence.waveform_repository import WaveformCacheRepository
from bookmark_studio.playback.adapter import PlaybackAdapter
from bookmark_studio.playback.command_queue import CommandExecutor
from bookmark_studio.playback.http_fallback import StandardHttpPlaybackAdapter
from bookmark_studio.playback.loop_controller import LoopController
from bookmark_studio.playback.playback_clock import PlaybackClock
from bookmark_studio.playback.status import PlaybackStatus, VlcPlaylistItem
from bookmark_studio.settings.settings_service import SettingsService
from bookmark_studio.ui.deck_fader import percent_to_level
from bookmark_studio.ui.dialogs.vlc_launch_dialog import VlcLaunchChoice, VlcLaunchDialog
from bookmark_studio.ui.main_window import MainWindow
from bookmark_studio.waveform.pyramid import WaveformPyramid
from bookmark_studio.waveform.service import WaveformService

if TYPE_CHECKING:
    from bookmark_studio.project.sync_service import SyncService

# A stop/pause made in VLC's own window (not through this app) is recognised when two
# consecutive *fresh* status samples say so while a loop is playing, and not within this
# long after one of the loop's own seek-backs (VLC reports transient states around them).
# An earlier attempt at this misfired on ordinary loops; it compared every sample,
# including ones sampled before a seek landed -- those are now discarded as stale.
EXTERNAL_STOP_GRACE_S = 0.6
EXTERNAL_STOP_VOTES = 2
_SYNC_INTERVAL_MS = 60_000
# Playing a bookmark brings a quieter (or muted) player up to 85 % (VLC's 0-512 scale).
BOOKMARK_PLAY_VOLUME = round(0.85 * 256)
# The Volume & EQ tab's Max: VLC's normal full volume (the fader goes to 125 %, which can
# distort). Its ramp steps every 40 ms.
MAX_BUTTON_VOLUME = 256
# A bookmark played without a loop counts as done once playback leaves it -- but not in
# this first moment, when a status poll may still predate the seek.
BOOKMARK_PLAYBACK_GRACE_S = 1.0
_VOLUME_RAMP_STEP_MS = 40


class Application(QObject):
    """Ties one player to persistence and the UI for a live session."""

    window: MainWindow

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
        vlc_process: subprocess.Popen[bytes] | None = None,
        command_executor: CommandExecutor | None = None,
        ask_playlist_match: AskPlaylistMatch | None = None,
        libvlc_dir: str | None = None,
        libvlc_args: list[str] | None = None,
        sync_service: "SyncService | None" = None,
        http_port: int | None = None,
        quit_app: Callable[[], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._log = get_logger("APP")
        self._conn = conn
        self._settings = settings
        self._vlc_path = vlc_path
        self._vlc_process = vlc_process  # only a VLC this app launched; closed on exit
        self._vlc_port: int | None = None
        self._libvlc_dir = libvlc_dir
        self._libvlc_args = libvlc_args
        self._sync = sync_service
        self._http_port = http_port  # --port: first port to try for a launched VLC
        self._quit_app = quit_app or QApplication.quit
        self._stopped = False

        self._bookmark_repository = BookmarkRepository(conn)
        self._media_repository = MediaRepository(conn)
        self._playlist_repository = PlaylistRepository(conn)
        self._waveform_repository = WaveformCacheRepository(conn)

        self.session = PlaybackSession(adapter, command_executor=command_executor, parent=self)
        self.session.mute_pending = mute_on_connect
        self.session.status_sampled.connect(self._on_status_sample)
        self.session.playlist_sampled.connect(self._on_playlist_result)
        self.session.connection_changed.connect(self._on_connection_changed)
        self.playlists = PlaylistContext(
            self._media_repository, self._playlist_repository,
            ask_playlist_match or self._ask_playlist_match_dialog,
        )

        self._waveform_service = WaveformService(ffmpeg_path=ffmpeg_path, cache_dir=waveform_cache_dir)
        self._waveform_orchestrator = WaveformOrchestrator(
            service=self._waveform_service, repository=self._waveform_repository
        )
        self._waveform_orchestrator.waveform_ready.connect(self._on_waveform_ready)
        self._waveform_orchestrator.waveform_progress.connect(self._on_waveform_progress)
        self._waveform_orchestrator.waveform_failed.connect(self._on_waveform_failed)
        self._waveform_orchestrator.duration_known.connect(self._on_duration_known)
        self._exact_duration_by_media: dict[UUID, int] = {}
        self._preload_requested: set[UUID] = set()

        self._clock = PlaybackClock()
        self._loop_controller = LoopController(adapter, self._clock, executor=self.session.commands)
        self._loop_controller.bookmark_navigation_requested.connect(self._on_loop_navigation_requested)
        self._loop_controller.loop_completed.connect(self._on_loop_completed)
        self._loop_controller.loop_failed.connect(self._on_loop_failed)
        self._loop_controller.loop_started.connect(lambda _spec: self._note_loop_segment_started())
        self._loop_controller.iteration_changed.connect(lambda _remaining: self._note_loop_segment_started())

        self.window = MainWindow(self._bookmark_repository, undo_stack=QUndoStack(self))
        # The player's equalizer: the user's saved settings, applied to every player.
        self._equalizer = settings.equalizer() if settings is not None else EqualizerSettings()
        self.window.show_equalizer(self._equalizer)
        self._show_equalizer_support(adapter)
        # Max / Mute ramps and what Reset goes back to.
        self._volume_ramp_ms = (
            (settings.volume_ramp_ms("max"), settings.volume_ramp_ms("mute")) if settings is not None
            else self.window.volume_ramp_times()
        )
        self.window.show_volume_ramp_times(*self._volume_ramp_ms)
        self._volume_ramp: tuple[int, int, int, int] | None = None  # start, end, started_ns, duration_ns
        self._volume_ramp_timer = QTimer(self)
        self._volume_ramp_timer.setInterval(_VOLUME_RAMP_STEP_MS)
        self._volume_ramp_timer.timeout.connect(self._on_volume_ramp_tick)
        # Normalize / Reset levels (percent; the Volume & EQ tab's fields).
        self._volume_levels = (
            (settings.volume_level_percent("normalize"), settings.volume_level_percent("reset"))
            if settings is not None else self.window.volume_levels()
        )
        self.window.show_volume_levels(*self._volume_levels)
        # Equalizer presets glide under the same rule as the volume ramps.
        if settings is not None:
            self.window.show_equalizer_glide_ms(settings.equalizer_glide_ms())

        self._current_media_id: UUID | None = None
        self._current_vlc_item_id: int | None = None  # the song on screen
        self._actually_playing_vlc_item_id: int | None = None  # the song the player is on
        self._last_playback_state = "stopped"
        # Equalizer presets glide under the volume ramps' rule: while something plays.
        self.window.set_equalizer_glide_condition(lambda: self._last_playback_state == "playing")
        self._selection_loop_active = False  # the waveform's own "Play" is looping a selection
        self._active_loop_bookmark_id: UUID | None = None
        self._active_loop_was_enabled = False  # the playing bookmark's Loop when it started
        # The bookmark playing (green) or played last (yellow) -- shown on its song, its
        # waveform area and its list row. See _begin/_finish_bookmark_playback.
        self._bookmark_playback: _BookmarkPlayback | None = None
        self._loop_item_id: int | None = None
        self._loop_segment_started = 0.0
        self._loop_last_time_us: int | None = None
        self._external_stop_votes = 0

        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self.sync_now)
        self._wire_window()

    # -- lifecycle --

    def start(self) -> None:
        if self._settings is not None:
            self.window.restore_layout(self._settings)
        self.session.start()
        if self._sync is not None:
            self.sync_now()
            self._sync_timer.start(_SYNC_INTERVAL_MS)
        self.window.show()

    def quit(self) -> None:
        """Quit button / File > Quit / Ctrl+Q. Saves what is still being typed and the
        window layout, then exits; stop() (on aboutToQuit) runs the final sync export and
        closes the VLC this app launched. Bookmarks themselves are saved on every edit."""
        self.window.commit_pending_edits()
        self._save_window_layout()
        self._quit_app()

    def _save_window_layout(self) -> None:
        if self._settings is not None:
            try:
                self.window.save_layout(self._settings)
                self._settings.sync()
            except Exception:  # noqa: BLE001 - never block quitting
                self._log.exception("could not save the window layout")

    def stop(self) -> None:
        """Shuts the session down (connected to QApplication.aboutToQuit)."""
        if self._stopped:
            return
        self._stopped = True
        self._save_window_layout()
        self._sync_timer.stop()
        self._stop_volume_ramp()
        self._loop_controller.stop(restore_volume=True)  # undo a fade's volume change
        self.session.stop(drain_timeout_s=1.5)
        self._waveform_orchestrator.cancel_all()
        self._waveform_orchestrator.wait_for_idle(3000)
        if self._sync is not None:
            try:
                self._sync.export_now()
            except Exception:  # noqa: BLE001 - never block quitting
                self._log.exception("final sync export failed")
        # Only a VLC this app launched is closed; one it attached to is left alone.
        if self._vlc_process is not None:
            try:
                terminate_managed_vlc(self._vlc_process, self._vlc_path, self._vlc_port)
            except Exception:  # noqa: BLE001 - never block quitting
                self._log.exception("could not close the managed VLC")

    # -- wiring --

    def _wire_window(self) -> None:
        w = self.window
        w.play_pause_requested.connect(self._on_play_pause_clicked)
        w.stop_requested.connect(self._on_stop_clicked)
        w.seek_relative_requested.connect(self._seek_relative)
        w.previous_track_requested.connect(lambda: self._submit_adapter_call("previous_track"))
        w.next_track_requested.connect(lambda: self._submit_adapter_call("next_track"))
        w.previous_bookmark_requested.connect(self._on_previous_bookmark)
        w.next_bookmark_requested.connect(self._on_next_bookmark)
        w.seek_requested.connect(self._seek_displayed)
        w.waveform_selection_changed.connect(self._on_waveform_selection_changed)
        w.playlist_item_double_clicked.connect(self._on_playlist_item_double_clicked)
        w.playlist_item_selected.connect(self._on_playlist_item_selected)
        w.follow_player_toggled.connect(self._on_follow_vlc_toggled)
        w.loop_selection_requested.connect(self._on_loop_selection_requested)
        w.launch_vlc_requested.connect(self.prompt_vlc_launch_dialog)
        w.play_bookmark_requested.connect(self._on_play_bookmark_requested)
        w.loop_bookmark_requested.connect(self._on_loop_bookmark_requested)
        w.bookmark_reorder_requested.connect(self._on_bookmark_reorder_requested)
        w.bookmarks_changed.connect(self._refresh_bookmark_views)
        w.bookmark_song_display_requested.connect(self._on_bookmark_song_display_requested)
        w.project_imported.connect(self._on_project_imported)
        w.playlist_refresh_requested.connect(self._force_playlist_refresh)
        w.sync_requested.connect(self._on_sync_requested)
        w.volume_requested.connect(self._on_user_volume)
        w.volume_max_requested.connect(self._on_volume_max_requested)
        w.volume_mute_requested.connect(self._on_volume_mute_requested)
        w.volume_reset_requested.connect(self._on_volume_reset_requested)
        w.volume_normalize_requested.connect(self._on_volume_normalize_requested)
        w.volume_levels_changed.connect(self._on_volume_levels_changed)
        w.volume_ramp_times_changed.connect(self._on_volume_ramp_times_changed)
        w.equalizer_glide_ms_changed.connect(
            lambda value_ms: self._settings.set_equalizer_glide_ms(value_ms) if self._settings is not None else None
        )
        w.equalizer_requested.connect(self._set_equalizer)
        w.quit_requested.connect(self.quit)

    # -- commands --

    @property
    def _adapter(self) -> PlaybackAdapter:
        return self.session.adapter

    def _submit(self, fn: Callable[[], object], *, fences: tuple[str, ...] = ("position",),
                on_done: Callable[[object], None] | None = None) -> None:
        self.session.submit(fn, fences=fences, on_done=on_done)

    def _fire_and_forget(self, fn: Callable[[], object]) -> None:
        self._submit(fn)

    def _submit_adapter_call(self, method_name: str) -> None:
        adapter = self._adapter
        self._submit(lambda: getattr(adapter, method_name)())

    def _seek_relative(self, delta_us: int) -> None:
        adapter = self._adapter
        self._submit(lambda: adapter.seek_relative_us(delta_us))

    # -- volume --

    def _set_player_volume(self, level: int, *, write: bool = True) -> None:
        """A volume the user chose (0-512): the fader, or a bookmark's start level. It
        becomes what fades ramp to, and goes to the player now unless a fade is running.
        Consecutive changes (a fader drag) collapse into the latest one."""
        self.session.mute_pending = False  # the user decided: no mute-on-connect after this
        if self._loop_controller.set_user_volume(level) and write:
            adapter = self._adapter
            self.session.commands.submit(
                lambda: adapter.set_volume(level), coalesce_key="volume", fences=("volume",),
                on_error=lambda exc: self._log.debug("volume change failed: %s", exc),
            )

    # -- equalizer --

    def _set_equalizer(self, settings: EqualizerSettings) -> None:
        """The user changed the equalizer (Volume & EQ tab): remembered, and sent to the
        player now. A fader drag collapses into its latest position."""
        self._equalizer = settings
        if self._settings is not None:
            self._settings.set_equalizer(settings)
        self._apply_equalizer()

    def _apply_equalizer(self) -> None:
        adapter = self._adapter
        if not getattr(adapter, "supports_equalizer", False):
            return
        settings = self._equalizer
        self.session.commands.submit(
            lambda: adapter.set_equalizer(settings), coalesce_key="equalizer",
            on_error=lambda exc: self._log.info("equalizer change failed: %s", exc),
        )

    def _show_equalizer_support(self, adapter: PlaybackAdapter) -> None:
        supported = bool(getattr(adapter, "supports_equalizer", False))
        band_hz = tuple(getattr(adapter, "equalizer_band_hz", ())) or VLC_BANDS_HZ
        note = "" if supported else "This player has no equalizer (VLC's Lua bridge)."
        self.window.set_equalizer_support(supported, band_hz, note)

    def _on_connection_changed(self, connected: bool) -> None:
        self.window.set_connected(connected)
        # A player just (re)connected: give it the user's equalizer. One that is off is
        # left alone, so a VLC window keeps an equalizer set up in VLC itself.
        if connected and self._equalizer.enabled:
            self._apply_equalizer()

    def _on_user_volume(self, level: int) -> None:
        """The fader: the user takes over from a running Max/Normalize/Reset/Mute."""
        self._stop_volume_ramp()
        self._set_player_volume(level)

    # -- Max / Normalize / Reset / Mute (Volume & EQ tab) --

    def _on_volume_max_requested(self) -> None:
        if self._loop_controller.target_volume >= MAX_BUTTON_VOLUME:
            return  # already at (or above) 100 %
        self._ramp_volume_to(MAX_BUTTON_VOLUME, self._volume_ramp_ms[0])

    def _on_volume_mute_requested(self) -> None:
        self._ramp_volume_to(0, self._volume_ramp_ms[1])

    def _on_volume_normalize_requested(self) -> None:
        self._glide_volume_to_percent(self._volume_levels[0])

    def _on_volume_reset_requested(self) -> None:
        self._glide_volume_to_percent(self._volume_levels[1])

    def _glide_volume_to_percent(self, percent: int) -> None:
        """Normalize / Reset: to their level, the way the volume comes -- at Max's speed
        coming down (as from Max), at Mute's speed going up (as from Mute)."""
        level = percent_to_level(percent)
        coming_down = self._loop_controller.target_volume > level
        self._ramp_volume_to(level, self._volume_ramp_ms[0] if coming_down else self._volume_ramp_ms[1])

    def _on_volume_levels_changed(self, normalize_percent: int, reset_percent: int) -> None:
        self._volume_levels = (normalize_percent, reset_percent)
        if self._settings is not None:
            self._settings.set_volume_level_percent("normalize", normalize_percent)
            self._settings.set_volume_level_percent("reset", reset_percent)

    def _on_volume_ramp_times_changed(self, max_ms: int, mute_ms: int) -> None:
        self._volume_ramp_ms = (max_ms, mute_ms)
        if self._settings is not None:
            self._settings.set_volume_ramp_ms("max", max_ms)
            self._settings.set_volume_ramp_ms("mute", mute_ms)

    def _ramp_volume_to(self, level: int, duration_ms: int) -> None:
        """Moves the user's volume to `level` over `duration_ms` while something plays (a
        bookmark or a song); at once when nothing does or the time is 0."""
        start = self._loop_controller.target_volume
        self._stop_volume_ramp()
        if duration_ms <= 0 or self._last_playback_state != "playing" or start == level:
            self._apply_volume_step(level)
            return
        self._volume_ramp = (start, level, time.monotonic_ns(), duration_ms * 1_000_000)
        self._volume_ramp_timer.start()

    def _on_volume_ramp_tick(self) -> None:
        if self._volume_ramp is None:
            self._volume_ramp_timer.stop()
            return
        start, end, started_ns, duration_ns = self._volume_ramp
        fraction = min(1.0, (time.monotonic_ns() - started_ns) / duration_ns)
        self._apply_volume_step(round(start + (end - start) * fraction))
        if fraction >= 1.0:
            self._stop_volume_ramp()

    def _stop_volume_ramp(self) -> None:
        self._volume_ramp = None
        self._volume_ramp_timer.stop()

    def _apply_volume_step(self, level: int) -> None:
        self._set_player_volume(level)  # fades ramp to/from it; writes are coalesced
        self.window.show_volume(level)

    def _raise_volume_for_bookmark(self, *, fades_in: bool = False) -> None:
        """Playing a bookmark brings a quieter (or muted) player up to 85 %; a louder
        setting is kept. With a fade-in, the fade itself ramps up to that level."""
        if self._loop_controller.target_volume >= BOOKMARK_PLAY_VOLUME:
            return
        self._stop_volume_ramp()  # e.g. a Mute still fading out
        self._set_player_volume(BOOKMARK_PLAY_VOLUME, write=not fades_in)
        self.window.show_volume(BOOKMARK_PLAY_VOLUME)

    # -- choosing the player: attach / launch a VLC window / play inside the app --

    def prompt_vlc_launch_dialog(self) -> None:
        from bookmark_studio.playback.libvlc_loader import libvlc_available

        in_app_ok = libvlc_available(self._libvlc_dir)
        if self._settings is None or (self._vlc_path is None and not in_app_ok):
            QMessageBox.information(
                self.window, "Open media",
                "Neither VLC nor libVLC was found on this machine."
                if self._settings is not None else "VLC integration is not available in this session.",
            )
            return
        instances = discover_vlc_instances(self._settings, vlc_path=self._vlc_path) if self._vlc_path else []
        unmanaged_running = not instances and self._vlc_path is not None and has_unmanaged_vlc_process()
        dialog = VlcLaunchDialog(
            instances, self._launch_dialog_media_filter(), parent=self.window,
            unmanaged_vlc_running=unmanaged_running,
            can_launch_vlc=self._vlc_path is not None, can_play_in_app=in_app_ok,
            prefer_in_app=self._settings.playback_backend() == "libvlc",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_launch_choice(dialog.choice())

    def apply_launch_choice(self, choice: VlcLaunchChoice) -> bool:
        """Attaches / launches / opens the in-app player; False (after telling the user)
        if that failed."""
        try:
            if choice.mode == "attach":
                if choice.port is None:
                    raise ValueError("no port to attach to")
                self._attach_to_vlc(choice.port, host=choice.host)
            elif choice.mode == "in_app":
                self.open_in_app_player(choice.media_paths, source_uri=choice.source_uri)
            else:
                self._launch_new_vlc(choice)
        except Exception as exc:  # noqa: BLE001 - reported to the user, the app keeps running
            self._log.exception("could not start playback (%s)", choice.mode)
            QMessageBox.warning(self.window, "Open media", f"Could not start playback:\n\n{exc}")
            return False
        return True

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
        if not isinstance(choice, VlcLaunchChoice):
            choice = VlcLaunchChoice(mode="launch", media_paths=list(choice))
        bind_host, connect_host = platform_support.vlc_http_hosts(self._vlc_path)
        # A Windows VLC driven from WSL listens on the Windows side, where a Linux-side
        # bind test proves nothing; the connect test still catches a busy port.
        windows_vlc_from_wsl = platform_support.is_wsl() and platform_support.is_windows_executable(self._vlc_path)
        port = find_free_http_port(
            self._http_port or self._settings.bridge_port(), connect_host=connect_host,
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

    def open_in_app_player(self, media_paths: list[str], *, source_uri: str | None = None) -> None:
        """Plays `media_paths` inside this app through libVLC (no VLC window)."""
        from bookmark_studio.playback.libvlc_adapter import LibVlcPlaybackAdapter

        adapter = LibVlcPlaybackAdapter(libvlc_dir=self._libvlc_dir, instance_args=self._libvlc_args)
        adapter.load(media_paths)
        self._swap_adapter(adapter, mute_on_connect=False, new_vlc_process=None, source_uri=source_uri)
        if self._settings is not None:
            self._settings.set_playback_backend("libvlc")
        self._log.info("In-app player with %d media item(s)", len(media_paths))

    def _swap_adapter(self, new_adapter: PlaybackAdapter, *, mute_on_connect: bool,
                      new_vlc_process: subprocess.Popen[bytes] | None, source_uri: str | None = None) -> None:
        """Retargets the whole session at another player. A VLC this app launched earlier
        is left running (the user may still want it); its handle is simply dropped."""
        self._loop_controller.set_adapter(new_adapter)
        self._vlc_process = new_vlc_process
        self._vlc_port = None
        self._current_vlc_item_id = None
        self._actually_playing_vlc_item_id = None
        self._current_media_id = None
        self._selection_loop_active = False
        self._active_loop_bookmark_id = None
        self._preload_requested.clear()
        # Forget the old player's playlist, or its bookmarks would be applied to whatever
        # the new one plays.
        self.playlists.reset(source_uri=source_uri)
        self._clear_bookmark_playback()
        self._show_equalizer_support(new_adapter)
        self.session.swap_adapter(new_adapter, mute_on_connect=mute_on_connect)

    # -- play / pause / stop --

    def _on_play_pause_clicked(self) -> None:
        adapter = self._adapter
        # The state is updated right away (not on the next poll) so a quick second press
        # toggles back instead of repeating the same command.
        if self._last_playback_state == "playing":
            self._stop_loop()  # or the loop's boundary timer would resume playback later
            self._submit(adapter.pause)
            self._last_playback_state = "paused"
        else:
            self._submit(adapter.play)
            self._last_playback_state = "playing"

    def _on_stop_clicked(self) -> None:
        self._stop_loop()
        self._submit(self._adapter.stop)
        self._last_playback_state = "stopped"

    def _stop_loop(self) -> None:
        if self._bookmark_playback is not None and self._bookmark_playback.looping:
            self._finish_bookmark_playback()
        self._loop_controller.stop()
        self._selection_loop_active = False
        self._active_loop_bookmark_id = None
        self._loop_item_id = None
        self._external_stop_votes = 0

    # -- the song on screen vs. the song the player is on --

    def _goto_for_displayed(self) -> Callable[[], None] | None:
        """A command that switches the player to the song on screen, or None if it is
        already there. Commands run in order, so from here on the player *will* be on that
        song -- recorded immediately, or a second command issued before the next poll
        (e.g. looping a bookmark of the previous song) would skip its own switch."""
        displayed = self._current_vlc_item_id
        if displayed is None or displayed == self._actually_playing_vlc_item_id:
            return None
        adapter = self._adapter
        self._actually_playing_vlc_item_id = displayed
        return lambda: adapter.goto_item(displayed)

    def _seek_displayed(self, time_us: int) -> None:
        """Waveform click / playhead drag / bookmark navigation: a position in the song on
        screen -- switching the player to it first if another song is playing."""
        goto = self._goto_for_displayed()
        adapter = self._adapter
        if goto is not None:
            self.window.set_follow_player(True, notify=False)

        def _seek() -> None:
            if goto is not None:
                goto()
            adapter.seek_absolute_us(time_us)

        self._submit(_seek, on_done=lambda _r: self._clock.note_seek(time_us))

    def _playlist_item_for_media(self, media_id: UUID) -> VlcPlaylistItem | None:
        return self.playlists.item_for_media(
            media_id, prefer=(self._actually_playing_vlc_item_id, self._current_vlc_item_id)
        )

    def _show_item(self, item: VlcPlaylistItem) -> None:
        """Makes `item` the song on screen (waveform, bookmarks, breadcrumb)."""
        if item.vlc_id == self._current_vlc_item_id and self._current_media_id is not None:
            return
        self._current_vlc_item_id = item.vlc_id
        self._on_current_item_changed(item.uri, _item_duration_us(item))

    def _switch_displayed_song_for_bookmark(self, item: VlcPlaylistItem) -> None:
        self.window.set_follow_player(True, notify=False)
        self._show_item(item)

    # -- selections and bookmarks --

    def _on_loop_selection_requested(self, start_us: int, end_us: int) -> None:
        """"Play" on a painted selection: loops it (in the song on screen)."""
        before = self._goto_for_displayed()
        if before is not None:
            self.window.set_follow_player(True, notify=False)
        self._selection_loop_active = True
        self._active_loop_bookmark_id = None
        self._start_loop(
            LoopSpec(start_us=start_us, end_us=end_us, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE),
            before=before,
        )

    def _on_waveform_selection_changed(self, selection: object) -> None:
        """Dragging an edge of a selection that is being looped restarts the loop with the
        new bounds; clearing it stops the loop."""
        if not self._selection_loop_active:
            return
        if not isinstance(selection, Selection):
            self._stop_loop()
            return
        self._start_loop(
            LoopSpec(start_us=selection.start_us, end_us=selection.end_us, repeat_count=None, gap_ms=0,
                     completion_action=CompletionAction.CONTINUE)
        )

    def _start_loop(self, spec: LoopSpec, *, before: Callable[[], object] | None = None) -> None:
        self._loop_item_id = self._current_vlc_item_id or self._actually_playing_vlc_item_id
        self._external_stop_votes = 0
        self._loop_controller.start(spec, before=before)

    def _note_loop_segment_started(self) -> None:
        self._loop_segment_started = time.monotonic()
        self._external_stop_votes = 0

    def _on_bookmark_song_display_requested(self, bookmark_id: UUID) -> None:
        """Selecting a bookmark shows its song (without playing it)."""
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        item = self._playlist_item_for_media(bookmark.media_id)
        if item is not None:
            self.window.select_playlist_item(item.vlc_id)

    def _on_play_bookmark_requested(self, bookmark_id: UUID) -> None:
        """Play Bookmark / double-click: a loop-enabled segment loops with its own settings;
        anything else is a seek+play. Switches the player to the bookmark's song first."""
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None:
            return
        if bookmark.loop_enabled and bookmark.end_us is not None:
            self._on_loop_bookmark_requested(bookmark_id)
            return
        self._stop_loop()
        self._raise_volume_for_bookmark()
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
        self._last_playback_state = "playing"
        self._begin_bookmark_playback(bookmark, looping=False)

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
        self._active_loop_was_enabled = bookmark.loop_enabled
        self._raise_volume_for_bookmark(fades_in=bookmark.fade_in_ms > 0)
        self._start_loop(_loop_spec(bookmark), before=before)
        self._last_playback_state = "playing"
        self._begin_bookmark_playback(bookmark, looping=True)

    # -- green while a bookmark plays, yellow once it has played --

    def _begin_bookmark_playback(self, bookmark: Any, *, looping: bool) -> None:
        item = self._playlist_item_for_media(bookmark.media_id)
        self._bookmark_playback = _BookmarkPlayback(
            bookmark_id=bookmark.id, song_vlc_id=item.vlc_id if item is not None else None,
            looping=looping, started=time.monotonic(),
        )
        self._show_bookmark_playback()

    def _finish_bookmark_playback(self) -> None:
        playback = self._bookmark_playback
        if playback is not None and playback.state == "playing":
            playback.state = "done"
            self._show_bookmark_playback()

    def _clear_bookmark_playback(self) -> None:
        if self._bookmark_playback is not None:
            self._bookmark_playback = None
            self._show_bookmark_playback()

    def _show_bookmark_playback(self) -> None:
        if self._stopped:
            return
        playback = self._bookmark_playback
        if playback is None:
            self.window.show_bookmark_playback(None, None, None)
        else:
            self.window.show_bookmark_playback(playback.bookmark_id, playback.song_vlc_id, playback.state)

    def _check_bookmark_playback_done(self, status: PlaybackStatus) -> None:
        """A bookmark played with Play (no loop) is done once playback passes its end,
        stops or pauses, or moves to another song. (A loop ends through the loop
        controller instead.) Not in the first moment: a poll may predate the seek."""
        playback = self._bookmark_playback
        if playback is None or playback.state != "playing" or playback.looping:
            return
        if time.monotonic() - playback.started < BOOKMARK_PLAYBACK_GRACE_S:
            return
        bookmark = self._bookmark_repository.get(playback.bookmark_id)
        if bookmark is None:
            self._clear_bookmark_playback()
            return
        other_song = playback.song_vlc_id is not None and status.current_playlist_item_id != playback.song_vlc_id
        past_end = bookmark.end_us is not None and status.time_us >= bookmark.end_us
        if status.state != "playing" or other_song or past_end:
            self._finish_bookmark_playback()

    def _on_loop_failed(self, message: str) -> None:
        self._log.info("loop stopped: %s", message)
        if self._bookmark_playback is not None and self._bookmark_playback.looping:
            self._finish_bookmark_playback()

    def _follow_active_loop_bookmark(self) -> None:
        """The playing loop's bookmark may just have been edited (Bookmark settings, the
        list's columns, a drag, undo): the loop takes its settings at once -- a repeat
        count already reached ends it after this pass and runs the new After-loop action.
        Switching Loop off finishes the current pass; deleting the bookmark stops the loop
        (playback goes on)."""
        playback = self._bookmark_playback
        if playback is not None and self._bookmark_repository.get(playback.bookmark_id) is None:
            self._clear_bookmark_playback()  # the bookmark was deleted
        bookmark_id = self._active_loop_bookmark_id
        current = self._loop_controller.spec
        if bookmark_id is None or current is None:
            return
        bookmark = self._bookmark_repository.get(bookmark_id)
        if bookmark is None or bookmark.end_us is None:
            self._loop_controller.stop()
            self._active_loop_bookmark_id = None
            return
        spec = _loop_spec(bookmark)
        switched_off = self._active_loop_was_enabled and not bookmark.loop_enabled
        if spec != current or switched_off:
            self._loop_controller.update_spec(spec, last_pass=switched_off)

    def _on_loop_completed(self, _action: object) -> None:
        self._selection_loop_active = False
        self._loop_item_id = None
        if self._bookmark_playback is not None and self._bookmark_playback.looping:
            self._finish_bookmark_playback()

    def _on_loop_navigation_requested(self, action: object) -> None:
        """"After loop: Next/Previous Bookmark": plays the neighbouring bookmark of the same
        song (by start time) with its own settings."""
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

    def _on_bookmark_reorder_requested(self, ordered_bookmark_ids: list[UUID]) -> None:
        self._bookmark_repository.reorder(ordered_bookmark_ids)
        self._refresh_bookmark_views()

    def _current_bookmarks_sorted(self) -> list[Any]:
        if self._current_media_id is None:
            return []
        bookmarks = self._bookmark_repository.list_for_context(self.playlists.active_playlist_id,
                                                               self._current_media_id)
        return sorted(bookmarks, key=lambda b: b.start_us)

    def _displayed_position_us(self) -> int:
        if self._current_vlc_item_id is not None and self._current_vlc_item_id == self._actually_playing_vlc_item_id:
            return self._clock.estimated_position_us()
        return self.window.playhead_time_us()

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

    # -- status --

    def _on_status_result(self, payload: object) -> None:
        """A status poll result, (issued_ns, status) or a bare status (tests)."""
        issued_ns, status = unpack_sample(payload)
        self._on_status_sample(issued_ns, status)

    def _on_status_sample(self, issued_ns: int, status: PlaybackStatus) -> None:
        if self._stopped:
            return  # a poll still in flight when the session stopped
        if self.session.mute_pending:
            # A VLC this app launched starts muted (it would otherwise blast the first
            # song). Through the volume model, so fades and a bookmark's 85 % know it's 0
            # right away -- not one poll later.
            self._set_player_volume(0)
            self.window.show_volume(0)
            return
        try:
            # Fades ramp to/from the user's real volume (ignored while a fade owns it).
            self._loop_controller.set_target_volume(status.volume, sampled_at_ns=issued_ns)
            if not self.session.is_volume_stale(issued_ns):
                self.window.show_volume(status.volume)
            if self.session.is_stale(issued_ns):
                return  # sampled before our last seek/goto/play took effect
            self._clock.update(status)
            self._last_playback_state = status.state
            self.window.show_playback(status.time_us, status.duration_us, status.current_playlist_item_id,
                                      is_playing=status.state == "playing")
            # The playhead follows the player only while its song is the one on screen.
            if self._current_vlc_item_id is not None and self._current_vlc_item_id == status.current_playlist_item_id:
                self.window.set_playhead(status.time_us)
            self._check_external_stop(status)
            self._loop_controller.on_tick()
            self._check_bookmark_playback_done(status)

            if status.current_playlist_item_id != self._actually_playing_vlc_item_id:
                self._actually_playing_vlc_item_id = status.current_playlist_item_id
                self._apply_actually_playing_item_if_following(status)
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            self._log.exception("status result handling failed")

    def _check_external_stop(self, status: PlaybackStatus) -> None:
        """Stops a playing loop when the user paused/stopped playback or changed song in
        VLC's own window -- otherwise the loop's boundary timer resumed playback moments
        later. Needs two consecutive fresh samples, outside the grace period after the
        loop's own seek-backs; a "paused" report only counts if the position stood still."""
        if self._loop_controller.state is not LoopState.PLAYING:
            self._external_stop_votes = 0
            self._loop_last_time_us = None
            return
        if time.monotonic() - self._loop_segment_started < EXTERNAL_STOP_GRACE_S:
            return
        previous_time = self._loop_last_time_us
        self._loop_last_time_us = status.time_us
        switched = (
            self._loop_item_id is not None and status.current_playlist_item_id is not None
            and status.current_playlist_item_id != self._loop_item_id
        )
        halted = status.state == "stopped" or (
            status.state == "paused" and previous_time is not None and previous_time == status.time_us
        )
        self._external_stop_votes = self._external_stop_votes + 1 if (switched or halted) else 0
        if self._external_stop_votes >= EXTERNAL_STOP_VOTES:
            self._log.info("playback was %s outside this app; stopping the loop",
                           "switched" if switched else status.state)
            self._stop_loop()

    def _apply_actually_playing_item_if_following(self, status: PlaybackStatus) -> None:
        if not self.window.follow_player_enabled():
            return  # the user is previewing another song
        item = self.playlists.item(status.current_playlist_item_id)
        if item is not None and item.uri:
            self._show_item(item)
            return
        # VLC's status.json media_uri is often a bare filename, not a resolvable URI --
        # only used before the first playlist poll.
        if status.media_uri:
            self._current_vlc_item_id = status.current_playlist_item_id
            self._on_current_item_changed(status.media_uri, status.duration_us)

    def _on_playlist_item_double_clicked(self, vlc_id: int) -> None:
        """Double-click on a song plays it from the beginning (stopping any loop)."""
        self._stop_loop()
        item = self.playlists.item(vlc_id)
        self.window.set_follow_player(True, notify=False)
        if item is not None and item.uri:
            self._show_item(item)
        self._actually_playing_vlc_item_id = vlc_id  # see _goto_for_displayed
        adapter = self._adapter

        def _play_from_start() -> None:
            adapter.goto_item(vlc_id)
            adapter.seek_absolute_us(0)
            adapter.play()

        self._submit(_play_from_start, on_done=lambda _r: self._clock.note_seek(0, playing=True))
        self._last_playback_state = "playing"

    def _on_playlist_item_selected(self, vlc_id: int) -> None:
        """A single click previews a song (waveform, bookmarks) without playing it."""
        item = self.playlists.item(vlc_id)
        if item is None or not item.uri:
            return
        if vlc_id != self._actually_playing_vlc_item_id:
            self.window.set_follow_player(False)
        self._show_item(item)

    def _on_follow_vlc_toggled(self, enabled: bool) -> None:
        if not enabled or self._actually_playing_vlc_item_id is None:
            return
        if self._actually_playing_vlc_item_id == self._current_vlc_item_id:
            return
        item = self.playlists.item(self._actually_playing_vlc_item_id)
        if item is None or not item.uri:
            return
        self._show_item(item)

    # -- playlist --

    def _force_playlist_refresh(self) -> None:
        """Playlist > Refresh (F5): re-resolve and re-recognise on the next poll."""
        self.playlists.force_refresh()
        self.session.poll_playlist()

    def _on_playlist_result(self, payload: object) -> None:
        _issued_ns, items = unpack_sample(payload)
        try:
            if not self.playlists.apply_snapshot(list(items)):
                return
            self._push_exact_durations()
            self._refresh_bookmark_views()
            self._preload_playlist_waveforms()
        except Exception:  # noqa: BLE001 - spec #104: never crash the UI over one poll
            self._log.exception("playlist result handling failed")

    def _ask_playlist_match_dialog(self, playlist_name: str, score: float) -> bool:
        answer = QMessageBox.question(
            self.window, "Same playlist?",
            f"The playlist looks like “{playlist_name}” ({score:.0%} similar), "
            "but it has changed.\n\nUse that playlist's bookmarks?",
        )
        return bool(answer == QMessageBox.StandardButton.Yes)

    def _on_project_imported(self) -> None:
        """An import may have added the signatures the live playlist should match."""
        self.playlists.synchronizer.reset()
        self._force_playlist_refresh()

    # -- sync (Windows <-> WSL <-> other machines, through a shared folder) --

    def sync_now(self) -> None:
        if self._sync is None:
            return
        try:
            changed = self._sync.sync_now()
        except Exception:  # noqa: BLE001 - a broken sync folder must not break the app
            self._log.exception("sync failed")
            return
        self.window.refresh_tag_catalog()  # another machine may have edited the tag list
        if changed:
            self._on_project_imported()
            self._refresh_bookmark_views()
            self.window.bookmarks_changed.emit()

    def _on_sync_requested(self) -> None:
        if self._sync is None:
            QMessageBox.information(
                self.window, "Sync",
                "No sync folder is set. Start the app with --sync-dir <folder> (use the same "
                "folder on every machine / in WSL and Windows).",
            )
            return
        self.sync_now()

    # -- bookmark views --

    def _refresh_bookmark_views(self) -> None:
        """Every bookmark of the playlist (all songs) in the list, and per-song counts in
        the playlist panel. Both panels ignore refreshes that change nothing. (Every
        bookmark edit ends up here, so the playing loop follows its bookmark too.)"""
        self._follow_active_loop_bookmark()
        playlist_id = self.playlists.active_playlist_id
        counts: dict[int, int] = {}
        resolved = self.playlists.resolved
        if playlist_id is not None:
            bookmarks = self._bookmark_repository.list_for_playlist(playlist_id)
            per_media: dict[UUID, int] = {}
            for bookmark in bookmarks:
                per_media[bookmark.media_id] = per_media.get(bookmark.media_id, 0) + 1
            for item, media in resolved:
                counts[item.vlc_id] = per_media.get(media.id, 0)
            self.window.load_all_bookmarks(bookmarks, dict(self.playlists.song_names))
        else:
            for item, media in resolved:
                counts[item.vlc_id] = len(self._bookmark_repository.list_global_for_media(media.id))
            self.window.load_all_bookmarks([], {})
        self.window.set_playlist([item for item, _media in resolved], counts)

    _refresh_bookmark_panel = _refresh_bookmark_views

    # -- waveforms --

    def _preload_playlist_waveforms(self) -> None:
        """Makes sure every song in the playlist has a cached waveform (without loading
        cached ones -- see WaveformOrchestrator.prefetch)."""
        for item, media in self.playlists.resolved:
            if media.id in self._preload_requested or not media.fast_fingerprint:
                continue
            local_path = uri_to_local_path(media.canonical_uri or item.uri)
            if local_path is None or not local_path.exists():
                continue
            self._preload_requested.add(media.id)
            self._waveform_orchestrator.prefetch(media.id, media.fast_fingerprint, str(local_path))

    def _on_current_item_changed(self, media_uri: str | None, duration_us: int | None) -> None:
        if not media_uri:
            return
        media = self.playlists.resolver.resolve(media_uri, duration_us=duration_us)
        self._current_media_id = media.id
        # A painted selection belongs to one song; it goes when the song on screen changes.
        self.window.clear_waveform_selection()

        playlist_id = self.playlists.active_playlist_id
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
        self.window.set_playhead(self._clock.estimated_position_us() if playing_here else 0, follow=False)
        self.window.fit_waveform()  # otherwise a multi-minute song shows its first second

        local_path = uri_to_local_path(media.canonical_uri or media_uri)
        if local_path is not None and local_path.exists() and media.fast_fingerprint:
            self._waveform_orchestrator.request(media.id, media.fast_fingerprint, str(local_path))

    def _on_waveform_ready(self, media_id: UUID, pyramid: WaveformPyramid) -> None:
        if media_id != self._current_media_id:
            return  # switched songs before this one finished (spec #65)
        self.window.show_waveform(pyramid, pyramid.duration_us or self.window.waveform_duration_us())

    def _on_waveform_progress(self, media_id: UUID, pyramid: WaveformPyramid) -> None:
        """A partial waveform while a long file is still being decoded."""
        if media_id == self._current_media_id:
            self.window.show_waveform(pyramid, max(self.window.waveform_duration_us(), pyramid.duration_us or 0))

    def _on_duration_known(self, media_id: UUID, duration_us: int) -> None:
        """The exact decoded length of a song (VLC's HTTP interface only reports whole
        seconds), handed to the adapter for its position<->time conversions and seeks."""
        if not duration_us or duration_us <= 0:
            return
        self._exact_duration_by_media[media_id] = int(duration_us)
        for item, media in self.playlists.resolved:
            if media.id == media_id:
                self._adapter.set_exact_duration(item.vlc_id, int(duration_us))

    def _push_exact_durations(self) -> None:
        for item, media in self.playlists.resolved:
            duration = self._exact_duration_by_media.get(media.id)
            if duration:
                self._adapter.set_exact_duration(item.vlc_id, duration)

    def _on_waveform_failed(self, media_id: UUID, message: str) -> None:
        self._log.info("waveform generation failed for media %s: %s", media_id, message)


@dataclass
class _BookmarkPlayback:
    """The bookmark playing ("playing") or played last ("done")."""

    bookmark_id: UUID
    song_vlc_id: int | None
    looping: bool
    started: float
    state: str = "playing"


def _loop_spec(bookmark: Any) -> LoopSpec:
    """How a (segment) bookmark loops: its range and its own loop settings."""
    return LoopSpec(
        start_us=bookmark.start_us, end_us=bookmark.end_us,
        repeat_count=bookmark.repeat_count, gap_ms=bookmark.loop_gap_ms,
        completion_action=bookmark.completion_action,
        fade_in_ms=bookmark.fade_in_ms, fade_out_ms=bookmark.fade_out_ms,
    )


def _item_duration_us(item: object) -> int | None:
    duration_s = getattr(item, "duration_s", None)
    if duration_s is None or duration_s <= 0:
        return None
    return int(duration_s * 1_000_000)
