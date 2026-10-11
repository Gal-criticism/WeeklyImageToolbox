"""Offline thread-boundary and closing regressions for review operations."""
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QThread, QTimer

from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.window import MainWindow
from galgame_news.review import ReviewDecision, ReviewSession


def fixture_controller(tmp_path, runner):
    controller = DesktopController(runner_factory=lambda: runner, app_data=tmp_path / "app")
    source = tmp_path / "issue.docx"
    source.write_bytes(b"offline fixture")
    record = controller.task_store.create_task("test", source, task_root=tmp_path / "task")
    raw = record.root_path / "raw"
    Image.new("RGB", (800, 600), "red").save(raw / "old.png")
    (raw / "image_index.json").write_text(json.dumps({
        "news_items": [{"news_id": "news-1", "section": "汉化", "title": "真实作品", "sequence": 1}],
        "candidates": [{"id": "old", "news_id": "news-1", "image_url": "https://public.example/old.png",
                        "source_url": "https://public.example/gallery", "local_path": "old.png",
                        "width": 800, "height": 600}],
    }), encoding="utf-8")
    session = controller.load_review(record)
    assert session is not None
    return controller, record, session


class SlowRunner:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls = []

    def retry_news(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        self.release.wait(3)
        root = Path(kwargs["task_root"]) / "retries" / kwargs["news_id"] / "retry-local"
        root.mkdir(parents=True)
        Image.new("RGB", (800, 600), "blue").save(root / "new.png")
        (root / "image_index.json").write_text(json.dumps({"candidates": [{
            "id": "new", "news_id": kwargs["news_id"], "image_url": "https://public.example/new.png",
            "source_url": kwargs["official_url"], "local_path": "new.png", "width": 800, "height": 600,
            "selected": True,
        }]}), encoding="utf-8")
        return "retry-local"


def test_retry_and_verification_background_preserve_latest_decision(qtbot, qapp, tmp_path, monkeypatch):
    runner = SlowRunner()
    controller, record, session = fixture_controller(tmp_path, runner)
    qtbot.addWidget(controller.review_page)
    original_prepare = ReviewSession.prepare_retry_delta
    preparation_threads = []

    def prepare(self, *args, **kwargs):
        preparation_threads.append(QThread.currentThread() is not qapp.thread())
        return original_prepare(self, *args, **kwargs)

    monkeypatch.setattr(ReviewSession, "prepare_retry_delta", prepare)
    ticks = []
    timer = QTimer(controller)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    try:
        assert controller.retry_news("news-1", "https://public.example/gallery")
        qtbot.waitUntil(runner.entered.is_set)
        assert not controller.retry_news("news-1", "https://public.example/gallery")
        assert not controller.supplement_news("news-1", "https://public.example/extra")
        controller.review_page.export_final()
        assert controller.review_page.exporter.session is None
        session.set_decisions(["old"], ReviewDecision.ACCEPTED, persist=False)
        controller.review_page.state_writer.enqueue(session)
        qtbot.wait(60)
        assert len(ticks) >= 3
        runner.release.set()
        qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=5000)
        qtbot.waitUntil(lambda: not controller.review_page.state_writer.busy)
        assert preparation_threads == [True]
        assert len(runner.calls) == 1
        assert runner.calls[0]["news_item"]["title"] == "真实作品"
        assert runner.calls[0]["request"].use_socialdata_x is False
        assert session.decision("old") is ReviewDecision.ACCEPTED
        assert session.decision("new") is ReviewDecision.ACCEPTED
        reopened = controller.load_review(record)
        assert reopened.decision("old") is ReviewDecision.ACCEPTED
        assert reopened.decision("new") is ReviewDecision.ACCEPTED
    finally:
        timer.stop()
        runner.release.set()
        assert controller.close(5000)
        controller.task_store.close()


def test_window_waits_for_retry_merge_and_state_write(qtbot, tmp_path):
    runner = SlowRunner()
    controller, _record, session = fixture_controller(tmp_path, runner)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.show()
    try:
        assert controller.retry_news("news-1", "https://public.example/gallery")
        qtbot.waitUntil(runner.entered.is_set)
        assert window.close() is False
        assert window.isVisible()
        assert runner.calls[0]["cancellation_token"].is_cancelled
        runner.release.set()
        qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)
        assert not controller.has_pending_work
        assert any(str(item.id) == "new" for item in session.images)
    finally:
        runner.release.set()
        controller.close(5000)
        controller.task_store.close()


def test_old_callback_cannot_merge_into_current_session(qtbot, tmp_path):
    controller, _record, session = fixture_controller(tmp_path, SlowRunner())
    qtbot.addWidget(controller.review_page)
    before = list(session.images)
    controller._on_supplement_finished((("stale", 0, "operation"), None, None))
    assert session.images == before
    assert controller.close(5000)
    controller.task_store.close()


def test_sync_retry_id_still_reports_cancelled_batch_counts(qtbot, tmp_path):
    class CancelledRunner(SlowRunner):
        def retry_news(self, **kwargs):
            attempt_id = super().retry_news(**kwargs)
            path = Path(kwargs["task_root"]) / "retries" / kwargs["news_id"] / attempt_id / "image_index.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update(kind="retry", status="cancelled")
            payload["candidates"].append({
                "id": "missing", "news_id": kwargs["news_id"], "image_url": "https://public.example/missing.png",
                "width": 800, "height": 600, "download_status": "retryable_failed",
            })
            path.write_text(json.dumps(payload), encoding="utf-8")
            return attempt_id

    runner = CancelledRunner()
    runner.release.set()
    controller, record, session = fixture_controller(tmp_path, runner)
    qtbot.addWidget(controller.review_page)
    assert controller.retry_news("news-1", "https://public.example/gallery")
    qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=5000)
    caption = controller.review_page.operation_status.text()
    assert "重试已取消" in caption and "成功 1 项" in caption and "失败 1 项" in caption
    assert session._image_by_id["missing"].image_url == "https://public.example/missing.png"
