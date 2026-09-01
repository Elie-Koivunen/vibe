"""Software A-B loop state machine driven over a PlaybackAdapter (spec #33-#35)."""
from __future__ import annotations

from bookmark_studio.domain.enums import LoopState
from bookmark_studio.domain.loop import LoopSpec

__all__ = ["LoopState", "LoopSpec"]
