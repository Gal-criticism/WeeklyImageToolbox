"""Progress page driven only by structured ``ProgressEvent`` objects."""

from __future__ import annotations

from PySide6.QtCore import Signal, Slot, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from .ui import page_header
from .i18n import STAGES, X_MEDIA_COUNTERS, event_summary, status_label, error_summary


class ProgressPage(QWidget):
    stop_requested = Signal()
    cancel_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("progressPage")
        self.counters: dict[str, int] = {"completed": 0, "total": 0, "failed": 0}
        self.x_media_counters = {key: 0 for key in X_MEDIA_COUNTERS}
        self.x_media_labels = {key: QLabel("0") for key in X_MEDIA_COUNTERS}
        self._paused = False
        self.status_label = QLabel("等待开始")
        self.current_news_label = QLabel("尚未开始处理新闻")
        self.current_news_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_news_label.setWordWrap(True)
        self.stage_label = QLabel("等待文档")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.completed_label = QLabel("0")
        self.total_label = QLabel("0")
        self.failed_label = QLabel("0")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("开始任务后，这里显示中文处理摘要")
        self.technical_log = QPlainTextEdit()
        self.technical_log.setReadOnly(True)
        self.technical_log.setMaximumBlockCount(2000)
        self.technical_group = QGroupBox("技术详情（原始诊断）")
        self.technical_group.setCheckable(True)
        self.technical_group.setChecked(False)
        QVBoxLayout(self.technical_group).addWidget(self.technical_log)
        self.technical_log.setVisible(False)
        self.technical_group.toggled.connect(self.technical_log.setVisible)
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.request_pause_or_resume)
        self.stop_button = QPushButton("停止任务")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)

        counters = QGroupBox("新闻进度与异常")
        form = QHBoxLayout(counters)
        for title, label in (("已处理新闻", self.completed_label), ("新闻总数", self.total_label), ("异常事件", self.failed_label)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            label.setObjectName("pageTitle")
            column.addWidget(label)
            form.addLayout(column)
        self.x_media_group = QGroupBox("X 媒体进度")
        x_media_form = QHBoxLayout(self.x_media_group)
        for key, title in X_MEDIA_COUNTERS.items():
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            label = self.x_media_labels[key]
            label.setObjectName(key + "Counter")
            column.addWidget(label)
            x_media_form.addLayout(column)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(14)
        layout.addWidget(page_header("抓取进度", "按新闻保存进度，暂停后在途请求会完成当前工作。"))
        # Keep the task controls fixed below a scrollable body. Expanding raw
        # diagnostics must not increase the minimum height of the main window.
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(14)
        self.content_scroll.setWidget(content)
        current = QGroupBox("当前处理")
        current_form = QFormLayout(current)
        current_form.addRow("任务状态", self.status_label)
        current_form.addRow("当前新闻", self.current_news_label)
        current_form.addRow("处理阶段", self.stage_label)
        body.addWidget(current)
        body.addWidget(self.progress_bar)
        body.addWidget(counters)
        body.addWidget(self.x_media_group)
        body.addWidget(QLabel("处理摘要"))
        self.log.setMinimumHeight(100)
        self.technical_log.setMinimumHeight(100)
        body.addWidget(self.log, 1)
        body.addWidget(self.technical_group)
        layout.addWidget(self.content_scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.pause_button)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)

    def set_running(self, running: bool) -> None:
        self.stop_button.setEnabled(bool(running))
        self.pause_button.setEnabled(bool(running))
        self.progress_bar.setVisible(bool(running) or self.counters["total"] > 0)
        self._paused = False
        if running:
            self.pause_button.setText("暂停")
            self.status_label.setText("正在抓取")
        else:
            self.pause_button.setText("暂停")

    def set_paused(self, paused: bool) -> None:
        """Reflect the cooperative token state without stopping the worker."""

        paused = bool(paused)
        self._paused = paused
        self.pause_button.setText("继续" if paused else "暂停")
        if paused:
            self.status_label.setText("已暂停")
        elif self.pause_button.isEnabled():
            self.status_label.setText("正在抓取")

    @Slot()
    def request_pause_or_resume(self) -> None:
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    @Slot()
    def request_stop(self) -> None:
        self.stop_requested.emit()
        self.cancel_requested.emit()

    @Slot(object)
    def handle_event(self, event: object) -> None:
        kind = str(getattr(event, "kind", "event"))
        payload = getattr(event, "payload", {}) or {}
        if kind == "x_media_stats":
            # These are task snapshots. In particular, pending decreases when
            # recovery succeeds; snapshots must never be added to old values.
            for key, label in self.x_media_labels.items():
                if key not in payload:
                    continue
                try:
                    self.x_media_counters[key] = max(0, int(payload[key]))
                except (TypeError, ValueError, OverflowError):
                    continue
                label.setText(str(self.x_media_counters[key]))
            # Only counters are needed; arbitrary API messages and payloads
            # must not enter either UI log or affect news progress and state.
            return
        message = str(getattr(event, "message", "") or kind)
        if kind in {"news_completed", "news_finished", "news_skipped"}:
            self.counters["completed"] += 1
        if kind.endswith("_failed"):
            self.counters["failed"] += 1
        if kind == "news_started":
            index = getattr(event, "news_index", None)
            self.current_news_label.setText(f"{index or ''} · {message}")
        phase = kind.rpartition("_")[0]
        if phase in STAGES:
            self.stage_label.setText(STAGES[phase])
        total = getattr(event, "total_news", None)
        if total is None:
            total = payload.get("total_news", payload.get("total"))
        if total is not None:
            try:
                self.counters["total"] = max(self.counters["total"], int(total))
            except (TypeError, ValueError):
                pass
        completed = payload.get("completed")
        if completed is not None:
            try:
                self.counters["completed"] = max(self.counters["completed"], int(completed))
            except (TypeError, ValueError):
                pass
        self.completed_label.setText(str(self.counters["completed"]))
        self.total_label.setText(str(self.counters["total"]))
        self.failed_label.setText(str(self.counters["failed"]))
        if self.counters["total"]:
            self.progress_bar.setRange(0, self.counters["total"])
            self.progress_bar.setValue(self.counters["completed"])
        summary = event_summary(kind)
        if kind.endswith("_failed"):
            summary += "；" + error_summary(message)
        if kind in {"news_started", "resolve_progress"}:
            summary += "：" + message
        self.log.appendPlainText(summary)
        self.technical_log.appendPlainText(f"[{kind}] {message}")

    def report_failure(self, message: str, *, count: bool = True) -> None:
        """Show a failure that happened outside the pipeline event stream.

        Runner construction can fail before a worker exists, so there is no
        structured event to deliver through ``handle_event``.  Keep that
        failure visible in the same log and counters used by pipeline events.
        """

        if count:
            self.counters["failed"] += 1
        self.failed_label.setText(str(self.counters["failed"]))
        self.log.appendPlainText(error_summary(message))
        self.technical_log.appendPlainText(str(message))

    def reset(self) -> None:
        self.counters = {"completed": 0, "total": 0, "failed": 0}
        self.completed_label.setText("0")
        self.total_label.setText("0")
        self.failed_label.setText("0")
        for key, label in self.x_media_labels.items():
            self.x_media_counters[key] = 0
            label.setText("0")
        self.log.clear()
        self.technical_log.clear()
        self.status_label.setText("等待开始")
        self.current_news_label.setText("尚未开始处理新闻")
        self.stage_label.setText("等待文档")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.pause_button.setText("暂停")
        self._paused = False

    def set_finished(self, status: str = "Completed") -> None:
        self.set_running(False)
        self.status_label.setText(status_label(status))
        self.progress_bar.setVisible(bool(self.counters["total"]))


__all__ = ["ProgressPage"]
