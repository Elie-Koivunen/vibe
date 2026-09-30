from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSettings

from bookmark_studio.logging.setup import configure_logging, get_logger
from bookmark_studio.settings.settings_service import SettingsService


def _ini_settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)


def test_bridge_token_is_generated_once_and_persists(tmp_path: Path) -> None:
    service = SettingsService(_ini_settings(tmp_path))
    token1 = service.bridge_token()
    token2 = service.bridge_token()
    assert token1 == token2
    assert len(token1) > 20


def test_bridge_token_persists_across_instances(tmp_path: Path) -> None:
    ini_path = tmp_path / "settings.ini"
    service1 = SettingsService(QSettings(str(ini_path), QSettings.IniFormat))
    token = service1.bridge_token()
    service1.sync()

    service2 = SettingsService(QSettings(str(ini_path), QSettings.IniFormat))
    assert service2.bridge_token() == token


def test_known_vlc_ports_add_and_remove_round_trip(tmp_path: Path) -> None:
    service = SettingsService(_ini_settings(tmp_path))
    assert service.known_vlc_ports() == []

    service.add_known_vlc_port(43120)
    service.add_known_vlc_port(43121)
    service.add_known_vlc_port(43120)  # duplicate, should not create a second entry
    assert service.known_vlc_ports() == [43120, 43121]

    service.remove_known_vlc_port(43120)
    assert service.known_vlc_ports() == [43121]


def test_theme_rejects_unknown_value(tmp_path: Path) -> None:
    service = SettingsService(_ini_settings(tmp_path))
    try:
        service.set_theme("neon")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_default_bridge_port(tmp_path: Path) -> None:
    service = SettingsService(_ini_settings(tmp_path))
    assert service.bridge_port() == 43119
    service.set_bridge_port(50000)
    assert service.bridge_port() == 50000


def test_logging_redacts_authorization_content(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logger = get_logger("BRIDGE")
    logger.info("sending request with Authorization: Basic abc123")
    for handler in logging.getLogger("bookmark_studio").handlers:
        handler.flush()

    log_file = tmp_path / "bookmarkstudio.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "abc123" not in content
    assert "redacted" in content


def test_get_logger_rejects_unknown_category() -> None:
    try:
        get_logger("NOT_A_CATEGORY")
        raised = False
    except ValueError:
        raised = True
    assert raised
