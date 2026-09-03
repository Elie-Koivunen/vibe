"""Software A-B loop state machine driven over a PlaybackAdapter (spec #33-#35)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from bookmark_studio.domain.enums import CompletionAction, LoopState
from bookmark_studio.domain.loop import LoopSpec
from bookmark_studio.playback.playback_clock import PlaybackClock

if TYPE_CHECKING:
    from bookmark_studio.playback.adapter import PlaybackAdapter

__all__ = ["LoopState", "LoopSpec", "LoopController"]

# Direct user report: "it still drifts outside of the bookmark area". The previous
# design only ever detected the loop boundary from on_tick(), called once per
# ~400ms status poll (app/application.py's STATUS_POLL_MS) -- by the time a poll
# noticed playback had crossed end_us, real playback could already be up to that
# whole interval (plus the seek command's own round-trip) past the marked bookmark
# end. _boundary_timer instead SCHEDULES the seek-back for the moment we expect the
# boundary to actually be crossed, computed directly from the spec right after each
# seek (PlaybackClock's own sample is still stale at that instant -- it only updates
# from the next poll, so it can't be trusted yet). on_tick() is kept as a backstop
# for cases the timer doesn't cover (e.g. a big jump past the boundary between polls,
# spec #166), not as the primary detection path anymore.
_MIN_BOUNDARY_TIMER_MS = 15
# Direct user request: "add options to fade in and fade out when playing back".
# Step interval for the volume ramp -- ~40ms is smooth without spamming VLC's HTTP
# interface (each set_volume() call is a real, blocking network round-trip, same as
# every other adapter call this controller already makes synchronously).
_FADE_STEP_MS = 40


class LoopController(QObject):
    """Drives one A-B loop over `adapter`, polled via `on_tick` (spec #32, #34).

    Not a DAW-grade sample-accurate loop (spec #35) -- boundary detection is timer-
    scheduled from the spec plus a poll-driven backstop, not truly sample-accurate.
    """

    loop_started = Signal(object)  # LoopSpec
    iteration_changed = Signal(object)  # remaining repeats, or None for infinite
    gap_started = Signal(int)  # gap_ms -- caller schedules resume_after_gap()
    loop_completed = Signal(object)  # CompletionAction that was applied
    bookmark_navigation_requested = Signal(object)  # CompletionAction (NEXT/PREVIOUS_BOOKMARK etc.)

    def __init__(self, adapter: "PlaybackAdapter", clock: PlaybackClock, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._adapter = adapter
        self._clock = clock
        self._spec: LoopSpec | None = None
        self._remaining: int | None = None
        self._state = LoopState.IDLE

        self._boundary_timer = QTimer(self)
        self._boundary_timer.setSingleShot(True)
        self._boundary_timer.timeout.connect(self._on_boundary_timer_fired)

        self._fade_out_timer = QTimer(self)
        self._fade_out_timer.setSingleShot(True)
        self._fade_out_timer.timeout.connect(lambda: self._begin_fade("out"))

        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._fade_active = False
        self._fade_direction = "in"
        self._fade_start_ns = 0
        self._fade_duration_us = 0
        # Best-known "real" listening volume (0-256), refreshed from live status
        # polls via set_target_volume() -- fades ramp between this and 0, never a
        # hardcoded "full volume", so a fade-in doesn't blast louder than whatever
        # the user actually had it set to.
        self._target_volume = 256

    def set_adapter(self, adapter: "PlaybackAdapter") -> None:
        """Repoints this controller at a new adapter (spec: switching which VLC
        instance the app talks to, mid-session, via the launch/attach picker). Any
        loop already ARMED/PLAYING against the old adapter is dropped rather than
        left driving a VLC process that just got disconnected.
        """
        self._adapter = adapter
        self.stop()

    def set_target_volume(self, level: int) -> None:
        """Called once per live status poll (Application._on_status_result) with the
        real, current VLC volume. Ignored while a fade is actively running -- our own
        ramp's intermediate set_volume() calls would otherwise be read back as if the
        user had manually retargeted the volume, corrupting the fade's own endpoint.
        """
        if not self._fade_active:
            self._target_volume = max(0, min(256, level))

    @property
    def state(self) -> LoopState:
        return self._state

    @property
    def spec(self) -> LoopSpec | None:
        return self._spec

    def start(self, spec: LoopSpec) -> None:
        self._spec = spec
        self._remaining = spec.repeat_count
        self._state = LoopState.ARMED
        self._adapter.seek_absolute_us(spec.start_us)
        self._start_fade_in()
        self._adapter.play()
        self._state = LoopState.PLAYING
        self.loop_started.emit(spec)
        self._arm_boundary_timer(spec.end_us - spec.start_us)

    def stop(self) -> None:
        """User-initiated stop (spec #166 case): no completion action applied."""
        self._boundary_timer.stop()
        self._fade_out_timer.stop()
        self._fade_timer.stop()
        self._fade_active = False
        self._state = LoopState.IDLE
        self._spec = None
        self._remaining = None

    def on_tick(self, *, now_ns: int | None = None) -> None:
        """Backstop for on_tick-era callers and for a boundary crossed in one big
        jump between polls (spec #166: "end reached before status poll") -- the
        scheduled _boundary_timer is the primary detection path now (see module
        docstring), this just catches whatever it might miss.
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
        self._seek_and_continue()

    def _handle_boundary_reached(self) -> None:
        assert self._spec is not None
        self._boundary_timer.stop()
        self._fade_out_timer.stop()
        # A fade-out started this iteration may still be mid-ramp exactly as the
        # boundary hits (its window is sized off the same segment length) -- stop it
        # here so it can't keep firing set_volume() after the seek-back/gap-pause
        # below have already decided what the volume should be.
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
            self._adapter.pause()
            self.gap_started.emit(self._spec.gap_ms)
        else:
            self._seek_and_continue()

    def _seek_and_continue(self) -> None:
        assert self._spec is not None
        self._state = LoopState.SEEKING_BACK
        self._adapter.seek_absolute_us(self._spec.start_us)
        self._start_fade_in()
        self._adapter.play()
        self._state = LoopState.PLAYING
        self.iteration_changed.emit(self._remaining)
        self._arm_boundary_timer(self._spec.end_us - self._spec.start_us)

    def _apply_completion_action(self) -> None:
        assert self._spec is not None
        action = self._spec.completion_action
        if action is CompletionAction.PAUSE:
            self._adapter.pause()
        elif action is CompletionAction.STOP:
            self._adapter.stop()
        elif action is CompletionAction.NEXT_TRACK:
            self._adapter.next_track()
        elif action in (
            CompletionAction.NEXT_BOOKMARK,
            CompletionAction.PREVIOUS_BOOKMARK,
            CompletionAction.NEXT_SEGMENT_QUEUE_ITEM,
        ):
            # Bookmark/queue navigation needs data this class doesn't own; the caller
            # (which does own the bookmark list / segment queue) handles it.
            self.bookmark_navigation_requested.emit(action)
        # CONTINUE: leave playback exactly as it is.
        self.loop_completed.emit(action)

    # -- scheduled boundary timer (precision loop-back, independent of poll cadence) --

    def _arm_boundary_timer(self, delay_us: int) -> None:
        delay_ms = max(_MIN_BOUNDARY_TIMER_MS, int(delay_us / 1000))
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

    def _start_fade_in(self) -> None:
        if self._spec is None:
            return
        if self._spec.fade_in_ms > 0:
            self._adapter.set_volume(0)
            self._begin_fade("in")
        elif self._spec.fade_out_ms > 0:
            # No fade-in configured, but a fade-out on THIS spec may have just ducked
            # the volume down toward the boundary we're seeking back from -- restore
            # it immediately so the loop doesn't stay silent forever after just one
            # iteration. A spec with neither fade configured never touches volume at
            # all, same as before this feature existed.
            self._adapter.set_volume(self._target_volume)

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
        self._fade_timer.start(_FADE_STEP_MS)

    def _on_fade_tick(self) -> None:
        if not self._fade_active:
            self._fade_timer.stop()
            return
        elapsed_us = (time.monotonic_ns() - self._fade_start_ns) / 1000.0
        fraction = min(1.0, elapsed_us / self._fade_duration_us) if self._fade_duration_us > 0 else 1.0
        level = fraction * self._target_volume if self._fade_direction == "in" else (1.0 - fraction) * self._target_volume
        self._adapter.set_volume(max(0, min(256, int(level))))
        if fraction >= 1.0:
            self._fade_active = False
            self._fade_timer.stop()
