"""Non-destructive task history page."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QPushButton, QVBoxLayout, QWidget, QLineEdit, QLabel

from ..tasks import TaskRecord, TaskStore
from .ui import page_header


class HistoryPage(QWidget):
    resume_requested = Signal(object)
    review_requested = Signal(object)
    open_requested = Signal(object)
    migration_requested = Signal(object)

    def __init__(self, store: TaskStore | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("historyPage")
        self.store = store or TaskStore()
        self.records: list[TaskRecord] = []
        self._visible_records: list[TaskRecord] = []
        self._migration_busy = False
        self._migration_blocked = False
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按期号搜索历史任务")
        self.search_edit.textChanged.connect(self._filter)
        self.empty_label = QLabel("暂无历史任务，可在新建任务页导入已有结果。")
        self.empty_label.setObjectName("muted")
        self.task_list = QListWidget()
        self.task_list.setObjectName("historyList")
        self.resume_button = QPushButton("继续任务")
        self.review_button = QPushButton("查看图片")
        self.review_button.setProperty("primary", True)
        self.open_button = QPushButton("打开目录")
        self.remove_button = QPushButton("移出历史")
        self.migrate_button = QPushButton("迁移任务存储")
        self.migration_status = QLabel("")
        self.migration_status.setObjectName("muted")
        self.remove_button.setToolTip("仅移出列表，原有任务文件保留")
        self.resume_button.clicked.connect(self.resume_selected)
        self.review_button.clicked.connect(self.review_selected)
        self.open_button.clicked.connect(self.open_selected)
        self.remove_button.clicked.connect(self.remove_selected)
        self.migrate_button.clicked.connect(self.migrate_selected)
        actions = QHBoxLayout()
        for button in (self.resume_button, self.review_button, self.open_button, self.remove_button, self.migrate_button):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(14)
        layout.addWidget(page_header("历史任务", "失败任务可继续抓取；任务输出保留在原目录，可继续审核或恢复。"))
        layout.addWidget(self.search_edit)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.task_list)
        layout.addLayout(actions)
        layout.addWidget(self.migration_status)
        self.task_list.currentRowChanged.connect(self._update_actions)
        self.refresh()

    def refresh(self) -> None:
        self.records = list(self.store.list_tasks())
        self._filter()

    def _filter(self, *_args) -> None:
        query = self.search_edit.text().strip().casefold()
        self._visible_records = [record for record in self.records if query in record.issue_id.casefold()]
        self.task_list.clear()
        for record in self._visible_records:
            try:
                created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                created = "创建时间未知"
            label = f"第 {record.issue_id} 期    ·    {created}"
            if record.imported:
                label += "    ·    已导入"
            elif record.images_only:
                label += "    ·    仅图片"
            label += f"\n{record.task_id}"
            item = self.task_list.addItem(label)
            _ = item
            self.task_list.item(self.task_list.count() - 1).setData(256, record.task_id)
        self.empty_label.setVisible(not self._visible_records)
        self.empty_label.setText("没有匹配的期号" if query else "暂无历史任务，可在新建任务页导入已有结果。")
        self._update_actions()

    def _update_actions(self, *_args) -> None:
        selected = self.selected_record()
        for button in (self.review_button, self.open_button, self.remove_button):
            button.setEnabled(selected is not None)
        self.migrate_button.setEnabled(selected is not None and not self._migration_busy and not self._migration_blocked)
        compact = selected is not None and selected.images_only
        self.resume_button.setEnabled(selected is not None and not selected.imported and not compact)
        self.review_button.setText("打开图片" if compact else "查看图片")
        self.review_button.setToolTip("此历史任务已精简，内部数据缺失；只能在文件夹中人工选择图片，无法重建审核或恢复数据。" if compact else "")
        self.resume_button.setToolTip("此历史任务已精简，缺少检查点和恢复数据，无法续跑。" if compact else "")

    def selected_record(self) -> TaskRecord | None:
        row = self.task_list.currentRow()
        return self._visible_records[row] if 0 <= row < len(self._visible_records) else None

    def resume_selected(self) -> None:
        record = self.selected_record()
        if record and not record.images_only:
            self.resume_requested.emit(record)

    def review_selected(self) -> None:
        record = self.selected_record()
        if record:
            if record.images_only:
                self.open_selected()
            else:
                self.review_requested.emit(record)

    def open_selected(self) -> None:
        record = self.selected_record()
        if not record:
            return
        self.open_requested.emit(record)
        root = Path(record.root_path)
        source = record.source_output_path
        candidates = [root / "final" / "images"]
        if source is not None:
            if source.name.casefold() == "raw":
                candidates.insert(0, source.parent / "final" / "images")
            candidates.extend((source / "final" / "images", source / "images", source))
        candidates.extend((root / "raw" / "images", root))
        target = next((path for path in candidates if path.is_dir()), root)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def remove_selected(self) -> bool:
        record = self.selected_record()
        if not record:
            return False
        removed = bool(self.store.remove_from_history(record.task_id))
        if removed:
            self.refresh()
        return removed

    def migrate_selected(self) -> None:
        record = self.selected_record()
        if record is not None:
            self.migration_requested.emit(record)

    def set_migration_busy(self, busy: bool) -> None:
        busy = bool(busy)
        self._migration_busy = busy
        self.search_edit.setEnabled(not busy)
        self.task_list.setEnabled(not busy)
        for button in (self.resume_button, self.review_button, self.open_button, self.remove_button):
            if busy:
                button.setEnabled(False)
        self.migrate_button.setEnabled(not busy and self.selected_record() is not None and not self._migration_blocked)
        if not busy:
            self._update_actions()

    def set_migration_blocked(self, blocked: bool) -> None:
        self._migration_blocked = bool(blocked)
        self._update_actions()

    def show_migration_report(self, message: str) -> None:
        self.migration_status.setText(message)


__all__ = ["HistoryPage"]
