"""Local branding resources, independent of cwd in source and frozen builds."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sys
from PIL import Image

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QLabel


def resource_path(name: str) -> Path:
    if name not in {"character.png", "toolbox.ico"}:
        raise ValueError("Unknown toolbox resource")
    package = Path(__file__).resolve().parent / "assets" / name
    if package.is_file():
        return package
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        frozen = Path(bundle) / "galgame_news" / "desktop" / "assets" / name
        if frozen.is_file():
            return frozen
    raise FileNotFoundError(f"工具箱资源缺失：{name}")


@lru_cache(maxsize=1)
def _character_pixmap() -> QPixmap:
    # Called only from widget constructors on the GUI thread. Crop transparent
    # margins for display; the packaged original remains byte-for-byte intact.
    pixmap = QPixmap(str(resource_path("character.png")))
    if pixmap.isNull():
        raise ValueError("角色图片无法解码")
    with Image.open(resource_path("character.png")) as image:
        bounds = image.getchannel("A").getbbox()
    if bounds:
        left, top, right, bottom = bounds
        return pixmap.copy(left, top, right - left, bottom - top)
    return pixmap


class CharacterArt(QLabel):
    def __init__(self, width: int, height: int, parent=None):
        super().__init__(parent)
        self.setObjectName("characterArt")
        self.setFixedSize(width, height)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        image = _character_pixmap().scaled(
            width * 3, height * 3, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        image.setDevicePixelRatio(3)
        self.setPixmap(image)


def configure_branding(app: QApplication) -> QIcon:
    artwork = QIcon(str(resource_path("toolbox.ico")))
    app.setWindowIcon(artwork)
    if sys.platform == "win32" and not app.property("toolboxAppIdReady"):
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("kasumi.WeeklyImages.Toolbox")
        except (AttributeError, OSError):
            pass
        app.setProperty("toolboxAppIdReady", True)
    return artwork
