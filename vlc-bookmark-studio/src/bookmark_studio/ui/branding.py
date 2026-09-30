"""The product name and logo, rendered from resources/icon.svg at whatever size a
place needs (window and taskbar icon, About box, dialog headers)."""
from __future__ import annotations

import sys
from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

APP_NAME = "VLC Bookmark Studio"
_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
# Groups the app's windows under its own taskbar button and icon on Windows (instead of
# python.exe's when run from source).
WINDOWS_APP_ID = "VLCBookmarkStudio.App"


def logo_svg() -> bytes:
    return (resources.files("bookmark_studio") / "resources" / "icon.svg").read_bytes()


@lru_cache(maxsize=None)
def logo_image(size: int) -> QImage:
    renderer = QSvgRenderer(QByteArray(logo_svg()))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return image


def logo_pixmap(size: int) -> QPixmap:
    return QPixmap.fromImage(logo_image(size))


def app_icon() -> QIcon:
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(logo_pixmap(size))
    return icon


def set_windows_app_id() -> None:
    """Before the first window: makes Windows show this app's icon in the taskbar."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):  # very old Windows: cosmetic only
        pass


__all__ = ["APP_NAME", "app_icon", "logo_image", "logo_pixmap", "logo_svg", "set_windows_app_id"]
