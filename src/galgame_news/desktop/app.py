"""Desktop application entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from .controller import DesktopController
from .theme import apply_theme
from .window import MainWindow


def create_application(argv: Sequence[str] | None = None) -> tuple[QApplication, MainWindow]:
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationDisplayName("周报图片采集工具箱")
    app.setApplicationVersion("1.0.0")
    controller = DesktopController()
    window = MainWindow(controller)
    try:
        apply_theme(controller.settings_page._settings.theme, app)
    except AttributeError:
        apply_theme("system", app)
    return app, window


def main(argv: Sequence[str] | None = None) -> int:
    app, window = create_application(argv)
    window.show()
    return int(app.exec())


__all__ = ["create_application", "main"]
