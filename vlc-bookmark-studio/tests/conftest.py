from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """Nothing a test does may touch the real per-user data folder (database, private
    VLC config files, sync state): every test gets its own."""
    monkeypatch.setenv("VLC_BOOKMARK_STUDIO_DATA_DIR", str(tmp_path_factory.mktemp("vlc-bookmark-studio-data")))
    monkeypatch.delenv("BM4VLC_DATA_DIR", raising=False)
