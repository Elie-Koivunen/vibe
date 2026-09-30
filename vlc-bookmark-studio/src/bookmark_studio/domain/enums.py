from __future__ import annotations

from enum import Enum, auto


class BookmarkType(str, Enum):
    POINT = "point"
    SEGMENT = "segment"


class BookmarkScope(str, Enum):
    PLAYLIST_MEDIA = "playlist_media"
    GLOBAL_MEDIA = "global_media"


class CompletionAction(str, Enum):
    CONTINUE = "continue"
    PAUSE = "pause"
    STOP = "stop"
    NEXT_BOOKMARK = "next_bookmark"
    PREVIOUS_BOOKMARK = "previous_bookmark"
    NEXT_SEGMENT_QUEUE_ITEM = "next_segment_queue_item"
    NEXT_TRACK = "next_track"


class LoopState(Enum):
    IDLE = auto()
    ARMED = auto()
    PLAYING = auto()
    SEEKING_BACK = auto()
    GAP = auto()
    COMPLETED = auto()


class SeekCapability(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    FAILED = "failed"
