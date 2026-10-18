"""Desktop application entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from .controller import DesktopController
from .theme import apply_theme
from .window import MainWindow
from .process_exit import DesktopProcessExit


def create_application(argv: Sequence[str] | None = None, *, exit_handler=None) -> tuple[QApplication, MainWindow]:
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationDisplayName("周报图片采集工具箱")
    app.setApplicationVersion("1.1.0")
    controller = DesktopController()
    window = MainWindow(controller, exit_handler=exit_handler)
    try:
        apply_theme(controller.settings_page._settings.theme, app)
    except AttributeError:
        apply_theme("system", app)
    return app, window


def main(argv: Sequence[str] | None = None) -> int:
    process_exit = DesktopProcessExit()
    app, window = create_application(argv, exit_handler=process_exit.force_exit)
    window.show()
    result = int(app.exec())
    # Qt can also quit from a menu/system request rather than closeEvent.
    process_exit.force_exit(result)
    return result  # Only reachable with an injected test exit function.


__all__ = ["create_application", "main"]
