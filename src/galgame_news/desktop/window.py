"""Main window and five-page navigation."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QWidget,
    QApplication,
    QMessageBox,
)

from .controller import DesktopController
from .ui import icon
from .resources import CharacterArt, configure_branding


class MainWindow(QMainWindow):
    PAGE_NAMES = ("新建任务", "抓取进度", "图片审核", "历史任务", "设置")

    def __init__(
        self,
        controller: DesktopController | None = None,
        parent: QWidget | None = None,
        *,
        exit_handler: Callable[[], object] | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("mainWindow")
        self.setWindowTitle("周报图片采集工具箱")
        self.setWindowIcon(configure_branding(QApplication.instance()))
        self.resize(1280, 820)
        self.setMinimumSize(1100, 720)
        self.controller = controller or DesktopController(parent=self)
        self._exit_handler = exit_handler
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.nav_list = self.navigation
        self.navigation.setMinimumWidth(155)
        for name, symbol in zip(self.PAGE_NAMES, ("new", "progress", "review", "history", "settings")):
            self.navigation.addItem(QListWidgetItem(icon(symbol), name))
        self.pages = QStackedWidget()
        self.stacked_widget = self.pages
        self.new_task_page = self.controller.new_task_page
        self.progress_page = self.controller.progress_page
        self.review_page = self.controller.review_page
        self.history_page = self.controller.history_page
        self.settings_page = self.controller.settings_page
        self._exit_committed = False
        self._force_close_prompt_open = False
        for page in (self.new_task_page, self.progress_page, self.review_page, self.history_page, self.settings_page):
            self.pages.addWidget(page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(192)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 26, 16, 18)
        side.addWidget(CharacterArt(154, 90), alignment=Qt.AlignmentFlag.AlignHCenter)
        brand = QLabel("周报图片\n采集工具箱")
        brand.setObjectName("brand")
        side.addWidget(brand)
        tagline = QLabel("采集 · 筛选 · 审阅")
        tagline.setObjectName("muted")
        side.addWidget(tagline)
        side.addSpacing(25)
        side.addWidget(self.navigation, 1)
        footer = QLabel("让每张配图都有出处")
        footer.setObjectName("muted")
        footer.setWordWrap(True)
        side.addWidget(footer)
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(18)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.controller.task_started.connect(lambda *_: self.navigation.setCurrentRow(1))
        self.controller.review_loaded.connect(lambda *_: self.navigation.setCurrentRow(2))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange) and hasattr(self, "navigation"):
            for index, symbol in enumerate(("new", "progress", "review", "history", "settings")):
                if (item := self.navigation.item(index)) is not None:
                    item.setIcon(icon(symbol))

    def _confirm_force_close(self) -> bool:
        dialog = QMessageBox(QMessageBox.Icon.Warning, "退出确认", "任务正在进行，退出将中断任务，尚未保存的审核改动可能丢失。是否退出？", self)
        exit_button = dialog.addButton("退出", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel_button)
        dialog.exec()
        return dialog.clickedButton() is exit_button

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._exit_committed:
            event.accept()
            self.hide()
            return
        if self._force_close_prompt_open:
            event.ignore()
            return
        if self.controller.has_active_operations:
            self._force_close_prompt_open = True
            try:
                if not self._confirm_force_close():
                    event.ignore()
                    return
            finally:
                self._force_close_prompt_open = False

        try:
            self.controller.begin_force_close()
        except Exception:
            # A cleanup error must not leave the application window stuck open.
            pass
        finally:
            self._exit_committed = True
            event.accept()
            self.hide()
            if self._exit_handler is not None:
                self._exit_handler()


__all__ = ["MainWindow"]
