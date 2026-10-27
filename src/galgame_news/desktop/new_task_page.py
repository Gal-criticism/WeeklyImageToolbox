"""New-task form with DOCX picker and drag/drop support."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, QEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QScrollArea,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from .ui import page_header, Starlight, icon
from .resources import CharacterArt


class NewTaskPage(QWidget):
    start_requested = Signal(object)
    import_requested = Signal(str)

    def __init__(self, default_output: Path | str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("newTaskPage")
        self.setAcceptDrops(True)
        self.default_output = Path(default_output) if default_output else Path.cwd() / "output"

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("将 DOCX 拖到这里，或点击选择文件")
        self.input_edit.setObjectName("inputPathEdit")
        browse = QPushButton("选择文件")
        self.input_browse_button = browse
        browse.setIcon(icon("file"))
        browse.clicked.connect(self.choose_input)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit)
        input_row.addWidget(browse)

        self.issue_edit = QLineEdit()
        self.issue_edit.setPlaceholderText("例如：226（选择文件后自动填写）")
        self.issue_edit.setObjectName("issueEdit")
        self.output_edit = QLineEdit(str(self.default_output))
        output_browse = QPushButton("选择目录")
        output_browse.clicked.connect(self.choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(output_browse)

        self.offline_checkbox = QCheckBox("离线模式：仅使用本地及已索引的来源")
        self.no_videos_checkbox = QCheckBox("跳过视频，仅抓取图片")
        self.no_videos_checkbox.setChecked(True)
        self.socialdata_checkbox = QCheckBox("本次使用 X 图片与视频 API（SocialData）")
        self.socialdata_checkbox.setToolTip("默认关闭；启用后按帖子查询，密钥从凭据库读取。")
        self.socialdata_checkbox.setObjectName("useSocialDataXCheckbox")
        self.archive_materials_checkbox = QCheckBox("往期周报素材检索")
        self.archive_materials_checkbox.setObjectName("useArchiveMaterialsCheckbox")
        self.archive_materials_checkbox.setToolTip("默认关闭；启用后优先匹配更早期号的历史图片和视频。")
        self.archive_range_combo = QComboBox()
        self.archive_range_combo.setObjectName("archiveRangeCombo")
        self.archive_range_combo.addItem("最近一年", "year")
        self.archive_range_combo.addItem("全部周报", "all")
        archive_range_row = QWidget()
        archive_range_layout = QHBoxLayout(archive_range_row)
        archive_range_layout.setContentsMargins(20, 0, 0, 0)
        archive_range_layout.addWidget(QLabel("检索范围"))
        archive_range_layout.addWidget(self.archive_range_combo)
        archive_range_layout.addStretch()
        archive_range_hint = QLabel("返回作品最近出现的两期全部匹配图片与视频。")
        archive_range_hint.setObjectName("muted")
        archive_range_hint.setWordWrap(True)
        self.archive_range_hint = archive_range_hint
        archive_range_container = QWidget()
        archive_range_container.setObjectName("archiveRangeOptions")
        archive_range_container_layout = QVBoxLayout(archive_range_container)
        archive_range_container_layout.setContentsMargins(0, 0, 0, 0)
        archive_range_container_layout.setSpacing(4)
        archive_range_container_layout.addWidget(archive_range_row)
        archive_range_container_layout.addWidget(archive_range_hint)
        archive_range_container.hide()
        self.archive_range_container = archive_range_container
        self.archive_materials_checkbox.toggled.connect(archive_range_container.setVisible)
        self.section_checkboxes = {
            "x": QCheckBox("新作"),
            "h": QCheckBox("汉化"),
            "z": QCheckBox("周边"),
        }
        self._busy = False
        section_row = QHBoxLayout()
        for checkbox in self.section_checkboxes.values():
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self._update_start_enabled)
            section_row.addWidget(checkbox)
        section_row.addStretch()
        self.section_hint = QLabel("请至少选择一个抓取栏目")
        self.section_hint.setObjectName("muted")
        self.section_hint.setWordWrap(True)
        self.section_hint.hide()
        self.start_button = QPushButton("开始抓取")
        self.start_button.setProperty("primary", True)
        self.start_button.setMinimumHeight(46)
        self.start_button.setObjectName("startTaskButton")
        self.start_button.clicked.connect(self.request_start)
        self.import_button = QPushButton("导入历史结果")
        self.import_button.clicked.connect(self.choose_import)
        self.drop_hint = QLabel("拖入一份周报，开始整理本期配图")
        self.drop_hint.setWordWrap(True)
        self.drop_hint.setObjectName("muted")

        drop_group = QGroupBox("周报文档")
        drop_layout = QHBoxLayout(drop_group)
        drop_form = QVBoxLayout()
        drop_form.addWidget(Starlight())
        drop_form.addWidget(self.drop_hint)
        drop_form.addLayout(input_row)
        drop_layout.addLayout(drop_form, 1)
        drop_layout.addWidget(CharacterArt(200, 154))
        destination = QGroupBox("任务信息")
        form = QFormLayout(destination)
        form.setVerticalSpacing(14)
        form.addRow("本期期号", self.issue_edit)
        form.addRow("输出目录", output_row)
        options = QGroupBox("抓取选项")
        options_layout = QVBoxLayout(options)
        options_layout.addWidget(QLabel("抓取栏目"))
        options_layout.addLayout(section_row)
        options_layout.addWidget(self.section_hint)
        options_layout.addWidget(self.no_videos_checkbox)
        options_layout.addWidget(self.offline_checkbox)
        options_layout.addWidget(self.socialdata_checkbox)
        options_layout.addWidget(self.archive_materials_checkbox)
        options_layout.addWidget(archive_range_container)
        notice = QLabel("任务完成后保留完整输出：raw、索引、检查点、缓存、审核状态与 final 图片交付，便于审核和恢复。")
        notice.setObjectName("muted")
        notice.setWordWrap(True)
        options_layout.addWidget(notice)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(14)
        layout.addWidget(page_header("新建任务", "导入周报，采集官网与 X 配图，再按新闻逐条审核。"))
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(14)
        content_layout.addWidget(drop_group)
        content_layout.addWidget(destination)
        content_layout.addWidget(options)
        content_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.import_button)
        actions.addStretch()
        actions.addWidget(self.start_button)
        layout.addLayout(actions)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange) and hasattr(self, "input_browse_button"):
            self.input_browse_button.setIcon(icon("file"))

    @staticmethod
    def infer_issue(path: Path | str) -> str:
        return Path(path).stem.strip() or "issue"

    def set_input_path(self, path: Path | str) -> None:
        selected = Path(path)
        self.input_edit.setText(str(selected))
        self.drop_hint.setText("已选择：" + selected.name)
        if not self.issue_edit.text().strip():
            self.issue_edit.setText(self.infer_issue(selected))

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._update_start_enabled()
        self.import_button.setEnabled(not busy)

    def _update_start_enabled(self, *_args) -> None:
        has_sections = any(box.isChecked() for box in self.section_checkboxes.values())
        self.section_hint.setVisible(not has_sections)
        self.start_button.setEnabled(
            not self._busy and has_sections
        )

    def request_start(self) -> None:
        selected_sections = [
            key for key, checkbox in self.section_checkboxes.items() if checkbox.isChecked()
        ]
        if not selected_sections:
            return
        payload = {
            "input_path": Path(self.input_edit.text().strip()),
            "issue_id": self.issue_edit.text().strip(),
            "output_dir": Path(self.output_edit.text().strip() or self.default_output),
            "offline": self.offline_checkbox.isChecked(),
            "no_videos": self.no_videos_checkbox.isChecked(),
            "use_socialdata_x": self.socialdata_checkbox.isChecked(),
            "use_archive_materials": self.archive_materials_checkbox.isChecked(),
            "archive_range": self.archive_range_combo.currentData(),
            "selected_sections": selected_sections,
        }
        if not payload["input_path"].name or not payload["issue_id"]:
            self.drop_hint.setText("请选择 DOCX 文档并填写期号")
            return
        self.start_requested.emit(payload)

    def request_import(self, path: Path | str) -> None:
        self.import_requested.emit(str(path))

    def choose_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择周报文档", "", "Word 文档 (*.docx)")
        if path:
            self.set_input_path(path)

    def choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def choose_import(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择历史结果目录", self.output_edit.text())
        if path:
            self.request_import(path)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if any(url.toLocalFile().casefold().endswith(".docx") for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.casefold().endswith(".docx"):
                self.set_input_path(path)
                event.acceptProposedAction()
                return
        event.ignore()


__all__ = ["NewTaskPage"]
