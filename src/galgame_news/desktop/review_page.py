"""Three-column media review with bounded asynchronous image previews."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QElapsedTimer, QEvent, QPoint, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QPushButton, QCheckBox, QScrollArea, QSplitter, QTabWidget, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from ..review import ReviewDecision, ReviewSession
from .async_export import AsyncExport
from .async_images import AsyncImages, ImageRequest
from .i18n import code, image_type_label, reason_label, status_label
from .thumbnail_cache import ThumbnailCache
from .review_jobs import ReviewQuery, ReviewStateWriter
from ..diagnostics import diagnose_error, format_error, sanitize_diagnostic
from .ui import page_header
from ..delivery.helpers import section_prefix


class ReviewPage(QWidget):
    decision_changed = Signal(str, str)
    retry_requested = Signal(str)
    supplement_requested = Signal(str, str)
    supplement_cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("reviewPage")
        self.session: ReviewSession | None = None
        self.cache: ThumbnailCache[QPixmap] = ThumbnailCache(128)
        self._items: list[Any] = []
        self._news: dict[str, dict[str, Any]] = {}
        self._row_paths = {}
        self._icon_rows: set[int] = set()
        self._refresh_generation = 0
        self._query_identity = None
        self._render_generation = 0
        self._render_source: list[Any] = []
        self._render_cursor = 0
        self._list_items: dict[str, QListWidgetItem] = {}
        self._render_rows: list[tuple[Any, str, str, Any]] = []
        self._row_candidate_by_id: dict[str, Any] = {}
        self._query_rows_cache = {}
        self._row_display_cache = {}
        self._preview_asset = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_chunk)
        self.query = ReviewQuery(self)
        self.query.ready.connect(self._query_ready)
        self.query.idle.connect(self._update_pending_state)
        self.state_writer = ReviewStateWriter(self)
        self.state_writer.saved.connect(self._state_saved)
        self.state_writer.failed.connect(self._state_failed)
        self.state_writer.idle.connect(self._update_pending_state)
        self._export_waiting_session = None
        self._state_baseline_revision = -1
        self._closing_ui = False
        self._force_closing_ui = False
        self._current_pixmap = QPixmap()
        self._preview_key = ""
        self._retry_context: tuple[int, str] | None = None
        self._supplement_busy = False
        self._supplement_context: tuple[int, str] | None = None
        self.zoom_factor = 1.0
        self._export_requested_count = 0
        self.images = AsyncImages(self)
        self.images.ready.connect(self._image_ready)
        self.exporter = AsyncExport(self)
        self.exporter.finished.connect(self._export_done)
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setSingleShot(True)
        self._thumbnail_timer.setInterval(35)
        self._thumbnail_timer.timeout.connect(self._load_visible)
        self.news_list = QTreeWidget()
        self.news_list.setHeaderHidden(True)
        self.news_list.setObjectName("reviewNewsList")
        self.news_list.setMinimumWidth(170)
        self.news_list.setWordWrap(True)
        self.news_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.news_list.currentItemChanged.connect(self._news_item_changed)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("reviewTabs")
        for label in ("已选", "未候选／待复核", "已排除", "视频"):
            self.tabs.addTab(QWidget(), label)
        self.tabs.setCurrentIndex(1)
        self.tabs.setMaximumHeight(48)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.media_list = QListWidget()
        self.media_list.setObjectName("reviewMediaList")
        self.media_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.media_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.media_list.setMovement(QListWidget.Movement.Static)
        self.media_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.media_list.setIconSize(QSize(148, 100))
        self.media_list.setGridSize(QSize(174, 166))
        self.media_list.setSpacing(6)
        self.media_list.setWordWrap(True)
        self.media_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.media_list.setUniformItemSizes(True)
        self.media_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.media_list.currentRowChanged.connect(self._select_row)
        self.media_list.itemSelectionChanged.connect(self._selection_changed)
        self.media_list.verticalScrollBar().valueChanged.connect(self._schedule_thumbnails)
        self.media_list.viewport().installEventFilter(self)
        self.media_list.installEventFilter(self)
        self.grid_summary = QLabel("尚未加载媒体")
        self.grid_summary.setObjectName("muted")
        self.preview = QLabel("载入抓取结果后，在中间选择媒体")
        self.preview.setObjectName("reviewPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(240, 200)
        self.preview.setWordWrap(True)
        self.preview.setScaledContents(False)
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setObjectName("reviewPreviewScroll")
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setMinimumHeight(225)
        self.preview_scroll.viewport().installEventFilter(self)
        self.preview_scroll.setStyleSheet("QScrollArea#reviewPreviewScroll, QLabel#reviewPreview { background: #272a32; color: #e5e7eb; border-radius: 8px; }")
        self.metadata = QLabel()
        self.metadata.setWordWrap(True)
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.selection_label = QLabel("可按 Ctrl / Shift 多选；A 已选 · R 排除 · P 待复核")
        self.selection_label.setObjectName("muted")
        self.selection_label.setWordWrap(True)
        self.source_button = QPushButton("打开来源")
        self.official_button = QPushButton("打开官网")
        self.original_button = QPushButton("打开原图")
        self.play_button = QPushButton("播放视频")
        self.retry_button = QPushButton("重试本条新闻")
        self.retry_url_edit = QLineEdit()
        self.retry_url_edit.setObjectName("retryOfficialUrlEdit")
        self.retry_url_edit.setPlaceholderText("重试官网地址（选填）")
        self.official_url_edit = self.retry_url_edit
        self.supplement_url_edit = QLineEdit()
        self.supplement_url_edit.setObjectName("supplementUrlEdit")
        self.supplement_url_edit.setPlaceholderText("补充图片网页地址（选填）")
        self.supplement_button = QPushButton("抓取补充图片")
        self.supplement_cancel_button = QPushButton("取消补充")
        self.supplement_cancel_button.setEnabled(False)
        self.supplement_button.clicked.connect(self.request_supplement)
        self.supplement_cancel_button.clicked.connect(self.supplement_cancel_requested.emit)
        self.accept_button = QPushButton("已选  A")
        self.accept_button.setObjectName("primaryButton")
        self.accept_button.setProperty("primary", True)
        self.reject_button = QPushButton("排除  R")
        self.pending_button = QPushButton("待复核  P")
        self.export_pending_checkbox = QCheckBox("同时导出未候选图片")
        self.export_pending_checkbox.setChecked(True)
        self.export_button = QPushButton("导出图片")
        self.export_button.setObjectName("primaryButton")
        self.export_button.setProperty("primary", True)
        self.export_button.setEnabled(False)
        self.open_images_button = QPushButton("打开图片目录")
        self.open_images_button.setEnabled(False)
        self.open_images_button.clicked.connect(self.open_images_directory)
        self.export_status = QLabel()
        self.export_status.setObjectName("muted")
        self.export_status.setWordWrap(True)
        self.operation_status = QLabel()
        self.operation_status.setObjectName("muted")
        self.operation_status.setWordWrap(True)
        self.save_status = QLabel()
        self.save_status.setObjectName("muted")
        self.save_status.setWordWrap(True)
        self.save_retry_button = QPushButton("重试保存")
        self.save_retry_button.setVisible(False)
        self.save_retry_button.clicked.connect(self._retry_save)
        self.raw_toggle = QToolButton()
        self.raw_toggle.setText("技术详情")
        self.raw_toggle.setCheckable(True)
        self.raw_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.raw_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.raw_details = QTextEdit()
        self.raw_details.setReadOnly(True)
        self.raw_details.setVisible(False)
        self.raw_details.setMaximumHeight(155)
        self.raw_toggle.toggled.connect(self._toggle_raw)
        for button, callback in ((self.source_button, self.open_source), (self.official_button, self.open_official),
                                 (self.original_button, self.open_original), (self.play_button, self.play_video),
                                 (self.retry_button, self.retry_selected), (self.accept_button, self.accept_selected),
                                 (self.reject_button, self.reject_selected), (self.pending_button, self.pending_selected),
                                 (self.export_button, self.export_final)):
            button.clicked.connect(callback)
        self.zoom_in_button = QPushButton("放大 +")
        self.zoom_out_button = QPushButton("缩小 −")
        self.zoom_reset_button = QPushButton("适应窗口")
        self.zoom_in_button.clicked.connect(self.zoom_in)
        self.zoom_out_button.clicked.connect(self.zoom_out)
        self.zoom_reset_button.clicked.connect(self.reset_zoom)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("本期新闻"))
        left_layout.addWidget(self.news_list)
        grid = QWidget()
        grid_layout = QVBoxLayout(grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.addWidget(self.tabs)
        grid_layout.addWidget(self.grid_summary)
        grid_layout.addWidget(self.media_list, 1)
        grid_layout.addWidget(self.selection_label)
        # Keep long evidence and expanded raw diagnostics inside the pane's
        # viewport; the page-level export footer stays accessible at 720px.
        right = QScrollArea()
        self.details_scroll = right
        right.setObjectName("reviewDetailsScroll")
        right.setWidgetResizable(True)
        right.setFrameShape(QScrollArea.Shape.NoFrame)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right.setMinimumWidth(340)
        detail_content = QWidget()
        right.setWidget(detail_content)
        details = QVBoxLayout(detail_content)
        details.setContentsMargins(0, 0, 0, 0)
        details.addWidget(QLabel("媒体预览与复核依据"))
        details.addWidget(self.preview_scroll, 1)
        for buttons in ((self.zoom_out_button, self.zoom_in_button, self.zoom_reset_button),
                        (self.accept_button, self.reject_button, self.pending_button),
                        (self.source_button, self.official_button),
                        (self.original_button, self.play_button)):
            actions = QHBoxLayout()
            for button in buttons:
                actions.addWidget(button)
            details.addLayout(actions)
        details.addWidget(self.metadata)
        details.addWidget(self.raw_toggle)
        details.addWidget(self.raw_details)
        retry_row = QHBoxLayout()
        retry_row.addWidget(self.retry_url_edit, 1)
        retry_row.addWidget(self.retry_button)
        details.addLayout(retry_row)
        supplement_row = QHBoxLayout()
        supplement_row.addWidget(self.supplement_url_edit, 1)
        supplement_row.addWidget(self.supplement_button)
        details.addLayout(supplement_row)
        details.addWidget(self.supplement_cancel_button)
        details.addWidget(self.operation_status)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_status, 1)
        save_row.addWidget(self.save_retry_button)
        details.addLayout(save_row)
        for widget in (left, grid, right):
            self.splitter.addWidget(widget)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([195, 430, 460])
        layout = QVBoxLayout(self)
        layout.addWidget(page_header("图片审核", "按新闻复核候选图片与视频，保留适合本期内容的媒体。"))
        layout.addWidget(self.splitter, 1)
        export_row = QHBoxLayout()
        export_row.addWidget(self.export_status, 1)
        export_row.addWidget(self.open_images_button)
        export_row.addWidget(self.export_button)
        export_row.addWidget(self.export_pending_checkbox)
        layout.addLayout(export_row)
        self._shortcuts = []
        for key, callback in (("A", self.accept_selected), ("R", self.reject_selected), ("P", self.pending_selected)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._clear_details()

    def set_session(self, session: ReviewSession | None) -> None:
        switched = session is not self.session
        self._closing_ui = False
        self._force_closing_ui = False
        self._refresh_generation += 1
        self._render_generation += 1
        self.query.invalidate()
        self._query_identity = None
        self._export_waiting_session = None
        if switched:
            self.media_list.blockSignals(True)
            self.media_list.clear()
            self.media_list.blockSignals(False)
            self._items.clear()
            self._list_items.clear()
            self._query_rows_cache.clear()
            self._row_display_cache.clear()
            self._row_paths.clear()
            self._render_source.clear()
            self._render_rows.clear()
            self._render_cursor = 0
        self.session = session
        self._state_baseline_revision = session.revision if session is not None else -1
        self._supplement_busy = False
        self._supplement_context = None
        self.supplement_url_edit.clear()
        self.supplement_url_edit.setEnabled(True)
        self.supplement_cancel_button.setEnabled(False)
        self._retry_context = None
        self.retry_url_edit.clear()
        self.images.invalidate()
        self.cache.clear()
        self.zoom_factor = 1.0
        self.export_status.clear()
        if session is not None:
            self.export_status.setText(
                f"来源目录：{session.output_dir}\n导出目录：{session.task_root / 'final' / 'images'}"
            )
        self.operation_status.clear()
        self.save_retry_button.setVisible(False)
        self.save_status.setText("审核决定会自动保存" if session else "")
        self.export_button.setEnabled(session is not None and self.exporter.session is None)
        self.open_images_button.setEnabled(session is not None)
        self.news_list.blockSignals(True)
        self.news_list.clear()
        self._news = {str(item["news_id"]): item for item in session.news_items} if session else {}
        all_news = QTreeWidgetItem([f"全部新闻（{len(self._news)}）"])
        all_news.setData(0, Qt.ItemDataRole.UserRole, None)
        self.news_list.addTopLevelItem(all_news)
        categories = {prefix: QTreeWidgetItem([label]) for prefix, label in (("x", "新作"), ("h", "汉化"), ("z", "周边"))}
        for prefix, node in categories.items():
            node.setData(0, Qt.ItemDataRole.UserRole, ("section", prefix))
            all_news.addChild(node)
        for news_id, news in self._news.items():
            title = str(news.get("title") or news_id)
            item = QTreeWidgetItem([f"{news.get('sequence', '')}  {title[:90]}" + ("…" if len(title) > 90 else "")])
            item.setToolTip(0, title)
            item.setData(0, Qt.ItemDataRole.UserRole, ("news", news_id))
            categories[section_prefix(str(news.get("section", "")))].addChild(item)
        self.news_list.expandAll()
        self.news_list.setCurrentItem(all_news)
        self.news_list.blockSignals(False)
        if switched:
            self._clear_details()
        self.refresh()

    def _news_filter(self) -> str | None:
        item = self.news_list.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return value[1] if isinstance(value, tuple) and value[0] == "news" else None

    def _selected_section(self) -> str | None:
        item = self.news_list.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return value[1] if isinstance(value, tuple) and value[0] == "section" else None

    def _news_item_changed(self, *_args: Any) -> None:
        context = (id(self.session), str(self._news_filter() or ""))
        if context != self._supplement_context:
            self.supplement_url_edit.clear()
            self._supplement_context = context
            if not self._supplement_busy:
                self.operation_status.clear()
        self.refresh()

    def failed_candidates(self, news_id: str | None = None) -> list[Any]:
        return list(self.session.failed_images(news_id)) if self.session else []

    def _visible_candidates(self) -> list[Any]:
        if self.session is None:
            return []
        index, news_id = self.tabs.currentIndex(), self._news_filter()
        section = self._selected_section()
        if section:
            allowed = {key for key, news in self._news.items() if section_prefix(str(news.get("section", ""))) == section}
            if index == 0:
                candidates = self.session.accepted_images()
                return [c for c in candidates if str(c.news_id) in allowed]
            if index == 2:
                candidates = self.session.rejected_images()
                return [c for c in candidates if str(c.news_id) in allowed]
            if index == 3:
                candidates = [*self.session.accepted_videos(), *self.session.pending_videos()]
                return [c for c in candidates if str(c.news_id) in allowed]
            candidates = self.session.pending_images()
            return [c for c in candidates if str(c.news_id) in allowed]
        if index == 0:
            return list(self.session.accepted_images(news_id))
        if index == 2:
            return list(self.session.rejected_images(news_id))
        if index == 3:
            return [*self.session.accepted_videos(news_id), *self.session.pending_videos(news_id)]
        return list(self.session.pending_images(news_id))

    def _title(self, candidate: Any) -> str:
        return str(getattr(candidate, "title", None) or self._news.get(str(candidate.news_id), {}).get("title") or candidate.news_id)

    def _badges(self, candidate: Any, failed_ids: set[str] | None = None) -> list[str]:
        return self._candidate_badges(candidate, failed_ids if failed_ids is not None else (self.session._failed_ids if self.session else set()))

    @staticmethod
    def _candidate_badges(candidate: Any, failed_ids: set[str]) -> list[str]:
        signals = getattr(candidate, "signals", {})
        reasons = [code(value) for value in getattr(candidate, "review_reasons", [])]
        badges = []
        if code(getattr(candidate, "image_type", "")) == "game_cg":
            badges.append("CG")
        if code(getattr(candidate, "source_type", "")) == "official_x" or "x_source" in reasons or "x.com/" in candidate.source_url or "twitter.com/" in candidate.source_url:
            badges.append("X")
        if any("duplicate" in value for value in reasons) or signals.get("historical_duplicate") or signals.get("duplicate_of") or getattr(getattr(candidate, "score", None), "duplicate_penalty", 0):
            badges.append("重复")
        if code(getattr(candidate, "curation_status", "")) == "invalid" or signals.get("invalid_reason"):
            badges.append("无效")
        if str(candidate.id) in failed_ids or getattr(candidate, "failure_reason", None):
            badges.append("失败")
        return badges

    @staticmethod
    def _localization_summary(candidate: Any) -> str | None:
        provenance = getattr(candidate, "localization_provenance", None)
        signals = getattr(candidate, "signals", {}) or {}

        def field(name: str) -> str:
            value = getattr(provenance, name, None) if provenance is not None else None
            if value in (None, ""):
                value = signals.get(f"localization_{name}", "")
            return str(value or "")

        source = field("source").casefold()
        work_id = field("work_id")
        if not source and not work_id:
            return None
        source_label = {"steam": "Steam", "vndb": "VNDB"}.get(source, source or "未知来源")
        binding_label = {"confirmed": "当前作品", "reference": "关联参考"}.get(field("binding").casefold(), "待确认")
        resolution_label = {"full": "原图", "thumbnail": "缩略图"}.get(field("resolution").casefold(), "尺寸待确认")
        parts = [f"汉化来源：{source_label}"]
        if work_id:
            parts.append(f"作品 ID {work_id}")
        parts.extend((binding_label, resolution_label))
        return " · ".join(parts)

    def refresh(self, preserve_scroll: bool = False, preserve_position: bool = False) -> None:
        preserve_scroll = preserve_scroll or preserve_position
        self._refresh_generation += 1
        refresh_generation = self._refresh_generation
        session = self.session
        news_id = self._news_filter()
        section = self._selected_section()
        tab = self.tabs.currentIndex()
        view = (id(session), news_id, section, tab)
        scroll_value = self.media_list.verticalScrollBar().value() if preserve_scroll else 0
        previous = str(self._selected().id) if self._selected() else ""
        if getattr(self, "_last_query_view", None) != view:
            self.images.invalidate()
            self._preview_key = ""
        self._last_query_view = view
        revision = session.revision if session else -1
        identity = (id(session), revision, refresh_generation, news_id, section, tab)
        self._query_identity = identity
        self.grid_summary.setText("正在整理审核列表…")
        if session is None:
            self._apply_query_result(identity, [], {}, view, previous, preserve_scroll, scroll_value)
            return
        snapshot = session.query_snapshot()
        allowed = ({key for key, news in self._news.items()
                    if section_prefix(str(news.get("section", ""))) == section} if section else None)
        news_by_id = dict(self._news)
        row_cache = dict(self._query_rows_cache)

        def query():
            snapshot.scope_repository_query(news_id, allowed)
            candidates = ReviewPage._query_snapshot_candidates(snapshot, news_id, allowed, tab)
            rows = []
            for candidate in candidates:
                media_id = str(candidate.id)
                stamp = (id(candidate), candidate.local_path, getattr(candidate, "output_sha256", None),
                         snapshot._source_paths.get(media_id), media_id in snapshot._failed_ids)
                cached = row_cache.get(media_id)
                if cached is not None and cached[0] == stamp:
                    rows.append(cached[1])
                    continue
                title = str(getattr(candidate, "title", None) or news_by_id.get(str(candidate.news_id), {}).get("title") or candidate.news_id)
                kind = "视频" if getattr(candidate, "video_url", None) else image_type_label(getattr(candidate, "image_type", None))
                badges = " · ".join(ReviewPage._candidate_badges(candidate, snapshot._failed_ids))
                displayed_kind = "游戏画面" if "CG" in badges.split(" · ") else kind
                tooltip = title + "\n" + "、".join(reason_label(value) for value in getattr(candidate, "review_reasons", []))
                path = snapshot._source_for(candidate)
                row = (candidate, f"{title[:32]}{'…' if len(title) > 32 else ''}\n{displayed_kind}" + (f" · {badges}" if badges else ""), tooltip, path)
                rows.append(row)
                row_cache[media_id] = (stamp, row)
            return rows, dict(snapshot._pending_cache), row_cache

        self.query.request(identity, query)

    @staticmethod
    def _query_snapshot_candidates(snapshot: ReviewSession, news_id: str | None,
                                   allowed: set[str] | None, tab: int) -> list[Any]:
        if tab == 0:
            values = list(snapshot.accepted_images(news_id))
        elif tab == 2:
            values = list(snapshot.rejected_images(news_id))
        elif tab == 3:
            values = [*snapshot.accepted_videos(news_id), *snapshot.pending_videos(news_id)]
        else:
            values = list(snapshot.pending_images(news_id))
        if allowed is not None:
            values = [candidate for candidate in values if str(candidate.news_id) in allowed]
        return values

    def _query_ready(self, identity: Any, result: Any, error: Any) -> None:
        if identity != self._query_identity or self.session is None or identity[0] != id(self.session) or identity[1] != self.session.revision:
            self._update_pending_state()
            return
        if error is not None:
            self.grid_summary.setText("审核列表整理失败")
            self.show_diagnostics([diagnose_error(error, "整理审核列表")])
            self._update_pending_state()
            return
        rows, pending_cache, row_cache = result
        self._query_rows_cache = row_cache
        # The cache is produced from this exact immutable session revision.
        self.session._pending_cache.update(pending_cache)
        view = (identity[0], identity[3], identity[4], identity[5])
        selected = str(self._selected().id) if self._selected() else ""
        self._apply_query_result(identity, rows, pending_cache, view, selected, True,
                                 self.media_list.verticalScrollBar().value())

    def _apply_query_result(self, identity, candidates, _pending_cache, view, previous, preserve_scroll, scroll_value):
        if identity != self._query_identity:
            return
        rows = list(candidates)
        desired_ids = [str(row[0].id) for row in rows]
        current_ids = [str(item.id) for item in self._items]
        if current_ids == desired_ids:
            for row in rows:
                media_id = str(row[0].id)
                if self._row_display_cache.get(media_id) is not row:
                    item = self._list_items.get(media_id)
                    if item is not None:
                        item.setText(row[1])
                        item.setToolTip(row[2])
                    self._row_display_cache[media_id] = row
            self._render_generation += 1
            self._render_rows = rows
            self._render_source = [row[0] for row in rows]
            self._render_cursor = len(rows)
            self._row_candidate_by_id = {str(row[0].id): row[0] for row in rows}
            self._items = [row[0] for row in rows]
            self._row_paths = {index: row[3] for index, row in enumerate(rows) if row[3]}
            if self._items:
                if self.media_list.currentRow() < 0:
                    self.media_list.setCurrentRow(0)
                selected = self._selected()
                asset = (id(selected), self._row_paths.get(self.media_list.currentRow()), getattr(selected, "output_sha256", None))
                if not self._preview_key or self._preview_asset != asset:
                    self._select_row(self.media_list.currentRow())
            else:
                self._clear_details()
            self.grid_summary.setText(f"{self.tabs.tabText(self.tabs.currentIndex())} · {len(self._items)} 项" if self._items else "本分类暂无媒体，切换新闻或分类查看")
            self._schedule_thumbnails()
            self._update_pending_state()
            return
        self._render_generation += 1
        self._render_source = [row[0] for row in rows]
        self._render_rows = rows
        # Common decision edits remove items locally and expose only a few
        # additional variants. Reuse the unchanged prefix without touching
        # thousands of Qt items again.
        append_only = current_ids == desired_ids[:len(current_ids)]
        self._render_cursor = len(current_ids) if append_only else 0
        self._render_previous = previous
        self._render_previous_row = self.media_list.currentRow()
        self._render_selection_ids = {str(item.data(Qt.ItemDataRole.UserRole)) for item in self.media_list.selectedItems()}
        self._render_scroll = scroll_value if preserve_scroll else 0
        self._render_preserve_scroll = preserve_scroll
        desired_id_set = set(desired_ids)
        self.media_list.blockSignals(True)
        for row in range(self.media_list.count() - 1, -1, -1):
            item = self.media_list.item(row)
            media_id = str(item.data(Qt.ItemDataRole.UserRole))
            if media_id not in desired_id_set:
                removed = self.media_list.takeItem(row)
                self._list_items.pop(media_id, None)
                self._icon_rows.discard(row)
        self._row_candidate_by_id = {str(row[0].id): row[0] for row in rows}
        self.media_list.blockSignals(False)
        self._render_view = view
        self._render_chunk()

    def _render_chunk(self) -> None:
        identity = self._query_identity
        if identity is None or identity[0] != (id(self.session) if self.session else None):
            return
        timer = QElapsedTimer()
        timer.start()
        rendered = 0
        self.media_list.blockSignals(True)
        while self._render_cursor < len(self._render_rows) and rendered < 100 and timer.elapsed() < 8:
            target_row = self._render_cursor
            candidate, text, tooltip, path = self._render_rows[target_row]
            media_id = str(candidate.id)
            item = self._list_items.get(media_id)
            if item is None:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, media_id)
                item.setSizeHint(QSize(174, 166))
                item.setSelected(media_id in getattr(self, "_render_selection_ids", set()))
                self._list_items[media_id] = item
                self.media_list.insertItem(target_row, item)
            else:
                current_row = self.media_list.row(item)
                if current_row != target_row:
                    self.media_list.takeItem(current_row)
                    self.media_list.insertItem(target_row, item)
            item.setText(text)
            item.setToolTip(tooltip)
            self._row_display_cache[media_id] = self._render_rows[target_row]
            if path:
                self._row_paths[target_row] = path
            else:
                self._row_paths.pop(target_row, None)
            self._render_cursor += 1
            rendered += 1
        self.media_list.blockSignals(False)
        self._items = [self._row_candidate_by_id[str(self.media_list.item(row).data(Qt.ItemDataRole.UserRole))]
                       for row in range(self.media_list.count())]
        if self._render_cursor < len(self._render_rows):
            self.grid_summary.setText(f"正在加载审核列表… {self._render_cursor}/{len(self._render_rows)} 项")
            generation = self._render_generation
            QTimer.singleShot(0, lambda: self._render_chunk() if generation == self._render_generation else None)
            return
        self.grid_summary.setText(f"{self.tabs.tabText(self.tabs.currentIndex())} · {len(self._items)} 项" if self._items else "本分类暂无媒体，切换新闻或分类查看")
        row = next((i for i, item in enumerate(self._items) if str(item.id) == getattr(self, "_render_previous", "")),
                   min(getattr(self, "_render_previous_row", 0), max(0, len(self._items) - 1)))
        if self._items:
            self.media_list.setCurrentRow(row)
            selected = self._selected()
            if selected is not None and (str(selected.id) != getattr(self, "_render_previous", "") or not self._preview_key):
                self._select_row(row)
        else:
            self._clear_details()
        if getattr(self, "_render_preserve_scroll", False):
            QTimer.singleShot(0, lambda: self.media_list.verticalScrollBar().setValue(min(self._render_scroll, self.media_list.verticalScrollBar().maximum())) if identity == self._query_identity else None)
        self._schedule_thumbnails()
        self._update_pending_state()

    def _selected(self) -> Any | None:
        row = self.media_list.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _clear_details(self) -> None:
        # An asynchronous empty-grid refresh must not erase a completed
        # retry/cancellation summary while its result is still being shown.
        self._bind_retry_context(self._news_filter())
        self._preview_key = ""
        self._current_pixmap = QPixmap()
        self.preview.setPixmap(QPixmap())
        self.preview.setText("本分类暂无媒体" if self.session else "载入抓取结果后，在中间选择媒体")
        self.preview.setMinimumSize(240, 200)
        self.metadata.clear()
        self.raw_details.clear()
        for button in (self.source_button, self.official_button, self.original_button, self.play_button, self.retry_button, self.accept_button, self.reject_button, self.pending_button, self.zoom_in_button, self.zoom_out_button, self.zoom_reset_button):
            button.setEnabled(False)
        self.supplement_button.setEnabled(bool(self.session and self._news_filter()) and not self._supplement_busy and self.exporter.session is not self.session)
        self.retry_button.setEnabled(bool(self.session and self._news_filter()) and not self._supplement_busy and self.exporter.session is not self.session)

    def _select_row(self, row: int) -> None:
        candidate = self._items[row] if 0 <= row < len(self._items) else None
        if candidate is None:
            self._clear_details()
            return
        self._bind_retry_context(str(candidate.news_id))
        self._current_pixmap = QPixmap()
        self._preview_key = ""
        self.zoom_factor = 1.0
        self.preview.setMinimumSize(240, 200)
        self.preview.setPixmap(QPixmap())
        path = self._row_paths.get(row)
        self._preview_asset = (id(candidate), path, getattr(candidate, "output_sha256", None))
        is_video = bool(getattr(candidate, "video_url", None))
        if path and not is_video:
            self.preview.setText("正在加载预览…")
            version = getattr(candidate, "output_sha256", None) or getattr(candidate, "sha256", None) or id(candidate)
            self._preview_key = f"preview:{candidate.id}:{path}:{version}"
            self.images.request_preview(ImageRequest(self._preview_key, path, QSize(2048, 2048), True))
        else:
            self.preview.setText("视频文件 · 点击播放视频" if is_video and path else "预览不可用 · 可打开来源查看")
        score = getattr(getattr(candidate, "score", None), "total", None)
        reasons = [*getattr(candidate, "review_reasons", []), *getattr(candidate, "selection_reasons", [])]
        signals = getattr(candidate, "signals", {})
        if signals.get("invalid_reason"):
            reasons.append("invalid:" + str(signals["invalid_reason"]))
        reason_text = "；".join(dict.fromkeys(reason_label(value) for value in reasons)) or "暂无额外复核原因"
        kind = "视频" if is_video else image_type_label(getattr(candidate, "image_type", None))
        decision = status_label(self.session.decision(str(candidate.id))) if self.session else ""
        localization_summary = self._localization_summary(candidate)
        self.metadata.setText(f"{self._title(candidate)[:180]}\n{kind} · {decision} · {getattr(candidate, 'width', None) or '—'} × {getattr(candidate, 'height', None) or '—'}"
                              + (f" · 评分 {score:.1f}" if score is not None else "")
                              + (f"\n{' · '.join(self._badges(candidate))}" if self._badges(candidate) else "")
                              + (f"\n{localization_summary}" if localization_summary else "")
                              + f"\n复核依据：{reason_text}")
        payload = candidate.model_dump(mode="json") if hasattr(candidate, "model_dump") else {"id": str(candidate.id)}
        payload["resolved_local_path"] = str(path) if path else None
        self.raw_details.setPlainText(json.dumps(sanitize_diagnostic(payload), ensure_ascii=False, indent=2, default=str))
        source = self._source_url(candidate)
        host = QUrl(source).host().casefold()
        self.source_button.setText("打开原帖" if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")) else "打开来源")
        self.source_button.setEnabled(bool(source))
        self.official_button.setEnabled(bool(self._official_url(candidate)))
        self.original_button.setEnabled(bool(getattr(candidate, "image_url", None)))
        self.play_button.setEnabled(bool(path and is_video))
        self.retry_button.setEnabled(bool(candidate.news_id) and self.exporter.session is not self.session)
        self.supplement_button.setEnabled(bool(self._news_filter()) and not self._supplement_busy and self.exporter.session is not self.session)
        self.retry_button.setEnabled(bool(candidate.news_id) and not self._supplement_busy and self.exporter.session is not self.session)
        for button in (self.accept_button, self.reject_button, self.pending_button):
            button.setEnabled(self.exporter.session is not self.session)
        for button in (self.zoom_in_button, self.zoom_out_button, self.zoom_reset_button):
            button.setEnabled(bool(path and not is_video))

    def _selection_changed(self) -> None:
        count = len(self.media_list.selectedIndexes())
        self.selection_label.setText(f"已选择 {count} 项 · Ctrl / Shift 多选；A 已选 · R 排除 · P 待复核")

    def _schedule_thumbnails(self, *_args: Any) -> None:
        self._thumbnail_timer.start()

    def _visible_rows(self) -> list[int]:
        viewport = self.media_list.viewport().rect()
        region = viewport.adjusted(0, -172, 0, 172)
        # Sample the viewport at half-cell intervals, then inspect only nearby
        # model rows. This avoids an O(total imports) scan on every decode.
        sampled = []
        for y in range(0, max(1, viewport.height()), 80):
            for x in range(0, max(1, viewport.width()), 80):
                index = self.media_list.indexAt(QPoint(x, y))
                if index.isValid():
                    sampled.append(index.row())
        columns = max(1, viewport.width() // 174)
        start = max(0, min(sampled) - 2 * columns) if sampled else 0
        stop = min(self.media_list.count(), max(sampled) + 2 * columns + 1) if sampled else min(self.media_list.count(), 48)
        rows = []
        for row in range(start, stop):
            if self.media_list.visualItemRect(self.media_list.item(row)).intersects(region):
                rows.append(row)
                if len(rows) >= self.images.max_pending:
                    break
        return rows

    def _load_visible(self) -> None:
        requests = []
        rows = set(self._visible_rows())
        for row in self._icon_rows - rows:
            item = self.media_list.item(row)
            if item:
                item.setIcon(QIcon())
        self._icon_rows.intersection_update(rows)
        for row in sorted(rows):
            path = self._row_paths.get(row)
            if path is None or getattr(self._items[row], "video_url", None):
                continue
            key = self._thumbnail_key(row)
            cached = self.cache.get(key)
            if cached is not None:
                self.media_list.item(row).setIcon(QIcon(cached))
                self._icon_rows.add(row)
            else:
                requests.append(ImageRequest(key, path, QSize(328, 224)))
        self.images.request_visible(requests)

    def _thumbnail_key(self, row: int) -> str:
        candidate = self._items[row] if 0 <= row < len(self._items) else None
        version = (getattr(candidate, "output_sha256", None) or getattr(candidate, "sha256", None)
                   or id(candidate))
        return f"thumbnail:{self._row_paths.get(row)}:{version}"

    def _image_ready(self, _generation: int, request: ImageRequest, image: QImage, error: str) -> None:
        if request.preview:
            if request.key != self._preview_key:
                return
            if image.isNull():
                self.preview.setText("预览不可用 · 文件无法解码")
                self.raw_details.append(f"\n预览解码错误：{error}")
            else:
                self._current_pixmap = QPixmap.fromImage(image)
                self._render_preview()
            return
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        self.cache.put(request.key, pixmap)
        for row in self._visible_rows():
            if self._thumbnail_key(row) == request.key:
                self.media_list.item(row).setIcon(QIcon(pixmap))
                self._icon_rows.add(row)

    def _render_preview(self) -> None:
        if self._current_pixmap.isNull():
            return
        available = self.preview_scroll.viewport().size() - QSize(16, 16)
        target = QSize(max(1, available.width()), max(1, available.height())) * self.zoom_factor
        pixmap = self._current_pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        self.preview.setMinimumSize(max(240, pixmap.width()), max(200, pixmap.height()))
        self.preview.setPixmap(pixmap)

    def zoom_in(self) -> float:
        self.zoom_factor = min(4.0, round(self.zoom_factor * 1.25, 3))
        self._render_preview()
        return self.zoom_factor

    def zoom_out(self) -> float:
        self.zoom_factor = max(0.25, round(self.zoom_factor / 1.25, 3))
        self._render_preview()
        return self.zoom_factor

    def reset_zoom(self) -> float:
        self.zoom_factor = 1.0
        self._render_preview()
        return self.zoom_factor

    def _set_selected_decision(self, decision: ReviewDecision) -> None:
        if self.session is None or self._closing_ui:
            return
        if self.exporter.session is self.session or self._export_waiting_session is self.session:
            self.operation_status.setText("正在导出，请等待完成后修改审核决定")
            return
        selected = self._selected_items()
        ids = [str(candidate.id) for candidate in selected]
        if not ids:
            return
        self.session.set_decisions(ids, decision, persist=False)
        self.state_writer.enqueue(self.session)
        for media_id in ids:
            self.decision_changed.emit(media_id, decision.value)
        self._patch_decision_items(selected, decision)
        self.refresh(preserve_scroll=True)

    def _patch_decision_items(self, changed: list[Any], decision: ReviewDecision) -> None:
        """Apply the immediate local list effect while the replacement query runs."""
        changed_ids = {str(candidate.id) for candidate in changed}
        scroll_value = self.media_list.verticalScrollBar().value()
        tab = self.tabs.currentIndex()
        moved = set()
        for candidate in changed:
            if str(candidate.id) not in changed_ids:
                continue
            is_video = bool(getattr(candidate, "video_url", None))
            remains = (tab == 0 and decision is ReviewDecision.ACCEPTED) or (tab == 2 and decision is ReviewDecision.REJECTED) or (tab == 1 and decision is ReviewDecision.PENDING) or (tab == 3 and is_video and decision in {ReviewDecision.ACCEPTED, ReviewDecision.PENDING})
            if not remains:
                moved.add(str(candidate.id))
        if not moved:
            return
        old_row = self.media_list.currentRow()
        current_id = str(self._selected().id) if self._selected() else ""
        path_by_id = {str(candidate.id): self._row_paths.get(row)
                      for row, candidate in enumerate(self._items)}
        self.media_list.blockSignals(True)
        removed_rows = [(self.media_list.row(self._list_items[media_id]), media_id)
                        for media_id in moved if media_id in self._list_items]
        for row, media_id in sorted(removed_rows, reverse=True):
            self.media_list.takeItem(row)
            self._list_items.pop(media_id, None)
            self._row_candidate_by_id.pop(media_id, None)
        self._items = [candidate for candidate in self._items if str(candidate.id) not in moved]
        self._row_paths = {row: path_by_id[str(candidate.id)] for row, candidate in enumerate(self._items)
                           if path_by_id.get(str(candidate.id))}
        if self._items:
            row = next((i for i, candidate in enumerate(self._items) if str(candidate.id) == current_id), min(old_row, len(self._items) - 1))
            self.media_list.setCurrentRow(row)
            self.media_list.blockSignals(False)
            if str(self._items[row].id) != current_id:
                self._select_row(row)
        else:
            self.media_list.blockSignals(False)
            self._clear_details()
        bar = self.media_list.verticalScrollBar()
        bar.setValue(min(scroll_value, bar.maximum()))

    @property
    def has_pending_work(self) -> bool:
        return bool(self.state_writer.busy or self.query.busy or self.images.running_count or self.images.pending_count
                    or self._render_cursor < len(self._render_source)
                    or self._export_waiting_session is not None or self.exporter.busy)

    def _update_pending_state(self, *_args) -> None:
        if self._closing_ui:
            return
        if self.session is None:
            self.save_status.clear()
            self.save_retry_button.setVisible(False)
            return
        if self.state_writer.has_failure(self.session):
            self.save_status.setText("审核决定尚未保存，点击重试保存")
            self.save_retry_button.setVisible(True)
        elif self.state_writer.busy or not self._is_state_saved(self.session):
            self.save_status.setText("正在保存审核决定…" if self.state_writer.busy else "审核决定尚未保存")
            self.save_retry_button.setVisible(False)
        else:
            self.save_status.setText("审核决定已保存")
            self.save_retry_button.setVisible(False)

    def _state_saved(self, session, version: int) -> None:
        if self._force_closing_ui:
            return
        if session is self.session and version >= session.revision:
            self._update_pending_state()
        if self._export_waiting_session is session and self._is_state_saved(session):
            self._start_export(session)

    def _is_state_saved(self, session: ReviewSession) -> bool:
        return (not self.state_writer.busy and not self.state_writer.has_failure(session)
                and (self.state_writer.is_saved(session) or session.revision == self._state_baseline_revision))

    def _state_failed(self, session, version: int, error: Any) -> None:
        if self._force_closing_ui:
            return
        if version < session.revision:
            self._update_pending_state()
            return
        if session is self.session:
            self.save_status.setText("审核决定保存失败：" + format_error(error, "review_state_save"))
            self.save_retry_button.setVisible(True)
            self.raw_details.setPlainText(json.dumps(diagnose_error(error, "保存审核决定"), ensure_ascii=False, indent=2))
            self.raw_toggle.setChecked(True)
            if self._export_waiting_session is session:
                self._export_waiting_session = None
                self.operation_status.setText("审核决定保存失败，修复保存后再导出")
                self._set_decision_buttons_enabled(True)

    def _retry_save(self) -> None:
        if self.session is not None:
            self.state_writer.retry(self.session)
            self._update_pending_state()

    def _set_decision_buttons_enabled(self, enabled: bool) -> None:
        for button in (self.accept_button, self.reject_button, self.pending_button):
            button.setEnabled(bool(enabled and not self._closing_ui and self.session is not None
                                   and self.exporter.session is not self.session
                                   and self._export_waiting_session is not self.session))

    def show_diagnostics(self, diagnostics: list[Any]) -> None:
        diagnostics = sanitize_diagnostic(diagnostics)
        rows = []
        for item in diagnostics:
            if isinstance(item, dict):
                rows.append(str(item.get("message") or item.get("code") or "发生错误"))
            else:
                rows.append(format_error(item, "review"))
        if rows:
            self.operation_status.setText("；".join(rows[:5]))
            self.raw_details.setPlainText(json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str))
            self.raw_toggle.setChecked(True)

    def show_retry_summary(self, summary: dict[str, Any]) -> None:
        label = {"cancelled": "重试已取消，已保留完成的图片", "partial": "重试部分完成", "failed": "重试失败，已保留完成的图片"}.get(summary.get("status"), "重试完成")
        self.operation_status.setText(f"{label}：新增 {summary.get('added_count', 0)} 项，成功 {summary.get('success_count', 0)} 项，失败 {summary.get('failed_count', 0)} 项")

    def _selected_items(self) -> list[Any]:
        rows = sorted({index.row() for index in self.media_list.selectedIndexes()})
        if not rows and self._selected() is not None:
            return [self._selected()]
        return [self._items[row] for row in rows if 0 <= row < len(self._items)]

    def accept_selected(self) -> None:
        self._set_selected_decision(ReviewDecision.ACCEPTED)

    def reject_selected(self) -> None:
        self._set_selected_decision(ReviewDecision.REJECTED)

    def pending_selected(self) -> None:
        self._set_selected_decision(ReviewDecision.PENDING)

    def _source_url(self, candidate: Any) -> str:
        return str(getattr(candidate, "news_source_url", None) or getattr(candidate, "parent_source_url", None)
                   or getattr(candidate, "source_url", None) or getattr(candidate, "image_url", None) or "")

    def _official_url(self, candidate: Any) -> str:
        news = self._news.get(str(candidate.news_id), {})
        official = news.get("official_url") or news.get("official_site_url")
        if official:
            return str(official)
        if code(getattr(candidate, "source_type", "")) in {"official_site", "steam"}:
            return str(getattr(candidate, "source_url", ""))
        return ""

    def open_source(self) -> None:
        candidate = self._selected()
        if candidate and self._source_url(candidate):
            QDesktopServices.openUrl(QUrl(self._source_url(candidate)))

    def open_official(self) -> None:
        candidate = self._selected()
        if candidate and self._official_url(candidate):
            QDesktopServices.openUrl(QUrl(self._official_url(candidate)))

    def open_original(self) -> None:
        candidate = self._selected()
        if candidate and getattr(candidate, "image_url", None):
            QDesktopServices.openUrl(QUrl(str(candidate.image_url)))

    def play_video(self) -> None:
        candidate = self._selected()
        path = self.session._source_for(candidate) if self.session and candidate else None
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def retry_selected(self) -> None:
        if self._supplement_busy or self._closing_ui:
            return
        if self.session is not None and (self.exporter.session is self.session or self._export_waiting_session is self.session):
            self.operation_status.setText("正在导出，请等待完成后重试")
            return
        candidate = self._selected()
        news_id = candidate.news_id if candidate else self._news_filter()
        if not news_id:
            return
        self._bind_retry_context(str(news_id))
        if not self.retry_url() and candidate:
            self.retry_url_edit.setText(self._official_url(candidate))
        elif not self.retry_url():
            news = self._news.get(str(news_id), {})
            self.retry_url_edit.setText(str(news.get("official_url") or news.get("official_site_url") or ""))
        url = QUrl(self.retry_url())
        if url.scheme().casefold() not in {"http", "https"} or not url.host():
            self.operation_status.setText("请输入可访问的官网地址（http 或 https），再重试本条新闻")
            self.retry_url_edit.setFocus()
            return
        self.operation_status.setText("正在重试本条新闻…")
        self.retry_requested.emit(str(news_id))

    def _bind_retry_context(self, news_id: str | None) -> None:
        context = (id(self.session), str(news_id or ""))
        if context != self._retry_context:
            self.retry_url_edit.clear()
            self._retry_context = context

    def show_retry_result(self, success: bool) -> None:
        self.operation_status.setText("重试完成，候选已更新" if success else "重试未完成，请检查官网地址与设置后重试")

    def show_retry_failure(self, *args: Any) -> None:
        self.operation_status.setText("重试失败，请展开技术详情查看原因")
        details = [diagnose_error(value, "重试抓图") if isinstance(value, BaseException) else sanitize_diagnostic(value) for value in args]
        self.raw_details.setPlainText(json.dumps(details, ensure_ascii=False, indent=2, default=str))
        self.raw_toggle.setChecked(True)

    def retry_url(self) -> str:
        return self.retry_url_edit.text().strip()

    def request_supplement(self) -> None:
        if self._supplement_busy or self._closing_ui or self._export_waiting_session is self.session or not self.session:
            return
        news_id = self._news_filter()
        if news_id is None:
            return
        url = QUrl(self.supplement_url_edit.text().strip())
        if url.scheme().casefold() not in {"http", "https"} or not url.host():
            self.operation_status.setText("请输入有效的 HTTP 或 HTTPS 网页地址，再抓取补充图片")
            self.supplement_url_edit.setFocus()
            return
        self.operation_status.setText("正在抓取补充图片…")
        self.set_supplement_busy(True)
        self.supplement_requested.emit(str(news_id), url.toString())

    def set_supplement_busy(self, busy: bool) -> None:
        self._supplement_busy = bool(busy)
        self.supplement_url_edit.setEnabled(not self._supplement_busy)
        candidate = self._selected()
        can_act = bool(self.session and self._news_filter() and not self._supplement_busy and self.exporter.session is not self.session)
        self.supplement_button.setEnabled(can_act)
        self.retry_button.setEnabled(bool(self.session and (candidate or self._news_filter())) and not self._supplement_busy and self.exporter.session is not self.session)
        self.export_button.setEnabled(bool(self.session and not self._supplement_busy and self.exporter.session is None))
        self.supplement_cancel_button.setEnabled(self._supplement_busy)

    def show_supplement_result(self, result: Any) -> None:
        if isinstance(result, dict):
            added = int(result.get("added_count", 0) or 0)
            success = int(result.get("success_count", 0) or 0)
            failures = result.get("failures") or []
            failed = max(int(result.get("failed_count", 0) or 0), len(failures))
            status = {"completed": "已完成", "partial": "部分完成", "cancelled": "已取消", "failed": "失败"}.get(
                str(result.get("status") or "").casefold(), str(result.get("status") or "")
            )
            self.operation_status.setText(f"补充图片{status or '完成'}：新增 {added} 项，成功 {success} 项，失败 {failed} 项")
            failures = result.get("failures") or []
            if failures:
                self.raw_details.setPlainText(json.dumps(sanitize_diagnostic(failures), ensure_ascii=False, indent=2, default=str))
                self.raw_toggle.setChecked(True)
        else:
            self.operation_status.setText(f"补充图片抓取完成：{result}")

    def show_supplement_failure(self, error: Any) -> None:
        self.set_supplement_busy(False)
        self.operation_status.setText("补充图片抓取失败，请展开技术详情查看原因")
        self.raw_details.setPlainText(json.dumps(diagnose_error(error, "补充抓图") if isinstance(error, BaseException) else sanitize_diagnostic(error), ensure_ascii=False, indent=2, default=str))
        self.raw_toggle.setChecked(True)

    def export_final(self) -> None:
        if self._supplement_busy or self._closing_ui or self.session is None or self._export_waiting_session is not None:
            return
        session = self.session
        if self.state_writer.has_failure(session):
            self.operation_status.setText("审核决定保存失败，请先重试保存")
            return
        if not self._is_state_saved(session):
            self._export_waiting_session = session
            self.operation_status.setText("正在保存审核决定，保存完成后开始导出…")
            self.export_button.setEnabled(False)
            self._set_decision_buttons_enabled(False)
            if not self.state_writer.busy:
                self.state_writer.enqueue(session)
            return
        self._start_export(session)

    def _start_export(self, session: ReviewSession) -> None:
        include_pending = self.export_pending_checkbox.isChecked()
        if session is not self.session or not self.exporter.start(session, include_pending=include_pending):
            self._export_waiting_session = None
            self._set_decision_buttons_enabled(True)
            return
        self._export_waiting_session = None
        self._export_requested_count = len(self.session.accepted_images()) + len(self.session.accepted_videos())
        self.export_button.setEnabled(False)
        self.export_status.setText("正在导出已选媒体；未候选项目将保留为备选…" if include_pending else "正在导出已选媒体…")
        for button in (self.accept_button, self.reject_button, self.pending_button, self.retry_button):
            button.setEnabled(False)
        self.supplement_button.setEnabled(False)

    def _export_done(self, session: ReviewSession, manifest: dict | None, error: str) -> None:
        if self._force_closing_ui:
            return
        self.export_button.setEnabled(self.session is not None and not self._supplement_busy)
        if session is not self.session:
            return
        if manifest is not None:
            exported = int(manifest.get("image_count", 0)) + int(manifest.get("video_count", 0))
            skipped = max(0, self._export_requested_count - exported)
            pending = int(manifest.get("pending_image_count", 0))
            detail = f"；另有 {skipped} 项已选媒体未导出，请检查本地文件" if skipped else ""
            self.export_status.setText(
                f"已导出 {manifest.get('image_count', 0)} 张图片、{manifest.get('video_count', 0)} 个视频；"
                f"未候选备选 {pending} 张{detail}\n来源目录：{session.output_dir}"
                f"\n导出目录：{session.task_root / 'final' / 'images'}"
            )
            self.raw_details.setPlainText(json.dumps(sanitize_diagnostic(manifest), ensure_ascii=False, indent=2))
        else:
            self.export_status.setText("导出失败，请展开技术详情查看原因")
            self.raw_details.setPlainText(str(sanitize_diagnostic(error)) if error else "导出失败")
            self.raw_toggle.setChecked(True)
        if self._selected():
            for button in (self.accept_button, self.reject_button, self.pending_button, self.retry_button):
                button.setEnabled(True)
        self.set_supplement_busy(self._supplement_busy)

    def _toggle_raw(self, checked: bool) -> None:
        self.raw_details.setVisible(checked)
        self.raw_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def begin_close(self) -> None:
        """Stop new review work while outstanding writes and exports drain."""
        self._closing_ui = True
        self._refresh_generation += 1
        self._render_generation += 1
        self.query.invalidate()
        self._query_identity = None
        self._render_timer.stop()
        self._thumbnail_timer.stop()
        self._render_source.clear()
        self._render_rows.clear()
        self._render_cursor = 0
        self.images.invalidate()
        shutdown = getattr(self.images, "shutdown", None)
        if callable(shutdown):
            shutdown()
        self.supplement_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self._set_decision_buttons_enabled(False)
        self.save_retry_button.setEnabled(self.session is not None and self.state_writer.has_failure(self.session))

    def finish_close(self) -> None:
        """Release idle processing workers after every result has been consumed."""
        if self.has_pending_work:
            return
        self.query.pool.close()
        self.state_writer.pool.close()
        self.images.pool.waitForDone(0)
        self.exporter.pool.waitForDone(0)

    def begin_force_close(self) -> None:
        """Discard queued UI/export requests; running writes may finish off-thread."""
        self._force_closing_ui = True
        self.begin_close()
        self._export_waiting_session = None
        self.setEnabled(False)

    def open_images_directory(self) -> None:
        if self.session is None:
            return
        root = self.session.task_root
        source = self.session.output_dir
        candidates = []
        if source.name.casefold() == "raw":
            candidates.append(source.parent / "final" / "images")
        candidates.extend((source / "images", root / "final" / "images", root / "raw" / "images", source))
        target = next((path for path in candidates if path.is_dir()), root)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.media_list and event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress}:
            callback = {Qt.Key.Key_A: self.accept_selected, Qt.Key.Key_R: self.reject_selected,
                        Qt.Key.Key_P: self.pending_selected}.get(event.key())
            if callback and not event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
                if event.type() == QEvent.Type.KeyPress:
                    callback()
                event.accept()
                return True
        if watched is self.media_list.viewport() and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}:
            self._schedule_thumbnails()
        if watched is self.preview_scroll.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._render_preview)
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        callback = {Qt.Key.Key_A: self.accept_selected, Qt.Key.Key_R: self.reject_selected,
                    Qt.Key.Key_P: self.pending_selected}.get(event.key())
        if callback:
            callback()
            event.accept()
            return
        super().keyPressEvent(event)


__all__ = ["ReviewPage"]
