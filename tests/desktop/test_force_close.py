from __future__ import annotations

from types import SimpleNamespace
import time

import pytest

from galgame_news.desktop.controller import DesktopController, _TaskMigrationWorker
from galgame_news.pipeline import CancellationToken


@pytest.mark.parametrize("operation", ["grab", "paused", "retry", "supplement", "migration", "export", "export_wait"])
def test_real_controller_accounts_for_business_operations(qtbot, tmp_path, operation):
    controller = DesktopController(app_data=tmp_path / "app")
    page = controller.review_page
    assert not controller.has_active_operations
    try:
        if operation in {"grab", "paused"}:
            controller._thread = SimpleNamespace(isRunning=lambda: True)
            controller._token = CancellationToken()
            if operation == "paused":
                controller._token.pause()
        elif operation in {"retry", "supplement"}:
            controller._supplement_active = True
        elif operation == "migration":
            controller._migration_active = True
        elif operation == "export":
            page.exporter.session = object()
        else:
            page._export_waiting_session = object()
        assert controller.has_active_operations
    finally:
        controller._thread = controller._token = None
        controller._supplement_active = controller._migration_active = False
        page.exporter.session = page._export_waiting_session = None


def test_force_close_never_drains_queries_or_failed_save_and_cancels_every_token(qtbot, tmp_path, monkeypatch):
    controller = DesktopController(app_data=tmp_path / "app")
    page = controller.review_page
    grab = CancellationToken()
    grab.pause()
    supplement = CancellationToken()
    migration = _TaskMigrationWorker(tmp_path / "task")
    controller._thread = SimpleNamespace(isRunning=lambda: True)
    controller._token = grab
    controller._supplement_token = supplement
    controller._migration_worker = migration
    controller._migration_active = True
    page._export_waiting_session = object()

    def forbidden(*args, **kwargs):
        raise AssertionError("force close must not wait, persist or drain workers")

    with monkeypatch.context() as patch:
        patch.setattr(controller, "close", forbidden)
        patch.setattr(controller, "persist_state", forbidden)
        patch.setattr(page, "finish_close", forbidden)
        patch.setattr(page.state_writer, "has_failure", lambda *args: True)
        patch.setattr(page.state_writer, "enqueue", forbidden)
        patch.setattr(page.query.pool, "close", forbidden)
        patch.setattr(page.state_writer.pool, "close", forbidden)
        try:
            started = time.perf_counter()
            controller.begin_force_close()
            controller.begin_force_close()
            assert time.perf_counter() - started < 0.1
            assert grab.is_cancelled and supplement.is_cancelled and migration.cancelled.is_set()
            assert controller._force_closing
            assert page._force_closing_ui
            assert page._export_waiting_session is None
            assert not page.isEnabled()
        finally:
            controller._thread = controller._token = controller._supplement_token = None
            controller._migration_worker = None
            controller._migration_active = False


def test_ui_queries_thumbnails_and_failed_save_do_not_count_as_operations(qtbot, tmp_path, monkeypatch):
    controller = DesktopController(app_data=tmp_path / "app")
    with monkeypatch.context() as patch:
        patch.setattr(type(controller._review_loader), "busy", property(lambda _: True))
        patch.setattr(type(controller.review_page.state_writer), "busy", property(lambda _: True))
        patch.setattr(controller.review_page.state_writer, "has_failure", lambda *args: True)
        assert controller.has_pending_work
        assert not controller.has_active_operations


def test_stale_results_cannot_change_page_or_merge_after_force_close(qtbot, tmp_path, monkeypatch):
    controller = DesktopController(app_data=tmp_path / "app")
    page = controller.review_page
    controller.begin_force_close()
    before = (page.operation_status.text(), page.save_status.text(), page.export_status.text())
    # Invalid payloads would fail if any callback attempted to inspect or merge them.
    controller._on_event(None)
    controller._on_supplement_event(None)
    controller._on_migration_finished(None)
    controller._on_migration_failed(RuntimeError("stale"))
    controller._review_load_ready((controller._review_load_generation,), None, None)
    page._state_saved(None, 99)
    page._state_failed(None, 99, RuntimeError("stale"))
    page._export_done(None, None, "stale")
    assert (page.operation_status.text(), page.save_status.text(), page.export_status.text()) == before


def test_migration_worker_passes_cancellation_event(qtbot, tmp_path, monkeypatch):
    worker = _TaskMigrationWorker(tmp_path / "task")
    seen = []
    def migrate(root, **options):
        seen.append(options["cancelled"])
        assert options["cancelled"].is_set()
        return object()
    monkeypatch.setattr("galgame_news.desktop.controller.migrate_task", migrate)
    worker.cancel()
    worker.run()
    assert seen == [worker.cancelled]
