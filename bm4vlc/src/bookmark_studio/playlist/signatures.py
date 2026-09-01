"""Strict ordered-media-identity playlist signatures (spec #11)."""
from __future__ import annotations

import hashlib
from uuid import UUID


def strict_signature(ordered_media_ids: list[UUID]) -> str:
    return hashlib.sha256(b"\0".join(m.bytes for m in ordered_media_ids)).hexdigest()
