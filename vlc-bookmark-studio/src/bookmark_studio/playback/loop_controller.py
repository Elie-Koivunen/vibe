"""Software A-B loop state machine driven over a PlaybackAdapter (spec #33-#35)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.logging.setup import get_logger
from bookmark_studio.playback.command_queue import CommandExecutor, ImmediateExecutor
from bookmark_studio.playback.playback_clock import PlaybackClock

if TYPE_CHECKING:
    from bookmark_studio.playback.adapter import PlaybackAdapter

__all__ = ["LoopState", "LoopSpec", "LoopController"]

# Detecting the loop boundary only from on_tick() (once per ~400ms status poll) would
# let playback run up to a whole poll interval past end_us. _boundary_timer SCHEDULES the
# seek-back for the moment the boundary is expected, computed from the spec (and the
# playback rate) as soon as the seek that started the segment has completed. on_tick()
# stays as a backstop for a big jump past the boundary between polls (spec #166).
_MIN_BOUNDARY_TIMER_MS = 15
# Fade in/out: step interval for the volume ramp -- ~40ms is smooth without flooding VLC's HTTP
# interface (volume jobs are coalesced, so a slow VLC only ever gets the latest level).
_FADE_STEP_MS = 40
_MAX_VOLUME = 512  # VLC's scale: 256 = 100%, 512 = 200%


class LoopController(QObject):
    """Drives one A-B loop over `adapter`, polled via `on_tick` (spec #32, #34).

    Not a DAW-grade sample-accurate loop (spec #35) -- boundary detection is timer-
    scheduled from the spec plus a poll-driven backstop, not truly sample-accurate.

    Adapter calls go through `executor` (a ThreadedCommandQueue in the running app, so
    no network call blocks the UI; ImmediateExecutor by default, which keeps unit tests
    synchronous). State that depends on a command having *completed* -- arming the
    boundary timer, starting a fade -- happens in the command's completion callback,
    and every callback checks a generation counter so a stop()/start() issued while an
    earlier command was still in flight can't resurrect a loop that no longer exists.
    """

    loop_started = Signal(object)  # LoopSpec
    iteration_changed = Signal(object)  # remaining repeats, or None for infinite
    gap_started = Signal(int)  # gap_ms -- the controller resumes by itself after the gap
    loop_completed = Signal(object)  # CompletionAction that was applied
    bookmark_navigation_requested = Signal(object)  # CompletionAction (NEXT/PREVIOUS_BOOKMARK etc.)
    loop_failed = Signal(str)  # a playback command failed; the loop was stopped

    def __init__(
        self,
        adapter: "PlaybackAdapter",
        clock: PlaybackClock,
        parent: QObject | None = None,
        *,
        executor: CommandExecutor | None = None,
    ) -> None:
        super().__init__(parent)
        self._log = get_logger("LOOP")
        self._adapter = adapter
        self._clock = clock
        self._executor: CommandExecutor = executor or ImmediateExecutor()
        self._spec: LoopSpec | None = None
        self._remaining: int | None = None
        self._state = LoopState.IDLE
        self._generation = 0

        self._boundary_timer = QTimer(self)
        self._boundary_timer.setSingleShot(True)
        self._boundary_timer.timeout.connect(self._on_boundary_timer_fired)

        # Resumes playback after the gap between iterations (the controller owns this;
        # nobody else would resume a paused gap).
        self._gap_timer = QTimer(self)
        self._gap_timer.setSingleShot(True)
        self._gap_timer.timeout.connect(self.resume_after_gap)

        self._fade_out_timer = QTimer(self)
        self._fade_out_timer.setSingleShot(True)
        self._fade_out_timer.timeout.connect(lambda: self._begin_fade("out"))

        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._fade_active = False
        self._fade_direction = "in"
        self._fade_start_ns = 0
        self._fade_duration_us = 0
        # Best-known "real" listening volume (0-512), refreshed from live status polls
        # via set_target_volume() -- fades ramp between this and 0, never a hardcoded
        # "full volume", so a fade-in doesn't blast louder than the user's own setting.
        self._target_volume = 256
        # True while VLC's volume is somewhere this controller put it (mid-fade, or
        # ducked by a fade-out) rather than the user's own level. While set, status-
        # poll volumes are ignored, and stop()/completion restore the target first, so
        # a finished fade-out is never mistaken for the user's own (near-silent) level.
        self._volume_owned = False
        self._volume_fence_ns = 0

    # -- configuration --

    def set_adapter(self, adapter: "PlaybackAdapter") -> None:
        """Repoints this controller at a new adapter (switching which VLC instance the
        app talks to). Any loop against the old adapter is dropped, without touching
        the old VLC's volume."""
        self.stop(restore_volume=False)
        self._volume_owned = False
        self._adapter = adapter

    def set_target_volume(self, level: int, *, sampled_at_ns: int | None = None) -> None:
        """Called once per live status poll with VLC's current volume. Ignored while this
        controller owns the volume (a fade is running, or a fade-out left it ducked), and
        for polls issued before our last volume write had completed -- such a poll can
        still report a mid-ramp level, which would otherwise ratchet the target down."""
        if self._fade_active or self._volume_owned:
            return
        if sampled_at_ns is not None and sampled_at_ns < self._volume_fence_ns:
            return
        self._target_volume = max(0, min(_MAX_VOLUME, int(level)))

    @property
    def state(self) -> LoopState:
        return self._state

    @property
    def spec(self) -> LoopSpec | None:
        return self._spec

    # -- lifecycle --

    def start(self, spec: LoopSpec, *, before: Callable[[], object] | None = None) -> None:
        """Starts looping `spec`. `before` (e.g. switching VLC to the right playlist
        item) runs first, in the same command, so the seek can't overtake it."""
        self._cancel_timers()
        self._generation += 1
        generation = self._generation
        self._spec = spec
        self._remaining = spec.repeat_count
        self._state = LoopState.ARMED
        self._submit_segment_start(generation, first=True, before=before)

    def stop(self, *, restore_volume: bool = True) -> None:
        """User-initiated stop (spec #166 case): no completion action applied. Puts VLC's
        volume back to the user's level if a fade had moved it."""
        self._generation += 1
        self._cancel_timers()
        self._fade_active = False
        self._state = LoopState.IDLE
        self._spec = None
        self._remaining = None
        if restore_volume:
            self._restore_volume_if_owned()

    def on_tick(self, *, now_ns: int | None = None) -> None:
        """Backstop for a boundary crossed in one big jump between polls (spec #166:
        "end reached before status poll") -- the scheduled _boundary_timer is the
        primary detection path, this just catches whatever it might miss.
        """
        if self._state != LoopState.PLAYING or self._spec is None:
            return
        position_us = self._clock.estimated_position_us(now_ns=now_ns)
        if position_us >= self._spec.end_us:
            self._boundary_timer.stop()
            self._handle_boundary_reached()

    def resume_after_gap(self) -> None:
        if self._state != LoopState.GAP:
            return
        self._gap_timer.stop()
        self._state = LoopState.SEEKING_BACK
        self._submit_segment_start(self._generation, first=False)

    # -- internals --

    def _cancel_timers(self) -> None:
        self._boundary_timer.stop()
        self._gap_timer.stop()
        self._fade_out_timer.stop()
        self._fade_timer.stop()

    def _submit_segment_start(self, generation: int, *, first: bool,
                              before: Callable[[], object] | None = None) -> None:
        spec = self._spec
        assert spec is not None
        adapter = self._adapter
        fade_in = spec.fade_in_ms > 0
        restore = not fade_in and self._volume_owned
        target = self._target_volume

        def job() -> None:
            if before is not None:
                before()
            adapter.seek_absolute_us(spec.start_us)
            if fade_in:
                adapter.set_volume(0)
            elif restore:
                adapter.set_volume(target)
            adapter.play()

        fences = ("position", "volume") if (fade_in or restore) else ("position",)
        self._executor.submit(
            job,
            on_done=lambda _result: self._on_segment_started(generation, first=first, fade_in=fade_in,
                                                             touched_volume=fade_in or restore),
            on_error=lambda exc: self._on_command_failed(generation, exc),
            fences=fences,
        )

    def _on_segment_started(self, generation: int, *, first: bool, fade_in: bool, touched_volume: bool) -> None:
        if generation != self._generation or self._spec is None:
            return
        spec = self._spec
        self._clock.note_seek(spec.start_us, playing=True)
        if touched_volume:
            self._volume_fence_ns = time.monotonic_ns()
            self._volume_owned = fade_in  # at 0 for a fade-in; back at target otherwise
        self._state = LoopState.PLAYING
        if fade_in:
            self._begin_fade("in")
        if first:
            self.loop_started.emit(spec)
        else:
            self.iteration_changed.emit(self._remaining)
        self._arm_boundary_timer(spec.end_us - spec.start_us)

    def _on_command_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._generation:
            return
        self._log.info("loop command failed, stopping the loop: %s", exc)
        self._generation += 1
        self._cancel_timers()
        self._fade_active = False
        self._state = LoopState.IDLE
        self._spec = None
        self._remaining = None
        self.loop_failed.emit(str(exc))

    def _handle_boundary_reached(self) -> None:
        assert self._spec is not None
        self._boundary_timer.stop()
        self._fade_out_timer.stop()
        # A fade-out may still be mid-ramp exactly as the boundary hits -- stop it so it
        # can't keep writing volume after the decision below. _volume_owned stays set,
        # so the next segment start (or the completion) restores the level.
        self._fade_timer.stop()
        self._fade_active = False
        if self._remaining is not None:
            self._remaining -= 1

        if self._remaining is not None and self._remaining <= 0:
            self._state = LoopState.COMPLETED
            self._apply_completion_action()
            return

        if self._spec.gap_ms > 0:
            self._state = LoopState.GAP
            adapter = self._adapter
            generation = self._generation
            self._executor.submit(
                adapter.pause, fences=("position",),
                on_error=lambda exc: self._on_command_failed(generation, exc),
            )
            self.gap_started.emit(self._spec.gap_ms)
            if self._state == LoopState.GAP:  # a gap_started listener may have resumed already
                self._gap_timer.start(self._spec.gap_ms)
        else:
            self._state = LoopState.SEEKING_BACK
            self._submit_segment_start(self._generation, first=False)

    def _apply_completion_action(self) -> None:
        assert self._spec is not None
        action = self._spec.completion_action
        # Whatever happens next, don't leave VLC ducked by this loop's fade-out.
        self._restore_volume_if_owned()
        adapter = self._adapter
        command: Callable[[], object] | None = None
        if action is CompletionAction.PAUSE:
            command = adapter.pause
        elif action is CompletionAction.STOP:
            command = adapter.stop
        elif action is CompletionAction.NEXT_TRACK:
            command = adapter.next_track
        elif action in (
            CompletionAction.NEXT_BOOKMARK,
            CompletionAction.PREVIOUS_BOOKMARK,
            CompletionAction.NEXT_SEGMENT_QUEUE_ITEM,
        ):
            # Bookmark/queue navigation needs data this class doesn't own; the caller
            # (which does own the bookmark list) handles it.
            self.bookmark_navigation_requested.emit(action)
        if command is not None:
            self._executor.submit(command, fences=("position",), on_error=lambda exc: self._log.info(
                "completion action %s failed: %s", action.value, exc))
        # CONTINUE: leave playback exactly as it is.
        self.loop_completed.emit(action)

    def _restore_volume_if_owned(self) -> None:
        if not self._volume_owned:
            return
        adapter = self._adapter
        target = self._target_volume
        self._volume_owned = False
        self._executor.submit(
            lambda: adapter.set_volume(target), coalesce_key="volume", fences=("volume",),
            on_done=lambda _result: self._mark_volume_written(),
            on_error=lambda exc: self._log.info("volume restore failed: %s", exc),
        )

    def _mark_volume_written(self) -> None:
        self._volume_fence_ns = time.monotonic_ns()

    # -- scheduled boundary timer (precision loop-back, independent of poll cadence) --

    def _arm_boundary_timer(self, segment_us: int) -> None:
        rate = self._clock.rate
        delay_ms = max(_MIN_BOUNDARY_TIMER_MS, int(segment_us / rate / 1000))
        self._boundary_timer.start(delay_ms)
        self._arm_fade_out(delay_ms)

    def _on_boundary_timer_fired(self) -> None:
        if self._state != LoopState.PLAYING or self._spec is None:
            return
        self._handle_boundary_reached()

    # -- fade in/out (spec: "add options to fade in and fade out when playing back") --

    def _arm_fade_out(self, segment_ms: int) -> None:
        self._fade_out_timer.stop()
        if self._spec is None or self._spec.fade_out_ms <= 0:
            return
        fade_out_ms = min(self._spec.fade_out_ms, segment_ms)
        lead_ms = segment_ms - fade_out_ms
        if lead_ms <= 0:
            self._begin_fade("out")
        else:
            self._fade_out_timer.start(lead_ms)

    def _begin_fade(self, direction: str) -> None:
        if self._spec is None:
            return
        duration_ms = self._spec.fade_in_ms if direction == "in" else self._spec.fade_out_ms
        if duration_ms <= 0:
            return
        self._fade_direction = direction
        self._fade_start_ns = time.monotonic_ns()
        self._fade_duration_us = duration_ms * 1000
        self._fade_active = True
        self._volume_owned = True
        self._fade_timer.start(_FADE_STEP_MS)

    def _on_fade_tick(self) -> None:
        if not self._fade_active:
            self._fade_timer.stop()
            return
        elapsed_us = (time.monotonic_ns() - self._fade_start_ns) / 1000.0
        fraction = min(1.0, elapsed_us / self._fade_duration_us) if self._fade_duration_us > 0 else 1.0
        if self._fade_direction == "in":
            level = fraction * self._target_volume
        else:
            level = (1.0 - fraction) * self._target_volume
        level_int = max(0, min(_MAX_VOLUME, int(round(level))))
        adapter = self._adapter
        self._executor.submit(
            lambda: adapter.set_volume(level_int), coalesce_key="volume", fences=("volume",),
            on_done=lambda _result: self._mark_volume_written(),
            on_error=lambda exc: self._log.debug("fade step failed: %s", exc),
        )
        if fraction >= 1.0:
            self._fade_active = False
            self._fade_timer.stop()
            if self._fade_direction == "in":
                self._volume_owned = False  # back at the user's own level
