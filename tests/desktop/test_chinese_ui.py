from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFileDialog

from galgame_news.desktop.history_page import HistoryPage
from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.new_task_page import NewTaskPage
from galgame_news.desktop.progress_page import ProgressPage
from galgame_news.desktop.settings_page import SettingsPage
from galgame_news.desktop.window import MainWindow
from galgame_news.desktop.theme import apply_theme, _system_changed
from galgame_news.pipeline import ProgressEvent
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend, SettingsStore
from galgame_news.tasks import TaskStore


def test_pause_dispatch_uses_state_not_caption(qtbot):
    page = ProgressPage()
    qtbot.addWidget(page)
    seen = []
    page.pause_requested.connect(lambda: seen.append("pause"))
    page.resume_requested.connect(lambda: seen.append("resume"))
    page.set_running(True)
    page.pause_button.setText("任意文案")
    page.request_pause_or_resume()
    page.set_paused(True)
    page.pause_button.setText("任意文案")
    page.request_pause_or_resume()
    assert seen == ["pause", "resume"]
    page.set_finished("Cancelled")
    assert page.status_label.text() == "已停止"


def test_progress_summaries_and_raw_diagnostics(qtbot):
    page = ProgressPage()
    qtbot.addWidget(page)
    for kind, message in (("news_started", "作品の原文标题"), ("collect_failed", "ConnectionResetError"),
                          ("news_completed", "news completed"), ("news_skipped", "restored from checkpoint")):
        page.handle_event(ProgressEvent(kind, "t", "226", news_index=1, total_news=3, message=message))
    assert page.current_news_label.text() == "1 · 作品の原文标题"
    assert page.failed_label.text() == "1"
    assert page.completed_label.text() == "2"
    assert page.progress_bar.maximum() == 3
    assert "ConnectionResetError" not in page.log.toPlainText()
    assert "ConnectionResetError" in page.technical_log.toPlainText()
    assert page.technical_group.isChecked() is False
    page.reset()
    assert page.technical_log.toPlainText() == ""


def test_theme_codes_and_saved_keys_stay_masked(qtbot, tmp_path):
    credentials = CredentialStore(InMemoryCredentialBackend())
    credentials.set("socialdata_api_key", "existing-private-value")
    store = SettingsStore(app_data=tmp_path)
    page = SettingsPage(store, credentials)
    qtbot.addWidget(page)
    assert page.socialdata_edit.text() == ""
    assert page.credential_status["socialdata_api_key"].text() == "已配置"
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
    page.theme_combo.setItemText(page.theme_combo.currentIndex(), "任意中文标题")
    page.socialdata_edit.setText("new-private-value")
    page.save()
    assert store.load().theme == "dark"
    assert credentials.get("socialdata_api_key") == "new-private-value"
    assert page.socialdata_edit.text() == ""
    assert "private-value" not in store.path.read_text(encoding="utf-8")


def test_palette_switch_and_follow_system(qapp):
    apply_theme("light", qapp)
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() > 128
    apply_theme("dark", qapp)
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() < 128
    apply_theme("system", qapp)
    _system_changed(qapp, Qt.ColorScheme.Dark)
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() < 128
    _system_changed(qapp, Qt.ColorScheme.Light)
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() > 128
    apply_theme("light", qapp)


def test_chinese_file_picker_preserves_payload_and_import(qtbot, monkeypatch, tmp_path):
    page = NewTaskPage(default_output=tmp_path)
    qtbot.addWidget(page)
    path = tmp_path / "226_副本.docx"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    page.choose_input()
    assert page.issue_edit.text() == "226_副本"
    values = []
    page.start_requested.connect(values.append)
    page.request_start()
    assert values[0]["input_path"] == path
    assert values[0]["use_socialdata_x"] is False
    seen = []
    page.import_requested.connect(seen.append)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    page.choose_import()
    assert seen == [str(tmp_path)]


def test_history_search_keeps_record_identity_and_files(qtbot, tmp_path):
    store = TaskStore(tmp_path / "catalog")
    first = store.create_task("226", tmp_path / "226.docx", task_root=tmp_path / "first")
    second = store.create_task("261", tmp_path / "261.docx", task_root=tmp_path / "second")
    page = HistoryPage(store)
    qtbot.addWidget(page)
    page.search_edit.setText("226")
    assert page.task_list.count() == 1
    page.task_list.setCurrentRow(0)
    assert page.selected_record().task_id == first.task_id
    assert page.remove_selected()
    assert first.root_path.exists()
    page.search_edit.clear()
    assert page.task_list.count() == 1
    page.task_list.setCurrentRow(0)
    assert page.selected_record().task_id == second.task_id
    store.close()


def test_ffmpeg_result_has_chinese_summary(qtbot, monkeypatch, tmp_path):
    page = SettingsPage(SettingsStore(app_data=tmp_path), CredentialStore(InMemoryCredentialBackend()))
    qtbot.addWidget(page)
    status = SimpleNamespace(available=False, diagnostic="FFmpeg not found: fixture failure")
    monkeypatch.setattr("galgame_news.desktop.settings_page.discover_ffmpeg", lambda **kwargs: status)
    assert page.diagnose_ffmpeg() is status
    assert "未找到" in page.ffmpeg_status.text()
    assert page.ffmpeg_diagnostic.text() == status.diagnostic


def test_completed_progress_remains_visible_after_controller_cleanup(qtbot, tmp_path):
    class Runner:
        def run(self, request, event_sink=None, cancellation_token=None):
            event_sink(ProgressEvent("news_completed", "t", "226", total_news=1, payload={"completed": 1}))
            return SimpleNamespace(status="completed")

    controller = DesktopController(runner_factory=Runner, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    assert controller.start_task(input_path=tmp_path / "226.docx", issue_id="226", output_dir=tmp_path / "output")
    qtbot.waitUntil(lambda: controller.thread is None and controller.progress_page.status_label.text() == "已完成")
    assert not controller.progress_page.progress_bar.isHidden()
    assert controller.progress_page.progress_bar.value() == 1
    assert controller.progress_page.progress_bar.maximum() == 1
    assert not controller.progress_page.stop_button.isEnabled()
    controller.progress_page.reset()
    assert controller.progress_page.progress_bar.isHidden()
    assert controller.progress_page.progress_bar.maximum() == 0


def test_expanded_progress_diagnostics_fit_minimum_window(qtbot, tmp_path):
    controller = DesktopController(app_data=tmp_path / "app")
    window = MainWindow(controller)
    qtbot.addWidget(window)
    apply_theme("light")
    window.resize(1100, 720)
    window.navigation.setCurrentRow(1)
    page = controller.progress_page
    page.set_running(True)
    page.handle_event(ProgressEvent("news_started", "t", "226", total_news=20, news_index=1,
                                    message="原文新闻标题 " * 40))
    page.technical_group.setChecked(True)
    page.technical_log.setPlainText("diagnostic line\n" * 30)
    window.show()
    qtbot.waitUntil(lambda: page.stop_button.isVisible())
    window.layout().activate()
    assert window.size().width() == 1100
    assert window.size().height() == 720
    assert window.minimumSizeHint().height() <= 720
    for button in (page.pause_button, page.stop_button):
        position = button.mapTo(window, button.rect().topLeft())
        assert 0 <= position.y() < window.height()
        assert position.y() + button.height() <= window.height()
        assert button.isEnabled()
    page.content_scroll.verticalScrollBar().setValue(page.content_scroll.verticalScrollBar().maximum())
    assert page.technical_log.isVisible()


def test_ui_retry_false_result_replaces_running_message(qtbot, monkeypatch, tmp_path):
    controller = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(controller.review_page)
    monkeypatch.setattr(controller, "retry_news", lambda news_id: False)
    controller.review_page.operation_status.setText("正在重试本条新闻…")
    controller.review_page.retry_requested.emit("news-1")
    assert "未完成" in controller.review_page.operation_status.text()
    assert "正在重试" not in controller.review_page.operation_status.text()


def test_ui_retry_factory_error_emits_existing_failure_and_raw_detail(qtbot, tmp_path):
    from galgame_news.review import ReviewSession

    def fail_factory():
        raise RuntimeError("runner factory failed: fixture diagnostic")

    controller = DesktopController(runner_factory=fail_factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.review_page)
    output = tmp_path / "output"
    output.mkdir()
    (output / "image_index.json").write_text('{"candidates": [], "news_items": [{"news_id": "news-1", "title": "新闻一", "section": "新作"}]}', encoding="utf-8")
    controller.review_record = controller.task_store.create_task("226", tmp_path / "226.docx")
    controller.review_page.set_session(ReviewSession.from_output(output))
    controller.review_page.retry_url_edit.setText("https://official.test/news")
    seen = []
    controller.retry_failed.connect(seen.append)
    controller.review_page.retry_requested.emit("news-1")
    assert len(seen) == 1
    assert isinstance(seen[0], RuntimeError)
    assert "重试失败" in controller.review_page.operation_status.text()
    assert "runner factory failed" in controller.review_page.raw_details.toPlainText()
    assert controller.review_page.raw_toggle.isChecked()


def test_ui_retry_preserves_specific_failure_when_public_api_returns_false(qtbot, monkeypatch, tmp_path):
    controller = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(controller.review_page)

    def retry(news_id):
        controller.retry_failed.emit(ValueError("specific fixture failure"))
        return False

    monkeypatch.setattr(controller, "retry_news", retry)
    controller.review_page.retry_requested.emit("news-1")
    assert "重试失败" in controller.review_page.operation_status.text()
    assert "specific fixture failure" in controller.review_page.raw_details.toPlainText()
