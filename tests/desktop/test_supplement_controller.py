from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from PIL import Image
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
import pytest

from galgame_news.desktop.controller import DesktopController
from galgame_news.review import ReviewDecision
from galgame_news.tasks import TaskStore


def _make_controller_fixture(tmp_path: Path, runner_factory, *, request_options=None):
    source = tmp_path / "issue.docx"
    source.write_bytes(b"fixture")
    task_root = tmp_path / "task-root"
    store = TaskStore(tmp_path / "app")
    record = store.create_task("issue-1", source, task_root=task_root)
    raw = task_root / "raw"
    raw.mkdir(exist_ok=True)
    Image.new("RGB", (400, 400), "red").save(raw / "old.jpg")
    (raw / "image_index.json").write_text(json.dumps({
        "issue_id": "issue-1",
        "news_items": [
            {"news_id": "news-1", "sequence": 1, "section": "新作", "title": "有图新闻"},
            {"news_id": "news-empty", "sequence": 2, "section": "汉化", "title": "尚无图片的新闻"},
        ],
        "candidates": [{
            "news_id": "news-1",
            "image_url": "https://images.example/old.jpg",
            "source_url": "https://official.example/old",
            "local_path": "old.jpg",
            "width": 400,
            "height": 400,
        }],
    }), encoding="utf-8")
    (raw / "video_index.json").write_text(json.dumps({"videos": []}), encoding="utf-8")
    if request_options is not None:
        store.save_request_options(record, request_options)
    controller = DesktopController(
        runner_factory=runner_factory,
        task_store=store,
        app_data=tmp_path / "unused-app",
    )
    controller.load_review(record)
    return controller, store, record


class _SupplementRunner:
    def __init__(self, *, blocked=False):
        self.started = Event()
        self.release = Event()
        self.blocked = blocked
        self.kwargs = None
        self.worker_thread = False
        self.retry_calls = []

    def supplement_news(self, **kwargs):
        self.kwargs = kwargs
        self.worker_thread = QThread.currentThread() is not QApplication.instance().thread()
        self.started.set()
        if self.blocked:
            while not kwargs["cancellation_token"].is_cancelled:
                self.release.wait(0.01)
        task_root = Path(kwargs["task_root"])
        news_id = kwargs["news_id"]
        attempt_id = "supplement-controller-test"
        attempt = task_root / "retries" / news_id / attempt_id
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / "images").mkdir(exist_ok=True)
        Image.new("RGB", (400, 400), "blue").save(attempt / "images" / "new.jpg")
        candidate = {
            "news_id": news_id,
            "image_url": "https://images.example/supplement.jpg",
            "source_url": kwargs["source_url"],
            "local_path": "images/new.jpg",
            "width": 400,
            "height": 400,
            "fetched_at": "2026-10-11T00:00:00Z",
            "download_status": "downloaded",
            "source_type": "official_site",
            "selected": False,
            "curation_status": "unselected",
        }
        (attempt / "image_index.json").write_text(json.dumps({
            "kind": "supplement",
            "source_url": kwargs["source_url"],
            "candidates": [candidate],
            "failures": [],
        }), encoding="utf-8")
        (attempt / "video_index.json").write_text(json.dumps({"videos": []}), encoding="utf-8")
        token = kwargs["cancellation_token"]
        return SimpleNamespace(
            path=attempt,
            attempt_id=attempt_id,
            news_id=news_id,
            status="cancelled" if token.is_cancelled else "completed",
            success_count=0 if token.is_cancelled else 1,
            failed_count=0,
            failures=[],
        )

    def retry_news(self, **kwargs):
        self.retry_calls.append(kwargs)


@pytest.mark.parametrize(
    ("saved_options", "expected_socialdata"),
    [({}, False), ({"use_socialdata_x": True}, True)],
)
def test_controller_supplements_empty_news_in_worker_and_restores_review_state(
    qtbot, tmp_path: Path, saved_options, expected_socialdata,
):
    runner = _SupplementRunner()
    controller, store, record = _make_controller_fixture(
        tmp_path, runner_factory=lambda: runner, request_options=saved_options,
    )
    qtbot.addWidget(controller.review_page)
    try:
        session = controller.review_page.session
        assert session is not None
        assert not [image for image in session.images if image.news_id == "news-empty"]
        original = next(image for image in session.images if image.news_id == "news-1")
        session.set_decision(str(original.id), ReviewDecision.ACCEPTED)

        assert controller.supplement_news("news-empty", "https://official.example/gallery") is True
        qtbot.waitUntil(lambda: runner.started.is_set(), timeout=2000)
        qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=3000)

        assert runner.worker_thread is True
        assert runner.kwargs["news_id"] == "news-empty"
        assert runner.kwargs["source_url"] == "https://official.example/gallery"
        assert runner.kwargs["request"].use_socialdata_x is expected_socialdata
        added = [image for image in session.images if image.image_url == "https://images.example/supplement.jpg"]
        assert len(added) == 1
        assert session.decision(str(added[0].id)) is ReviewDecision.PENDING
        assert session.decision(str(original.id)) is ReviewDecision.ACCEPTED

        reopened = controller.load_review(record)
        assert reopened is not None
        assert reopened.decision(str(original.id)) is ReviewDecision.ACCEPTED
        assert reopened.decision(str(added[0].id)) is ReviewDecision.PENDING
        attempts = store.list_retry_attempts(record)
        assert len(attempts) == 1 and attempts[0].kind == "supplement"
        assert Path(reopened.task_root).resolve() == Path(record.task_root).resolve()
    finally:
        controller.close()
        store.close()


def test_supplement_blocks_task_retry_and_second_supplement_and_close_cancels_worker(
    qtbot, tmp_path: Path,
):
    runner = _SupplementRunner(blocked=True)
    controller, store, _record = _make_controller_fixture(
        tmp_path, runner_factory=lambda: runner,
    )
    qtbot.addWidget(controller.review_page)
    input_path = tmp_path / "another.docx"
    input_path.write_bytes(b"fixture")
    try:
        assert controller.supplement_news("news-empty", "https://official.example/gallery") is True
        qtbot.waitUntil(lambda: runner.started.is_set(), timeout=2000)
        assert controller.is_supplement_running
        token = runner.kwargs["cancellation_token"]

        assert controller.start_task(input_path=input_path, issue_id="other", output_dir=tmp_path / "other") is False
        assert controller.retry_news("news-empty", "https://official.example/retry") is False
        assert controller.supplement_news("news-empty", "https://official.example/second") is False
        assert controller.review_page._supplement_busy
        assert controller.review_page.supplement_cancel_button.isEnabled()
        assert runner.retry_calls == []

        assert controller.close(timeout_ms=2000) is True
        assert token.is_cancelled
        qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=2000)
        assert controller._supplement_thread is None
    finally:
        if controller.is_supplement_running:
            controller.cancel_supplement()
            qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=2000)
        store.close()


def test_imported_task_supplement_uses_catalog_retry_root(tmp_path: Path, qtbot):
    legacy_output = tmp_path / "legacy-output"
    legacy_output.mkdir()
    (legacy_output / "image_index.json").write_text(json.dumps({
        "issue_id": "imported-issue",
        "news_items": [{
            "news_id": "news-empty", "sequence": 1, "section": "新作", "title": "旧任务中的新闻",
        }],
        "candidates": [],
    }), encoding="utf-8")
    (legacy_output / "video_index.json").write_text(json.dumps({"videos": []}), encoding="utf-8")
    store = TaskStore(tmp_path / "app")
    record = store.import_output(legacy_output)
    runner = _SupplementRunner()
    controller = DesktopController(
        runner_factory=lambda: runner,
        task_store=store,
        app_data=tmp_path / "unused-app",
    )
    qtbot.addWidget(controller.review_page)
    try:
        session = controller.load_review(record)
        assert session is not None
        expected_retry_root = Path(record.task_root) / "retries"
        assert Path(session.retry_root).resolve() == expected_retry_root.resolve()

        assert controller.supplement_news("news-empty", "https://official.example/gallery") is True
        qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=3000)

        assert Path(runner.kwargs["task_root"]).resolve() == Path(record.task_root).resolve()
        assert [attempt.kind for attempt in store.list_retry_attempts(record)] == ["supplement"]
        reopened = controller.load_review(record)
        assert reopened is not None
        assert any(image.image_url == "https://images.example/supplement.jpg" for image in reopened.images)
    finally:
        controller.close()
        store.close()
