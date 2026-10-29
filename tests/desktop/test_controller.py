from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from galgame_news.pipeline import ProgressEvent, TaskRequest
from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.worker import PipelineWorker


def test_frozen_config_path_prefers_meipass_bundle_config(monkeypatch, tmp_path: Path):
    import galgame_news.config as config_module

    meipass = tmp_path / "_internal"
    bundled = meipass / "config" / "default.toml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("# bundled config\n", encoding="utf-8")
    monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_module.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(
        config_module.sys,
        "executable",
        str(tmp_path / "GalgameNewsToolbox.exe"),
        raising=False,
    )

    assert config_module._default_path() == bundled


class _FakeRunner:
    def __init__(self, *, started: list[str] | None = None) -> None:
        self.started = started if started is not None else []

    def run(self, request, event_sink=None, cancellation_token=None):
        self.started.append(request.issue_id)
        event_sink(
            ProgressEvent(
                kind="news_completed",
                task_id=request.task_id or "task",
                issue_id=request.issue_id,
                news_id="news-1",
                news_index=1,
                total_news=1,
                message="done",
                payload={"completed": 1},
            )
        )
        return object()


class _ResultRunner:
    def __init__(self, task_dirs: list[Path], requests: list[object]) -> None:
        self.task_dirs = task_dirs
        self.requests = requests

    def run(self, request, event_sink=None, cancellation_token=None):
        self.requests.append(request)
        task_dir = self.task_dirs.pop(0)
        return SimpleNamespace(status="completed", task_dir=task_dir)


def test_controller_persists_real_task_dir_and_resume_uses_it(qtbot, tmp_path: Path):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"docx")
    output_dir = tmp_path / "output"
    real_task = output_dir / "issue-real-task"
    requests: list[object] = []
    controller = DesktopController(
        runner_factory=lambda: _ResultRunner([real_task, real_task], requests),
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)

    assert controller.start_task(
        input_path=input_path,
        issue_id="issue",
        output_dir=output_dir,
        use_socialdata_x=True,
        use_archive_materials=True,
        archive_range="all",
        selected_sections=["x", "z"],
    ) is True
    running_record = controller.task_store.active_task
    assert running_record is not None
    assert ".desktop-catalog" not in running_record.task_root

    qtbot.waitUntil(lambda: not controller.is_running, timeout=2000)
    completed_record = controller.task_store.active_task
    assert completed_record is not None
    assert Path(completed_record.task_root) == real_task
    assert controller.task_store.load_request_options(completed_record)["selected_sections"] == ["x", "z"]
    assert controller.task_store.load_request_options(completed_record)["archive_range"] == "all"
    assert requests[0].images_only is False

    assert controller.resume_task(completed_record) is True
    qtbot.waitUntil(lambda: not controller.is_running, timeout=2000)
    assert requests[1].resume is True
    assert requests[1].use_socialdata_x is True
    assert requests[1].use_archive_materials is True
    assert requests[1].archive_range == "all"
    assert requests[1].selected_sections == ["x", "z"]
    assert Path(requests[1].task_dir) == real_task
    assert Path(requests[1].output_dir) == output_dir


def test_controller_saves_request_options_before_starting_runner(qtbot, tmp_path: Path):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"docx")
    task_dir = tmp_path / "output" / "task"
    requests: list[object] = []
    controller = None

    def factory():
        record = controller.task_store.active_task
        assert record is not None
        options = controller.task_store.load_request_options(record)
        assert options["selected_sections"] == ["h", "z"]
        assert options["offline"] is True
        return _ResultRunner([task_dir], requests)

    controller = DesktopController(runner_factory=factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)

    assert controller.start_task(
        input_path=input_path,
        issue_id="issue",
        output_dir=tmp_path / "output",
        offline=True,
        selected_sections=["h", "z"],
    ) is True
    qtbot.waitUntil(lambda: not controller.is_running, timeout=2000)


def test_controller_old_request_options_resume_with_all_sections(qtbot, tmp_path: Path):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"docx")
    output_dir = tmp_path / "output"
    task_dir = output_dir / "legacy-task"
    requests: list[object] = []
    controller = DesktopController(
        runner_factory=lambda: _ResultRunner([task_dir], requests),
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)
    record = controller.task_store.create_task("issue", input_path, task_root=task_dir)
    controller.task_store.save_request_options(record, {"offline": True, "images_only": True})

    assert controller.resume_task(record) is True
    qtbot.waitUntil(lambda: not controller.is_running, timeout=2000)

    assert requests[0].resume is True
    assert requests[0].selected_sections == ["x", "h", "z"]
    assert requests[0].images_only is False
    assert requests[0].archive_range == "year"
    assert requests[0].supplement_sources == ("official", "store", "ymgal", "vndb")


def test_controller_persists_and_resumes_supplement_source_selection(qtbot, tmp_path: Path):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"docx")
    output_dir = tmp_path / "output"
    task_dir = output_dir / "task"
    requests: list[object] = []
    controller = DesktopController(runner_factory=lambda: _ResultRunner([task_dir], requests),
                                   app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    record = controller.task_store.create_task("issue", input_path, task_root=task_dir)
    controller.task_store.save_request_options(record, {
        "offline": False, "use_image_supplement": True, "supplement_target_images": 7,
        "supplement_sources": ["store", "vndb"],
    })

    assert controller.resume_task(record) is True
    qtbot.waitUntil(lambda: not controller.is_running, timeout=2000)

    assert requests[0].use_image_supplement is True
    assert requests[0].supplement_target_images == 7
    assert requests[0].supplement_sources == ("store", "vndb")
    updated = controller.task_store.get_task(record.task_id)
    assert controller.task_store.load_request_options(updated)["supplement_sources"] == ["store", "vndb"]


def test_task_request_legacy_images_only_flag_is_read_but_ignored(tmp_path: Path):
    request = TaskRequest(
        input_path=tmp_path / "issue.docx",
        issue_id="issue",
        output_dir=tmp_path / "output",
        images_only=True,
    )

    assert request.images_only is False


def test_controller_allows_only_one_running_task(qtbot, tmp_path: Path):
    runners: list[_FakeRunner] = []

    def factory():
        runner = _FakeRunner()
        runners.append(runner)
        return runner

    controller = DesktopController(runner_factory=factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    assert controller.start_task(
        input_path=tmp_path / "issue.docx",
        issue_id="issue-1",
        output_dir=tmp_path / "task",
    ) is True
    assert controller.start_task(
        input_path=tmp_path / "issue.docx",
        issue_id="issue-2",
        output_dir=tmp_path / "task-2",
    ) is False
    controller.cancel_task()
    controller.wait_for_task(1000)
    assert controller.is_running is False
    assert len(runners) == 1


def test_controller_surfaces_runner_factory_failure_and_clears_running_state(
    qtbot, tmp_path: Path
):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"docx")
    message = "default configuration is missing"

    class _FailingFactory:
        def __call__(self):
            raise FileNotFoundError(message)

    controller = DesktopController(
        runner_factory=_FailingFactory(),
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)

    try:
        assert controller.start_task(
            input_path=input_path,
            issue_id="issue",
            output_dir=tmp_path / "output",
            offline=True,
            no_videos=True,
        ) is False
        assert controller.is_running is False
        assert controller.last_error is not None
        assert message in str(controller.last_error)
        assert controller.progress_page.status_label.text() == "执行失败"
        assert controller.progress_page.failed_label.text() == "1"
        assert message in controller.progress_page.technical_log.toPlainText()
        assert controller.new_task_page.start_button.isEnabled()
    finally:
        controller.close()
        controller.task_store.close()


def test_pipeline_worker_emits_structured_events_from_worker_thread(qtbot):
    request = TaskRequest(input_path="input.docx", issue_id="issue", output_dir="output")
    worker = PipelineWorker(_FakeRunner(), request)
    events: list[ProgressEvent] = []
    worker.event.connect(events.append)
    worker.run()
    assert events and isinstance(events[0], ProgressEvent)
    assert worker.thread() is not None


def test_worker_cancellation_is_cooperative():
    class _CancellingRunner:
        def run(self, request, event_sink=None, cancellation_token=None):
            cancellation_token.cancel()
            return "cancelled"

    worker = PipelineWorker(_CancellingRunner(), object())
    results: list[object] = []
    worker.finished.connect(results.append)
    worker.run()
    assert results == []


def test_pipeline_worker_routes_failed_task_result_to_failed(qtbot):
    failed_result = SimpleNamespace(status="failed")

    class _FailedRunner:
        def run(self, request, event_sink=None, cancellation_token=None):
            return failed_result

    worker = PipelineWorker(_FailedRunner(), object())
    finished: list[object] = []
    failed: list[object] = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert finished == []
    assert failed == [failed_result]


def test_controller_shows_failed_when_runner_returns_empty_news_failure(qtbot, tmp_path: Path):
    failure = SimpleNamespace(code="no_news_items", message="No news items were recognized")
    failed_result = SimpleNamespace(
        status="failed",
        failures=[failure],
        task_dir=tmp_path / "task",
    )

    class _EmptyNewsRunner:
        def run(self, request, event_sink=None, cancellation_token=None):
            return failed_result

    input_path = tmp_path / "empty.docx"
    input_path.write_bytes(b"fixture")
    controller = DesktopController(
        runner_factory=_EmptyNewsRunner,
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)

    try:
        assert controller.start_task(
            input_path=input_path,
            issue_id="empty",
            output_dir=tmp_path / "output",
        ) is True
        qtbot.waitUntil(
            lambda: controller.progress_page.status_label.text() == "执行失败"
            and not controller.is_running,
            timeout=2000,
        )
        assert controller.progress_page.status_label.text() == "执行失败"
        assert controller.progress_page.completed_label.text() == "0"
        assert controller.progress_page.total_label.text() == "0"
        assert "No news items were recognized" in controller.progress_page.technical_log.toPlainText()
    finally:
        controller.close()
        controller.task_store.close()


def test_controller_retry_merges_one_news_attempt_without_overwriting_raw(qtbot, tmp_path: Path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"docx")
    task_root = tmp_path / "task"
    store = __import__("galgame_news.tasks", fromlist=["TaskStore"]).TaskStore(tmp_path / "app")
    record = store.create_task("issue", source, task_root=task_root)
    raw = task_root / "raw"
    candidate = {
        "id": "old",
        "news_id": "news-1",
        "image_url": "https://cdn.example/old.jpg",
        "source_url": "https://official.example/news",
        "local_path": None,
        "width": 400,
        "height": 400,
    }
    (raw / "image_index.json").write_text(
        json.dumps({"issue_id": "issue", "news_items": [{"news_id": "news-1"}], "candidates": [candidate]}),
        encoding="utf-8",
    )
    before = (raw / "image_index.json").read_bytes()

    class _RetryRunner:
        def retry_news(self, *, task_root, news_id, official_url, **_kwargs):
            assert Path(task_root) == task_root_path
            assert news_id == "news-1"
            assert official_url == "https://official.example/retry"
            attempt = Path(task_root) / "retries" / news_id / "attempt-1"
            attempt.mkdir(parents=True)
            replacement = dict(candidate, id="new", image_url="https://cdn.example/new.jpg")
            (attempt / "image_index.json").write_text(
                json.dumps({"candidates": [replacement]}), encoding="utf-8"
            )
            (attempt / "video_index.json").write_text(json.dumps({"videos": []}), encoding="utf-8")
            return "attempt-1"

    task_root_path = task_root
    controller = DesktopController(
        runner_factory=_RetryRunner,
        task_store=store,
        app_data=tmp_path / "unused-app",
    )
    qtbot.addWidget(controller.review_page)
    session = controller.load_review(record)
    assert session is not None

    assert controller.retry_news("news-1", "https://official.example/retry") is True
    qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=3000)
    assert [str(item.id) for item in session.images] == ["old", "new"]
    assert (raw / "image_index.json").read_bytes() == before
    assert controller.close(5000)
    store.close()
