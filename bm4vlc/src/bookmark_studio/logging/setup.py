"""Configures rotating log handlers under the app's local-appdata logs directory (spec #122)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

CATEGORIES = (
    "APP", "VLC", "BRIDGE", "PLAYLIST", "MEDIA", "WAVEFORM", "BOOKMARK", "LOOP",
    "DATABASE", "IMPORT", "EXPORT", "UI",
)


class _RedactAuthorizationFilter(logging.Filter):
    """Never let an HTTP Authorization header reach a log line (spec #121)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "authorization" in message.lower() or "bridge/token" in message.lower():
            record.msg = "[redacted: message referenced an auth token]"
            record.args = ()
        return True


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "VLCBookmarkStudio" / "logs"


def configure_logging(log_dir: Path | None = None, *, level: int = logging.INFO) -> logging.Logger:
    log_dir = log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bookmark_studio")
    root.setLevel(level)
    root.handlers.clear()

    handler = RotatingFileHandler(
        log_dir / "bookmarkstudio.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.addFilter(_RedactAuthorizationFilter())
    root.addHandler(handler)
    return root


def get_logger(category: str) -> logging.Logger:
    if category not in CATEGORIES:
        raise ValueError(f"unknown logging category {category!r}; expected one of {CATEGORIES}")
    return logging.getLogger(f"bookmark_studio.{category}")
