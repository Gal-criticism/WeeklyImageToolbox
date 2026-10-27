"""Offline-friendly UI surface for per-news historical material candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ArchivePanel(QWidget):
    """Displays all image and video candidates supplied by a controller."""

    asset_activated = Signal(object)
    adopt_requested = Signal(object)
    preview_requested = Signal(object)
    refresh_requested = Signal()
    query_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.current_news_id: str | None = None
        self._assets: list[Any] = []
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("手动输入搜索关键词")
        self.search_button = QPushButton("搜索")
        self.refresh_button = QPushButton("更新")
        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(96, 68))
        self.list_widget.setWordWrap(True)
        self.list_widget.setUniformItemSizes(False)
        self.list_widget.itemDoubleClicked.connect(self._activate_item)
        self.list_widget.itemClicked.connect(self._activate_item)
        self.list_widget.itemActivated.connect(self._activate_item)
        self.preview_button = QPushButton("预览")
        self.adopt_button = QPushButton("采用")
        self.preview_button.clicked.connect(lambda: self._emit_selected(self.preview_requested))
        self.adopt_button.clicked.connect(lambda: self._emit_selected(self.adopt_requested))
        self.search_button.clicked.connect(lambda: self.query_requested.emit(self.keyword_edit.text().strip()))
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.message_label = QLabel("当前新闻暂无往期周报素材")
        self.message_label.setObjectName("muted")
        self.message_label.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.keyword_edit, 1)
        top.addWidget(self.search_button)
        top.addWidget(self.refresh_button)
        actions = QHBoxLayout()
        actions.addWidget(self.preview_button)
        actions.addWidget(self.adopt_button)
        actions.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.message_label)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(actions)

    @property
    def asset_count(self) -> int:
        return len(self._assets)

    @property
    def asset_kinds(self) -> list[str]:
        return [str(getattr(asset, "kind", "")) for asset in self._assets]

    def set_assets(self, news_id: str, assets: list[Any], message: str = "") -> None:
        self.current_news_id = str(news_id)
        chosen: list[Any] = []
        for asset in assets:
            kind = str(getattr(asset, "kind", "")).casefold()
            if kind not in {"image", "video"}:
                continue
            chosen.append(asset)
        self._assets = chosen
        self.list_widget.clear()
        for asset in chosen:
            row = QListWidgetItem(self._asset_card_text(asset))
            row.setData(Qt.ItemDataRole.UserRole, asset)
            row.setSizeHint(QSize(280, 100))
            self._set_local_thumbnail(row, asset)
            self.list_widget.addItem(row)
        self.set_status(message or ("找到历史素材" if chosen else "当前新闻暂无往期周报素材"))

    @staticmethod
    def _format_size(size: Any) -> str:
        try:
            value = max(0, int(size))
        except (TypeError, ValueError):
            return "大小未知"
        if value < 1024:
            return f"{value} B"
        if value < 1024**2:
            return f"{value / 1024:.0f} KB"
        if value < 1024**3:
            return f"{value / 1024**2:.1f} MB"
        return f"{value / 1024**3:.1f} GB"

    @classmethod
    def _asset_card_text(cls, asset: Any) -> str:
        kind = str(getattr(asset, "kind", "")).casefold()
        name = str(getattr(asset, "name", None) or getattr(asset, "path", "") or "历史素材")
        issue = str(getattr(asset, "issue_number", None) or "期号未知")
        source_kind = str(getattr(asset, "source_kind", None) or "来源未知")
        size = cls._format_size(getattr(asset, "size", None))
        title = str(getattr(asset, "news_title", None) or "新闻标题未知")
        issue_date = str(getattr(asset, "issue_date", None) or "日期未知")
        version = str(getattr(asset, "version", None) or "版本待核")
        date_label = f"日期待核（{issue_date}）" if getattr(asset, "date_suspect", False) else issue_date
        label = "图片" if kind == "image" else "视频"
        return (
            f"{label} · {name}\n"
            f"期号：{issue} · 来源：{source_kind} · 大小：{size}\n"
            f"日期：{date_label} · 版本：{version}\n"
            f"新闻：{title}"
        )

    @staticmethod
    def _set_local_thumbnail(row: QListWidgetItem, asset: Any) -> None:
        if str(getattr(asset, "kind", "")).casefold() != "image":
            return
        local_path = getattr(asset, "local_path", None)
        if not local_path:
            return
        raw_path = str(local_path)
        if raw_path.startswith("\\\\") or "://" in raw_path:
            return
        path = Path(raw_path)
        if not path.is_file():
            return
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                QSize(96, 68), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            row.setIcon(QIcon(scaled))

    def set_status(self, message: str) -> None:
        self.message_label.setText(str(message or ""))

    def _selected_asset(self) -> Any | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _emit_selected(self, signal: Any) -> None:
        asset = self._selected_asset()
        if asset is not None:
            signal.emit(asset)

    def _activate_item(self, item: QListWidgetItem) -> None:
        asset = item.data(Qt.ItemDataRole.UserRole)
        if asset is not None:
            self.asset_activated.emit(asset)
