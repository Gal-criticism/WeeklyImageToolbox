from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest
from PySide6.QtCore import QSize, QThread, Qt
from PySide6.QtWidgets import QAbstractItemView

from galgame_news.desktop.async_images import AsyncImages, ImageRequest, _Decode
from galgame_news.desktop.review_page import ReviewPage
from galgame_news.domain import ReviewReason, SourceType
from galgame_news.desktop.i18n import reason_label
from galgame_news.review import ReviewDecision, ReviewSession


def session_at(root: Path, count: int = 3) -> ReviewSession:
    root.mkdir(parents=True)
    candidates = []
    for row in range(count):
        Image.new("RGB", (420, 320), "red" if row == 0 else "blue").save(root / f"{row}.jpg")
        candidates.append({"id": f"image-{row}", "news_id": "news-1" if row % 2 == 0 else "news-2",
                           "image_url": f"https://example.test/{row}.jpg", "source_url": "https://official.test/page",
                           "source_type": "official_site", "local_path": f"{row}.jpg",
                           "width": 1024, "height": 768, "image_type": "game_cg", "score": {"total": 80 - row % 80}})
    (root / "image_index.json").write_text(json.dumps({
        "news_items": [{"news_id": "news-1", "title": "第一条新闻"}, {"news_id": "news-2", "title": "第二条新闻"}],
        "candidates": candidates}), encoding="utf-8")
    session = ReviewSession.from_output(root, state_path=root.parent / f"{root.name}-state.json")
    for sequence, item in enumerate(session.news_items, start=1):
        item["sequence"] = sequence
    return session


def select_news(page: ReviewPage, news_id: str) -> None:
    root = page.news_list.topLevelItem(0)
    for section_index in range(root.childCount()):
        section = root.child(section_index)
        for index in range(section.childCount()):
            child = section.child(index)
            if child.data(0, Qt.ItemDataRole.UserRole) == ("news", news_id):
                page.news_list.setCurrentItem(child)
                return
    raise AssertionError(f"missing news item {news_id}")


def wait_ready(qtbot, page: ReviewPage) -> None:
    qtbot.waitUntil(lambda: (
        not page.query.busy
        and page._render_cursor >= len(page._render_source)
        and page.query.pool.activeThreadCount() == 0
        and page.state_writer.pool.activeThreadCount() == 0
        and not page.query._retired
        and not page.state_writer._retired
        and (page._query_identity is None or (
            page.session is not None
            and page._query_identity[0] == id(page.session)
            and page._query_identity[1] == page.session.revision
        ))
    ), timeout=10000)


def test_chinese_grid_and_news_filter_keep_tab_mapping(qtbot, tmp_path):
    session = session_at(tmp_path / "output")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    assert page.splitter.count() == 3
    assert [page.tabs.tabText(index) for index in range(5)] == ["已选", "未候选／待复核", "往期周报", "已排除", "视频"]
    assert page.media_list.viewMode() is page.media_list.ViewMode.IconMode
    assert page.media_list.selectionMode() is QAbstractItemView.SelectionMode.ExtendedSelection
    assert page.media_list.count() == 3
    select_news(page, "news-2")
    wait_ready(qtbot, page)
    assert [candidate.news_id for candidate in page._items] == ["news-2"]
    page.news_list.setCurrentItem(page.news_list.topLevelItem(0))
    wait_ready(qtbot, page)
    assert page.media_list.count() == 3


def test_news_tree_section_filter_and_sequence_preservation(qtbot, tmp_path):
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session_at(tmp_path / "tree"))
    wait_ready(qtbot, page)
    root = page.news_list.topLevelItem(0)
    assert root.text(0).startswith("全部新闻")
    assert [root.child(i).text(0) for i in range(3)] == ["新作", "汉化", "周边"]
    assert root.child(2).child(0).text(0).startswith("1  ")
    assert root.child(2).child(1).text(0).startswith("2  ")
    second = root.child(2).takeChild(1)
    root.child(1).addChild(second)
    page._news["news-2"]["section"] = "汉化"
    page.news_list.setCurrentItem(root.child(1))
    wait_ready(qtbot, page)
    assert {item.news_id for item in page._items} == {"news-2"}
    assert {str(item.id) for item in page._items} == {str(item.id) for item in page.session.pending_images() if item.news_id == "news-2"}
    page.tabs.setCurrentIndex(3)
    wait_ready(qtbot, page)
    assert page.media_list.count() == 0


def test_grid_focus_shortcuts_apply_to_multiple_selected_items(qtbot, tmp_path):
    session = session_at(tmp_path / "output")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.resize(1100, 720)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.show()
    page.activateWindow()
    page.media_list.setFocus()
    page.media_list.item(0).setSelected(True)
    page.media_list.item(1).setSelected(True)
    selected = [str(candidate.id) for candidate in page._selected_items()]
    assert len(selected) == 2
    qtbot.keyClick(page.media_list, Qt.Key.Key_A)
    qtbot.waitUntil(lambda: all(session.decision(key) is ReviewDecision.ACCEPTED for key in selected))
    page.tabs.setCurrentIndex(0)
    wait_ready(qtbot, page)
    page.media_list.selectAll()
    qtbot.keyClick(page.media_list, Qt.Key.Key_P)
    qtbot.waitUntil(lambda: all(session.decision(key) is ReviewDecision.PENDING for key in selected))
    page.tabs.setCurrentIndex(1)
    wait_ready(qtbot, page)
    page.media_list.selectAll()
    qtbot.keyClick(page.media_list, Qt.Key.Key_R)
    qtbot.waitUntil(lambda: all(session.decision(str(candidate.id)) is ReviewDecision.REJECTED for candidate in session.images))


def test_decode_in_worker_and_pixmap_delivery_in_gui(qtbot, qapp, tmp_path, monkeypatch):
    session = session_at(tmp_path / "output", 1)
    seen = []
    original = _Decode.run

    def record(job):
        seen.append(QThread.currentThread() is not qapp.thread())
        original(job)

    monkeypatch.setattr(_Decode, "run", record)
    page = ReviewPage()
    qtbot.addWidget(page)
    deliveries = []
    page.images.ready.connect(lambda *_: deliveries.append(QThread.currentThread() is qapp.thread()))
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    page.show()
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    assert seen and all(seen)
    assert deliveries and all(deliveries)
    assert page._current_pixmap.width() <= 2048
    assert page._current_pixmap.size() == QSize(420, 320)
    assert page.images.pool.maxThreadCount() == 2
    assert page.cache.max_items == 128


def test_generation_discards_queued_old_result(qtbot, tmp_path):
    path = tmp_path / "one.png"
    Image.new("RGB", (100, 100), "red").save(path)
    loader = AsyncImages(max_pending=4)
    delivered = []
    loader.ready.connect(lambda _generation, request, *_: delivered.append(request.key))
    loader.request_visible([ImageRequest("old", path, QSize(50, 50))])
    loader.invalidate()
    loader.request_visible([ImageRequest("new", path, QSize(50, 50))])
    qtbot.waitUntil(lambda: loader.running_count == 0 and loader.pending_count == 0)
    assert delivered == ["new"]


def test_decode_queue_is_bounded_and_replaced_when_scrolling(qtbot, tmp_path):
    path = tmp_path / "one.png"
    Image.new("RGB", (100, 100), "red").save(path)
    loader = AsyncImages(max_pending=4)
    loader.request_visible([ImageRequest(str(index), path, QSize(50, 50)) for index in range(100)])
    assert loader.running_count <= 2
    assert loader.pending_count <= 4
    loader.request_visible([ImageRequest("last", path, QSize(50, 50))])
    assert list(loader._pending) == ["last"]
    qtbot.waitUntil(lambda: loader.running_count == 0 and loader.pending_count == 0)


def test_new_session_never_receives_old_preview_or_cache(qtbot, tmp_path):
    first = session_at(tmp_path / "first", 1)
    second = session_at(tmp_path / "second", 2)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(first)
    page.set_session(second)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    color = page._current_pixmap.toImage().pixelColor(20, 20)
    assert color.blue() > color.red()
    assert all(str(first.output_dir) not in key for key in page.cache.keys)


def test_visible_range_limits_work_and_offscreen_icons(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 160)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.resize(1280, 720)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.show()
    page._load_visible()
    assert len(page._visible_rows()) < 48
    assert page.images.pending_count <= 48
    assert page.images.running_count <= 2
    qtbot.waitUntil(lambda: page.images.running_count == 0 and page.images.pending_count == 0)
    assert len(page.cache) < 160
    first_icons = set(page._icon_rows)
    page.media_list.scrollToBottom()
    page._load_visible()
    assert all(page.media_list.item(row).icon().isNull() for row in first_icons - set(page._visible_rows()))
    qtbot.waitUntil(lambda: page.images.running_count == 0 and page.images.pending_count == 0)
    assert len(page.cache) <= 128


def test_corrupt_preview_is_cleared_and_reports_diagnostic(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 2)
    (session.output_dir / "1.jpg").write_bytes(b"not an image")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    page.media_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: "无法解码" in page.preview.text())
    assert page._current_pixmap.isNull()
    assert "预览解码错误" in page.raw_details.toPlainText()
    page.set_session(None)
    assert not page.accept_button.isEnabled()
    assert not page.export_button.isEnabled()
    assert "载入" in page.preview.text()


def test_source_buttons_open_evidence_urls_and_import_path(qtbot, tmp_path, monkeypatch):
    session = session_at(tmp_path / "output", 1)
    candidate = session.images[0]
    object.__setattr__(candidate, "news_source_url", "https://x.com/author/status/123")
    seen = []
    monkeypatch.setattr("galgame_news.desktop.review_page.QDesktopServices.openUrl", lambda url: seen.append(url.toString()))
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    assert page.source_button.text() == "打开原帖"
    page.open_source()
    page.open_official()
    page.open_original()
    assert seen == [candidate.news_source_url, candidate.source_url, candidate.image_url]
    assert json.loads(page.raw_details.toPlainText())["resolved_local_path"] == str(session._source_for(candidate))


def test_retry_url_validation_autofill_and_result_message(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 1)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    seen = []
    page.retry_requested.connect(seen.append)
    page.retry_selected()
    assert seen == ["news-1"]
    assert page.retry_url() == "https://official.test/page"
    page.retry_url_edit.setText("file:///tmp/file")
    page.retry_selected()
    assert len(seen) == 1
    assert "官网地址" in page.operation_status.text()
    page.show_retry_result(False)
    assert "未完成" in page.operation_status.text()
    page.show_retry_failure("network traceback")
    assert page.raw_toggle.isChecked()
    assert "network traceback" in page.raw_details.toPlainText()


def test_supplement_url_validation_busy_and_result(qtbot, tmp_path):
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session_at(tmp_path / "supplement", 1))
    wait_ready(qtbot, page)
    assert not page.supplement_button.isEnabled()  # all-news is not a concrete item
    select_news(page, "news-1")
    wait_ready(qtbot, page)
    assert page.supplement_button.isEnabled()
    requested = []
    canceled = []
    page.supplement_requested.connect(lambda *args: requested.append(args))
    page.supplement_cancel_requested.connect(lambda: canceled.append(True))
    page.supplement_url_edit.setText("file:///tmp/not-http")
    page.request_supplement()
    assert requested == []
    assert "HTTP 或 HTTPS" in page.operation_status.text()
    page.supplement_url_edit.setText("https://example.test/gallery")
    page.request_supplement()
    assert requested == [("news-1", "https://example.test/gallery")]
    assert page._supplement_busy and not page.supplement_button.isEnabled()
    assert not page.supplement_url_edit.isEnabled()
    page.supplement_cancel_button.click()
    assert canceled == [True]
    page.show_supplement_result({"added_count": 2, "success_count": 2, "failed_count": 0,
                                 "status": "partial", "failures": [{"source": "source-a", "error": "fetch failed"}]})
    page.set_supplement_busy(False)
    assert not page._supplement_busy and page.supplement_url_edit.isEnabled()
    assert "部分完成" in page.operation_status.text() and "失败 1 项" in page.operation_status.text()
    assert "source-a" in page.raw_details.toPlainText()
    select_news(page, "news-2")
    wait_ready(qtbot, page)
    assert page.supplement_url_edit.text() == ""
    page.show_supplement_failure("network error")
    assert "network error" in page.raw_details.toPlainText()


def test_retry_url_belongs_to_selected_news_and_clears_on_session_change(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 2)
    object.__setattr__(session.images[0], "source_url", "https://official.test/news-a")
    object.__setattr__(session.images[1], "source_url", "https://official.test/news-b")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    seen = []
    page.retry_requested.connect(lambda news_id: seen.append((news_id, page.retry_url())))
    # The default mixed all-news view binds each URL to its current candidate.
    page.retry_selected()
    assert seen[-1] == ("news-1", "https://official.test/news-a")
    page.retry_url_edit.setText("https://manual.test/news-a")
    page.refresh()
    wait_ready(qtbot, page)
    assert page.retry_url() == "https://manual.test/news-a"
    page.media_list.setCurrentRow(1)
    wait_ready(qtbot, page)
    assert page.retry_url() == ""
    page.retry_selected()
    assert seen[-1] == ("news-2", "https://official.test/news-b")
    select_news(page, "news-1")
    wait_ready(qtbot, page)
    assert page.retry_url() == ""
    page.retry_selected()
    assert seen[-1] == ("news-1", "https://official.test/news-a")
    page.set_session(session_at(tmp_path / "other", 1))
    wait_ready(qtbot, page)
    assert page.retry_url() == ""


def test_empty_news_can_retry_with_official_url_and_editor_keeps_typing(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 1)
    session.news_items[1]["official_url"] = "https://official.test/second"
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    select_news(page, "news-2")
    wait_ready(qtbot, page)
    assert page.media_list.count() == 0
    assert page.retry_button.isEnabled()
    assert page.supplement_button.isEnabled()
    page.supplement_url_edit.setText("https://example.test/second")
    supplement_requests = []
    page.supplement_requested.connect(lambda *args: supplement_requests.append(args))
    page.request_supplement()
    assert supplement_requests == [("news-2", "https://example.test/second")]
    page.set_supplement_busy(False)
    seen = []
    page.retry_requested.connect(seen.append)
    page.retry_selected()
    assert seen == ["news-2"]
    assert page.retry_url() == "https://official.test/second"
    page.show()
    page.retry_url_edit.setFocus()
    page.retry_url_edit.clear()
    qtbot.keyClicks(page.retry_url_edit, "https://example.test/paper")
    assert page.retry_url() == "https://example.test/paper"
    assert session.decision("image-0") is ReviewDecision.PENDING


def test_export_calls_existing_session_method_in_worker(qtbot, qapp, tmp_path, monkeypatch):
    session = session_at(tmp_path / "output", 1)
    session.set_decision("image-0", ReviewDecision.ACCEPTED)
    original = session.export_final
    worker_thread = []

    def export(**kwargs):
        worker_thread.append(QThread.currentThread() is not qapp.thread())
        return original(**kwargs)

    monkeypatch.setattr(session, "export_final", export)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.export_final()
    assert not page.export_button.isEnabled()
    qtbot.waitUntil(lambda: page.exporter.session is None and page._export_waiting_session is None and "已导出" in page.export_status.text())
    assert worker_thread == [True]
    assert "已导出 1 张图片" in page.export_status.text()
    assert (session.task_root / "final" / "final_manifest.json").is_file()
    assert "image_count" in page.raw_details.toPlainText()


def test_export_error_has_chinese_outcome_and_raw_error(qtbot, tmp_path, monkeypatch):
    session = session_at(tmp_path / "output", 1)

    def fail(**kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(session, "export_final", fail)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.export_final()
    qtbot.waitUntil(lambda: page.exporter.session is None and page._export_waiting_session is None and "导出失败" in page.export_status.text())
    assert "导出失败" in page.export_status.text()
    assert page.raw_toggle.isChecked()
    assert "disk error" in page.raw_details.toPlainText()
    assert "OSError" in page.raw_details.toPlainText()


def test_export_result_from_previous_session_is_not_shown(qtbot, tmp_path):
    first = session_at(tmp_path / "first", 1)
    second = session_at(tmp_path / "second", 1)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(first)
    page.export_final()
    page.set_session(second)
    wait_ready(qtbot, page)
    qtbot.waitUntil(lambda: page.exporter.session is None and page._export_waiting_session is None)
    assert str(second.output_dir) in page.export_status.text()
    assert str(first.output_dir) not in page.export_status.text()
    assert page.export_button.isEnabled()
    assert str(first.output_dir) not in page.raw_details.toPlainText()


def test_long_title_and_all_risk_badges_do_not_show_raw_codes(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 1)
    session.news_items[0]["title"] = "很长的新闻标题" * 100
    candidate = session.images[0]
    object.__setattr__(candidate, "source_type", SourceType.OFFICIAL_X)
    candidate.signals.update({"duplicate_of": "other", "invalid_reason": "test_invalid"})
    object.__setattr__(candidate, "review_reasons", [ReviewReason.X_SOURCE, ReviewReason.UNCERTAIN_MATCH])
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    assert "CG" in page.media_list.item(0).text()
    assert all(label in page.media_list.item(0).text() for label in ("X", "重复", "无效"))
    assert "x_source" not in page.metadata.text()
    assert "uncertain_match" not in page.metadata.text()
    assert len(page.media_list.item(0).text()) < 100
    assert "uncertain_match" in page.raw_details.toPlainText()
    assert page.source_button.text() == "打开来源"


def test_localization_provenance_uses_chinese_labels(qtbot, tmp_path):
    session = session_at(tmp_path / "output", 1)
    session.images[0].signals.update({
        "localization_source": "steam", "localization_work_id": "3419820",
        "localization_binding": "reference", "localization_resolution": "thumbnail",
    })
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    assert "汉化来源：Steam" in page.metadata.text()
    assert "作品 ID 3419820" in page.metadata.text()
    assert "关联参考" in page.metadata.text()
    assert "缩略图" in page.metadata.text()
    assert reason_label("localization_manual_review_required") == "保留此图供人工复核，不自动入选"
    assert reason_label("localization_binding_conflict") == "作品绑定存在冲突"
    assert reason_label("localization_full_native_screenshot") == "已确认当前作品的原图游戏截图"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_expanded_review_details_keep_export_accessible_at_minimum_window(qtbot, tmp_path, theme):
    from galgame_news.desktop.controller import DesktopController
    from galgame_news.desktop.theme import apply_theme
    from galgame_news.desktop.window import MainWindow

    controller = DesktopController(app_data=tmp_path / "app")
    window = MainWindow(controller)
    qtbot.addWidget(window)
    controller.review_page.set_session(session_at(tmp_path / "output", 1))
    wait_ready(qtbot, controller.review_page)
    window.navigation.setCurrentRow(2)
    apply_theme(theme)
    window.resize(1100, 720)
    page = controller.review_page
    page.metadata.setText("较长的新闻标题与具体复核依据。" * 160)
    page.raw_toggle.setChecked(True)
    window.show()
    page.media_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    assert window.size() == QSize(1100, 720)
    assert window.minimumSizeHint().height() <= 720
    assert page.export_button.isVisible()
    export_bottom = page.export_button.mapTo(window, page.export_button.rect().bottomRight()).y()
    assert export_bottom < 720
    viewport = page.details_scroll.viewport()
    for button in (page.zoom_in_button, page.zoom_out_button, page.zoom_reset_button,
                   page.accept_button, page.reject_button, page.pending_button,
                   page.source_button, page.official_button, page.original_button,
                   page.play_button, page.retry_button, page.raw_toggle):
        left = button.mapTo(viewport, button.rect().topLeft()).x()
        assert left >= 0, (theme, button.text(), left)
        assert left + button.width() <= viewport.width(), (theme, button.text(), left, button.width(), viewport.width())
    scrollbar = page.details_scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum())
    retry_bottom = page.retry_button.mapTo(page.details_scroll.viewport(), page.retry_button.rect().bottomRight()).y()
    assert retry_bottom <= page.details_scroll.viewport().height()
