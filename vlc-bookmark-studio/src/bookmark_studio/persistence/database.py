"""SQLite connection factory with WAL + foreign_keys pragmas (spec #71)."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    # sqlite3.connect() does not create missing parent directories -- on a genuine
    # first run (no %LOCALAPPDATA%\VLCBookmarkStudio\data\ yet) this crashed the app
    # at startup before a single line of UI code ran, caught only by actually running
    # the real entry point rather than testing against a pre-existing tmp_path.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
