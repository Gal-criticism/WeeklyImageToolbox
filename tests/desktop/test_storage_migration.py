from __future__ import annotations

import threading
from pathlib import Path

from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.history_page import HistoryPage
from galgame_news.tasks import TaskStore


def test_empty_background_refresh_preserves_retry_summary(qtbot):
    from galgame_news.desktop.review_page import ReviewPage

    page = ReviewPage()
    qtbot.addWidget(page)
    page._retry_context = (id(page.session), "previous-news")
    summary = "重试已取消：成功 1 项，失败 1 项"
    page.operation_status.setText(summary)
    page._clear_details()
    assert page.operation_status.text() == summary
    page._bind_retry_context("newly-rendered-news")
    assert page.operation_status.text() == summary


def test_history_exposes_migration_signal_and_busy_state(qtbot, tmp_path):
    store = TaskStore(tmp_path / "app")
    input_path = tmp_path / "input.docx"
    input_path.write_bytes(b"input")
    record = store.create_task("1", input_path, task_root=tmp_path / "task")
    page = HistoryPage(store)
    qtbot.addWidget(page)
    page.task_list.setCurrentRow(0)
    received = []
    page.migration_requested.connect(received.append)
    page.migrate_button.click()
    assert received == [record]
    page.set_migration_busy(True)
    assert not page.migrate_button.isEnabled()
    assert not page.task_list.isEnabled()
    page.set_migration_busy(False)
    assert page.migrate_button.isEnabled()


def test_imported_task_migrates_actual_external_source_off_gui_thread(
    monkeypatch, qtbot, tmp_path
):
    import galgame_news.desktop.controller as controller_module

    source_root = tmp_path / "legacy"
    raw = source_root / "raw"
    raw.mkdir(parents=True)
    (raw / "image_index.json").write_text(
        '{"schema_version": 1, "issue_id": "7", "candidates": []}', encoding="utf-8"
    )
    store = TaskStore(tmp_path / "app")
    stub = tmp_path / "app" / "stub"
    record = store.import_output(raw, issue_id="7", task_root=stub)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def migrate(root, **kwargs):
        assert kwargs.get("private_task_root") == stub
        calls.append((Path(root), threading.current_thread().name))
        entered.set()
        release.wait(2)
        return {
            "task_root": str(root),
            "migrated": False,
            "counts": {},
            "warnings": [],
            "diagnostics": [],
        }

    monkeypatch.setattr(controller_module, "migrate_task", migrate)
    controller = DesktopController(task_store=store, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    assert controller.migration_task(record)
    assert entered.wait(1)
    assert controller._migration_active
    assert not controller.start_task(
        input_path=tmp_path / "x", issue_id="x", output_dir=tmp_path
    )
    release.set()
    qtbot.waitUntil(
        lambda: controller.history_page.migration_status.text().startswith(
            "迁移未完成"
        ),
        timeout=3000,
    )
    assert calls and calls[0][0] == source_root
    assert calls[0][1] != threading.current_thread().name
    assert "迁移未完成" in controller.history_page.migration_status.text()


def test_storage_cli_dispatches_without_constructing_application(
    monkeypatch, tmp_path, capsys
):
    from galgame_news import cli
    from galgame_news.tasks import migration, repository

    calls = []
    monkeypatch.setattr(
        cli,
        "Application",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("paid pipeline invoked")),
    )
    monkeypatch.setattr(
        migration,
        "migrate_task",
        lambda root: calls.append(("migrate", Path(root))) or {"migrated": True},
    )

    class FakeRepository:
        def __init__(self, root, create=False):
            self.root = Path(root)

        @staticmethod
        def is_authoritative(root):
            return True

        def integrity_check(self):
            calls.append(("check", self.root))
            return {"ok": True}

        def backup(self, destination):
            calls.append(("backup", self.root, Path(destination)))
            return Path(destination)

    monkeypatch.setattr(repository, "TaskRepository", FakeRepository)
    taskdir, backup = tmp_path / "task", tmp_path / "backup"
    assert cli.main(["migrate-storage", str(taskdir)]) == 0
    assert cli.main(["check-storage", str(taskdir)]) == 0
    assert cli.main(["backup-storage", str(taskdir), str(backup)]) == 0
    assert [row[0] for row in calls] == ["migrate", "check", "backup"]
    assert '"authoritative": true' in capsys.readouterr().out


def test_imported_migration_worker_preserves_private_manual_decisions(tmp_path):
    import json
    from galgame_news.desktop.controller import _TaskMigrationWorker
    from galgame_news.tasks.repository import TaskRepository

    root = tmp_path / "legacy"
    root.mkdir()
    (root / "review_state.json").write_text(json.dumps({"schema_version": 1,
        "revision": 1, "decisions": {"a": "rejected"}, "manual_decisions": []}), encoding="utf-8")
    private = tmp_path / "stub" / "review_state.json"
    private.parent.mkdir()
    latest = {"schema_version": 1, "revision": 7,
              "decisions": {"a": "accepted"}, "manual_decisions": ["a"]}
    private.write_text(json.dumps(latest), encoding="utf-8")
    worker = _TaskMigrationWorker(root, private)
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert failures == []
    assert TaskRepository(root, create=False).load_review_state() == latest
    assert json.loads(private.read_text(encoding="utf-8")) == latest
