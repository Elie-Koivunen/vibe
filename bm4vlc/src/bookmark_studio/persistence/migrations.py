"""Applies numbered SQL migration files from migrations/ in order (spec #126)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def _migrations_dir() -> Path:
    # migrations/ lives at the repo root, two levels above src/bookmark_studio/persistence/
    return Path(__file__).resolve().parents[3] / "migrations"


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """Returns (version, path) pairs sorted by version, from *_NNN.sql filenames."""
    directory = directory or _migrations_dir()
    found: list[tuple[int, Path]] = []
    for path in directory.glob("*.sql"):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda pair: pair[0])
    return found


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else 0


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> int:
    """Applies every migration newer than the current schema version. Returns new version.

    Sets PRAGMA foreign_keys = ON itself (rather than trusting every caller to have set
    it) -- forgetting it silently disables FK enforcement with no error, which would
    otherwise let a broken import or a stray direct SQL write corrupt referential
    integrity without anyone noticing.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    applied_from = current_version(conn)
    latest = applied_from
    for version, path in discover_migrations(directory):
        if version <= applied_from:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
        latest = version
    return latest
