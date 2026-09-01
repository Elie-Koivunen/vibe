"""Fast media fingerprint: sha256(size + first/middle/last 1 MiB) (spec #68)."""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
SMALL_FILE_THRESHOLD = CHUNK_SIZE * 3


def fast_fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(size.to_bytes(8, "big"))
    with path.open("rb") as f:
        if size <= SMALL_FILE_THRESHOLD:
            digest.update(f.read())
        else:
            digest.update(f.read(CHUNK_SIZE))
            f.seek(size // 2)
            digest.update(f.read(CHUNK_SIZE))
            f.seek(max(size - CHUNK_SIZE, 0))
            digest.update(f.read(CHUNK_SIZE))
    return digest.hexdigest()
