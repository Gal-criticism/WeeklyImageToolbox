from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.window import MainWindow


def test_main_window_has_five_pages_and_switches_navigation(qtbot, tmp_path: Path):
    controller = DesktopController(app_data=tmp_path / "app")
    window = MainWindow(controller, exit_handler=lambda: None)
    qtbot.addWidget(window)
    assert window.navigation.count() == 5
    assert window.nav_list is window.navigation
    assert window.stacked_widget is window.pages
    assert [window.navigation.item(i).text() for i in range(5)] == [
        "新建任务",
        "抓取进度",
        "图片审核",
        "历史任务",
        "设置",
    ]
    window.navigation.setCurrentRow(2)
    assert window.pages.currentWidget() is window.review_page


class _MessageBoxMock:
    instances: list["_MessageBoxMock"] = []
    next_result = "退出"
    on_exec = None

    class Icon:
        Warning = object()

    class ButtonRole:
        AcceptRole = object()
        RejectRole = object()

    def __init__(self, icon, title, text, parent):
        self.icon = icon
        self.title = title
        self.text = text
        self.parent = parent
        self.buttons = []
        self.default_button = None
        self.clicked = None
        type(self).instances.append(self)

    def addButton(self, text, role):
        button = type("Button", (), {"text": text, "role": role})()
        self.buttons.append(button)
        return button

    def setDefaultButton(self, button):
        self.default_button = button

    def exec(self):
        if type(self).on_exec:
            type(self).on_exec(self)
        self.clicked = next((button for button in self.buttons if button.text == type(self).next_result), None)
        return 0

    def clickedButton(self):
        return self.clicked


def _patch_message_box(monkeypatch, *, answer="退出", on_exec=None):
    _MessageBoxMock.instances = []
    _MessageBoxMock.next_result = answer
    _MessageBoxMock.on_exec = on_exec
    monkeypatch.setattr("galgame_news.desktop.window.QMessageBox", _MessageBoxMock)


def _set_busy(monkeypatch, controller, operation: str):
    # Keep this UI test independent of how each operation is represented inside
    # the controller; the controller's own tests cover its operation accounting.
    monkeypatch.setattr(
        type(controller), "has_active_operations", property(lambda self: self._test_busy), raising=False
    )
    controller._test_busy = operation


def test_idle_close_skips_prompt_and_requests_forced_close(qtbot, tmp_path: Path, monkeypatch):
    _patch_message_box(monkeypatch)
    controller = DesktopController(app_data=tmp_path / "app")
    calls = []
    monkeypatch.setattr(controller, "begin_force_close", lambda: calls.append("begin"), raising=False)
    exited = []
    window = MainWindow(controller, exit_handler=lambda: exited.append("exit"))
    qtbot.addWidget(window)
    window.show()

    assert window.close() is True
    assert not window.isVisible()
    assert _MessageBoxMock.instances == []
    assert calls == ["begin"]
    assert exited == ["exit"]

    assert window.close() is True
    assert calls == ["begin"]
    assert exited == ["exit"]


@pytest.mark.parametrize(
    "operation",
    ["grab", "paused grab", "retry", "supplement", "migration", "export", "waiting for save"],
)
def test_busy_close_prompts_with_cancel_as_default_and_confirms_force_close(
    qtbot, tmp_path: Path, monkeypatch, operation: str
):
    _patch_message_box(monkeypatch, answer="退出")
    controller = DesktopController(app_data=tmp_path / "app")
    _set_busy(monkeypatch, controller, operation)
    calls = []
    monkeypatch.setattr(controller, "begin_force_close", lambda: calls.append("begin"), raising=False)
    exited = []
    window = MainWindow(controller, exit_handler=lambda: exited.append("exit"))
    qtbot.addWidget(window)
    window.show()

    assert window.close() is True
    prompt = _MessageBoxMock.instances[0]
    assert prompt.text == "任务正在进行，退出将中断任务，尚未保存的审核改动可能丢失。是否退出？"
    assert [button.text for button in prompt.buttons] == ["退出", "取消"]
    assert prompt.default_button.text == "取消"
    assert calls == ["begin"]
    assert exited == ["exit"]


def test_busy_close_cancel_preserves_operation_and_does_not_exit(qtbot, tmp_path: Path, monkeypatch):
    _patch_message_box(monkeypatch, answer="取消")
    controller = DesktopController(app_data=tmp_path / "app")
    _set_busy(monkeypatch, controller, "supplement")
    calls = []
    monkeypatch.setattr(controller, "begin_force_close", lambda: calls.append("begin"), raising=False)
    exited = []
    window = MainWindow(controller, exit_handler=lambda: exited.append("exit"))
    qtbot.addWidget(window)
    window.show()

    assert window.close() is False
    assert window.isVisible()
    assert controller.has_active_operations == "supplement"
    assert calls == []
    assert exited == []


def test_cancelled_close_leaves_live_grab_running_and_waits_before_teardown(qtbot, tmp_path: Path, monkeypatch):
    entered = Event()
    release = Event()

    class _DelayedRunner:
        def run(self, request, event_sink=None, cancellation_token=None):
            entered.set()
            release.wait(5)
            return SimpleNamespace(status="completed", task_dir=tmp_path / "task")

    _patch_message_box(monkeypatch, answer="取消")
    controller = DesktopController(runner_factory=_DelayedRunner, app_data=tmp_path / "app")
    window = MainWindow(controller, exit_handler=lambda: None)
    qtbot.addWidget(window)
    assert controller.start_task(
        input_path=tmp_path / "issue.docx",
        issue_id="issue",
        output_dir=tmp_path / "output",
    ) is True
    try:
        assert entered.wait(2)
        window.show()
        assert window.close() is False
        assert controller.is_running
        assert controller._token is not None and not controller._token.is_cancelled
        assert len(_MessageBoxMock.instances) == 1
    finally:
        release.set()
        qtbot.waitUntil(lambda: not controller.is_running, timeout=4000)


def test_repeated_close_while_prompt_is_open_shows_one_dialog(qtbot, tmp_path: Path, monkeypatch):
    controller = DesktopController(app_data=tmp_path / "app")
    _set_busy(monkeypatch, controller, "migration")
    nested_results = []
    window_ref = []

    def close_again(_dialog):
        nested_results.append(window_ref[0].close())

    _patch_message_box(monkeypatch, answer="取消", on_exec=close_again)
    window = MainWindow(controller, exit_handler=lambda: None)
    window_ref.append(window)
    qtbot.addWidget(window)
    window.show()

    assert window.close() is False
    assert len(nested_results) == 1
    assert len(_MessageBoxMock.instances) == 1
    assert window.isVisible()


def test_save_failure_and_non_business_state_do_not_prompt(qtbot, tmp_path: Path, monkeypatch):
    _patch_message_box(monkeypatch)
    controller = DesktopController(app_data=tmp_path / "app")
    # A failed prior save and ordinary loaded state are not active operations.
    controller.review_page.state_writer._failed["simulated-save-failure"] = RuntimeError("disk full")
    assert controller.review_page.state_writer.has_failure()
    calls = []
    monkeypatch.setattr(controller, "begin_force_close", lambda: calls.append("begin"), raising=False)
    window = MainWindow(controller, exit_handler=lambda: None)
    qtbot.addWidget(window)

    assert window.close() is True
    assert _MessageBoxMock.instances == []
    assert calls == ["begin"]


def test_cleanup_error_still_commits_close_and_calls_exit_handler(qtbot, tmp_path: Path, monkeypatch):
    _patch_message_box(monkeypatch)
    controller = DesktopController(app_data=tmp_path / "app")

    def fail_cleanup():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(controller, "begin_force_close", fail_cleanup, raising=False)
    exited = []
    window = MainWindow(controller, exit_handler=lambda: exited.append("exit"))
    qtbot.addWidget(window)
    window.show()

    assert window.close() is True
    assert not window.isVisible()
    assert window._exit_committed is True
    assert exited == ["exit"]

    assert window.close() is True
    assert exited == ["exit"]
