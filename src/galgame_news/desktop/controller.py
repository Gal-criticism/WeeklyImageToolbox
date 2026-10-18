"""Desktop orchestration facade and Qt thread boundary."""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import uuid
from threading import Event
from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot, QCoreApplication, QElapsedTimer, QTimer
from PySide6.QtWidgets import QMessageBox

from ..pipeline import CancellationToken, PipelineRunner, ProgressEvent, TaskRequest
from ..review import ReviewSession
from ..delivery.asset_paths import resolve_review_index_root
from ..settings import CredentialStore, InMemoryCredentialBackend, SettingsStore
from ..tasks import TaskRecord, TaskStore
from ..tasks.migration import migrate_task
from .history_page import HistoryPage
from .new_task_page import NewTaskPage
from .progress_page import ProgressPage
from .review_page import ReviewPage
from .settings_page import SettingsPage
from .theme import apply_theme
from .worker import PipelineWorker, ReviewGrabWorker
from ..diagnostics import diagnose_error, format_error
from .review_jobs import ReviewQuery


class _TaskMigrationWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, task_root: Path, review_state_path: Path | None = None,
                 private_task_root: Path | None = None):
        super().__init__()
        self.task_root = task_root
        self.review_state_path = review_state_path
        self.private_task_root = private_task_root
        self.cancelled = Event()

    def cancel(self):
        self.cancelled.set()

    @Slot()
    def run(self):
        try:
            from ..tasks.repository import TaskRepository
            options = {"cancelled": self.cancelled}
            if not TaskRepository.is_authoritative(self.task_root):
                if self.review_state_path is not None and self.review_state_path.exists():
                    options["review_state_override"] = json.loads(
                        self.review_state_path.read_text(encoding="utf-8"))
                if self.private_task_root is not None:
                    options["private_task_root"] = self.private_task_root
            report = migrate_task(self.task_root, **options)
            self.finished.emit(report)
        except BaseException as exc:
            self.failed.emit(exc)


class DesktopController(QObject):
    """Own one optional pipeline worker and the five page models/widgets."""

    task_started = Signal(object)
    progress_event = Signal(object)
    task_finished = Signal(object)
    task_cancelled = Signal(object)
    task_failed = Signal(object)
    task_paused = Signal()
    task_resumed = Signal()
    stop_requested = Signal()
    review_loaded = Signal(object)
    retry_finished = Signal(object)
    retry_failed = Signal(object)
    supplement_finished = Signal(object)
    supplement_failed = Signal(object)
    history_changed = Signal()
    work_changed = Signal()

    # Camel-case aliases are useful to Qt-oriented callers while snake-case is
    # the normal Python spelling.
    taskStarted = Signal(object)
    progressEvent = Signal(object)
    taskFinished = Signal(object)
    taskCancelled = Signal(object)
    taskFailed = Signal(object)

    def __init__(
        self,
        *,
        runner_factory: Callable[[], Any] | None = None,
        task_store: TaskStore | None = None,
        settings_store: SettingsStore | None = None,
        credential_store: CredentialStore | None = None,
        app_data: Path | str | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.runner_factory = runner_factory or (lambda: PipelineRunner())
        self.task_store = task_store or TaskStore(app_data)
        self.settings_store = settings_store or SettingsStore(app_data=app_data)
        if credential_store is None:
            try:
                credential_store = CredentialStore()
            except RuntimeError:
                credential_store = CredentialStore(InMemoryCredentialBackend())
        self.credential_store = credential_store
        settings = self.settings_store.load() if self.settings_store.path.exists() else None
        default_output = settings.default_output_path if settings is not None else None

        self.new_task_page = NewTaskPage(default_output)
        self.progress_page = ProgressPage()
        self.review_page = ReviewPage()
        self.history_page = HistoryPage(self.task_store)
        self.settings_page = SettingsPage(self.settings_store, self.credential_store)
        self.new_task_page.start_requested.connect(self._start_from_page)
        self.new_task_page.import_requested.connect(self._import_from_page)
        self.progress_page.pause_requested.connect(self.pause_task)
        self.progress_page.resume_requested.connect(self.resume_current_task)
        self.progress_page.stop_requested.connect(self.cancel_task)
        self.history_page.review_requested.connect(self._load_review_from_page)
        self.history_page.migration_requested.connect(self.migration_task)
        self.history_page.resume_requested.connect(self.resume_task)
        self.review_page.retry_requested.connect(self._retry_from_page)
        self.retry_failed.connect(self.review_page.show_retry_failure)
        self.review_page.supplement_requested.connect(self.supplement_news)
        self.review_page.supplement_cancel_requested.connect(self.cancel_supplement)
        self.settings_page.theme_changed.connect(self.apply_theme)

        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._token: CancellationToken | None = None
        self._supplement_thread = None
        self._supplement_worker = None
        self._supplement_token = None
        self._supplement_context = None
        self._supplement_active = False
        self._grab_result_done = self._grab_thread_done = False
        self._closing = False
        self._force_closing = False
        self._migration_active = False
        self._migration_thread: QThread | None = None
        self._migration_worker: _TaskMigrationWorker | None = None
        self._migration_record: TaskRecord | None = None
        self._migration_result: dict[str, Any] | None = None
        self._migration_error: BaseException | None = None
        self._review_load_generation = 0
        self._review_loader = ReviewQuery(self)
        self._review_loader.ready.connect(self._review_load_ready)
        self._review_loader.idle.connect(self.work_changed)
        self.work_changed.connect(self._update_migration_action)
        self.task_started.connect(self._update_migration_action)
        self.task_finished.connect(self._update_migration_action)
        self.task_cancelled.connect(self._update_migration_action)
        self.task_failed.connect(self._update_migration_action)
        self._review_loader.idle.connect(self._update_migration_action)
        self.review_page.state_writer.idle.connect(self._update_migration_action)
        self.review_page.exporter.finished.connect(self._update_migration_action)
        self.review_page.export_button.clicked.connect(self._update_migration_action)
        self.current_request: TaskRequest | None = None
        self.current_record: TaskRecord | None = None
        self.review_record: TaskRecord | None = None
        self.last_result: Any = None
        self.last_error: BaseException | None = None
        self._update_migration_action()

    @property
    def worker(self) -> PipelineWorker | None:
        return self._worker

    @property
    def thread(self) -> QThread | None:
        return self._thread

    @property
    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.isRunning())

    @property
    def is_paused(self) -> bool:
        return bool(self._token is not None and self._token.is_paused)

    @property
    def is_supplement_running(self) -> bool:
        return self._supplement_active

    @property
    def has_pending_work(self) -> bool:
        page = self.review_page
        return bool(self.is_running or self.is_supplement_running or self._migration_active
                    or (self._migration_thread is not None and self._migration_thread.isRunning()) or
                    self._review_loader.busy or page.exporter.session is not None or page.has_pending_work)

    @property
    def has_active_operations(self) -> bool:
        """User-started operations which require an exit confirmation."""
        page = self.review_page
        return bool(self.is_running or self.is_supplement_running or self._migration_active
                    or (self._migration_thread is not None and self._migration_thread.isRunning())
                    or page.exporter.busy or page._export_waiting_session is not None)

    @Slot()
    def _update_migration_action(self, *_args) -> None:
        page = self.review_page
        blocked = bool(self._closing or self._migration_active or self.is_running or self.is_supplement_running
                       or self._review_loader.busy or page.exporter.busy or page._export_waiting_session is not None
                       or page.state_writer.busy)
        self.history_page.set_migration_blocked(blocked)

    def migration_task(self, record: object) -> bool:
        """Migrate one legacy task off the GUI thread, then reload its review session."""
        if not isinstance(record, TaskRecord):
            return False
        page = self.review_page
        if (self._closing or self._migration_active or self.is_running or self.is_supplement_running
                or self._review_loader.busy or page.exporter.busy or page._export_waiting_session is not None
                or page.state_writer.busy or page.has_pending_work):
            self.history_page.show_migration_report("已有抓取、审核保存、载入或导出正在运行，请结束后再迁移。")
            return False
        session = page.session
        if session is not None and not page._is_state_saved(session):
            if not page.state_writer.has_failure(session):
                page.state_writer.enqueue(session)
            self.history_page.show_migration_report("审核决定正在保存；保存完成后请再次启动迁移。")
            return False
        try:
            root = record.root_path
            if record.imported and record.source_output_path is not None:
                source = resolve_review_index_root(record.source_output_path)
                root = source.parent if source.name.casefold() == "raw" else source
            root = root.expanduser().resolve()
        except (OSError, ValueError, TypeError) as exc:
            self.history_page.show_migration_report(f"无法定位任务目录：{exc}")
            return False
        self._migration_active = True
        self._migration_record = record
        self._migration_result = None
        self._migration_error = None
        self.history_page.set_migration_busy(True)
        self.history_page.show_migration_report(f"正在迁移任务存储：{root}")
        self.review_page.setEnabled(False)
        self.new_task_page.set_busy(True)
        self._migration_worker = _TaskMigrationWorker(
            root, record.review_state_path if record.imported else None,
            record.root_path if record.imported else None)
        self._migration_thread = QThread(self)
        self._migration_worker.moveToThread(self._migration_thread)
        self._migration_thread.started.connect(self._migration_worker.run)
        self._migration_worker.finished.connect(self._on_migration_finished)
        self._migration_worker.failed.connect(self._on_migration_failed)
        self._migration_worker.finished.connect(self._migration_thread.quit, Qt.ConnectionType.DirectConnection)
        self._migration_worker.failed.connect(self._migration_thread.quit, Qt.ConnectionType.DirectConnection)
        self._migration_thread.finished.connect(self._migration_worker.deleteLater)
        self._migration_thread.finished.connect(self._on_migration_thread_finished)
        self.work_changed.emit()
        self._migration_thread.start()
        return True

    @Slot(object)
    def _on_migration_finished(self, report: object) -> None:
        if self._force_closing:
            return
        self._migration_result = report if isinstance(report, dict) else {"migrated": False}

    @Slot(object)
    def _on_migration_failed(self, error: object) -> None:
        if self._force_closing:
            return
        self._migration_error = error if isinstance(error, BaseException) else RuntimeError(str(error))

    @Slot()
    def _on_migration_thread_finished(self) -> None:
        if self.sender() is not self._migration_thread:
            return
        thread = self._migration_thread
        record, report, error = self._migration_record, self._migration_result, self._migration_error
        self._migration_active = False
        self._migration_thread = self._migration_worker = None
        self._migration_record = None
        self._migration_result = None
        self._migration_error = None
        if thread is not None:
            thread.deleteLater()
        if self._force_closing:
            return
        self.history_page.set_migration_busy(False)
        self.review_page.setEnabled(True)
        self.new_task_page.set_busy(self.is_running or self.is_supplement_running)
        if error is not None:
            self.history_page.show_migration_report(f"迁移失败，原任务文件保留：{error}")
        else:
            report = report or {}
            counts = report.get("counts", {})
            summary = "、".join(f"{name} {count} 项" for name, count in sorted(counts.items())) or "无新记录"
            diagnostics = report.get("diagnostics", [])
            warning_count = len(report.get("warnings", [])) + len(diagnostics)
            label = "已是 SQLite 权威存储" if report.get("already_authoritative") else "迁移完成" if report.get("migrated") else "迁移未完成"
            message = f"{label}：{summary}。"
            if warning_count:
                message += f" 诊断 {warning_count} 条；详情见技术信息。"
            self.history_page.show_migration_report(message)
            if diagnostics:
                self.review_page.show_diagnostics(diagnostics)
            if report.get("migrated") and record is not None and not self._closing:
                self._load_review_from_page(record)
        self.work_changed.emit()

    def _start_from_page(self, payload: object) -> bool:
        values = dict(payload) if isinstance(payload, dict) else {}
        return self.start_task(**values)

    def start_task(
        self,
        request: TaskRequest | None = None,
        *,
        input_path: Path | str | None = None,
        issue_id: str | None = None,
        output_dir: Path | str | None = None,
        offline: bool = False,
        no_videos: bool = False,
        use_socialdata_x: bool = False,
        selected_sections: list[str] | None = None,
        images_only: bool = False,
        resume: bool = False,
        task_dir: Path | str | None = None,
        task_id: str | None = None,
    ) -> bool:
        if (self._closing or self._migration_active or self.is_running or self.is_supplement_running or self._review_loader.busy
                or self.review_page.exporter.busy or self.review_page._export_waiting_session is not None):
            return False
        if request is None:
            if input_path is None or issue_id is None or output_dir is None:
                raise ValueError("input_path, issue_id, and output_dir are required")
            input_value = Path(input_path)
            output_value = Path(output_dir)
            # Register before starting so history has a stable running row.
            # The pipeline binds this row to its timestamped task directory
            # when the worker returns.
            self.current_record = self.task_store.create_task(str(issue_id), input_value)
            request = TaskRequest(
                input_path=input_value,
                issue_id=str(issue_id),
                output_dir=output_value,
                offline=offline,
                no_videos=no_videos,
                use_socialdata_x=use_socialdata_x,
                images_only=images_only,
                selected_sections=(
                    ["x", "h", "z"] if selected_sections is None else selected_sections
                ),
                resume=resume,
                task_dir=task_dir,
                task_id=task_id or self.current_record.task_id,
            )
        elif request.task_id:
            self.current_record = self.task_store.get_task(request.task_id)
        self.current_request = request
        self._save_request_options(self.current_record, request)
        self.last_error = None
        self.last_result = None
        self.progress_page.reset()
        self.progress_page.set_running(True)
        self.new_task_page.set_busy(True)
        self._token = CancellationToken()
        try:
            runner = self.runner_factory()
            self._configure_runner_browser(runner)
        except BaseException as exc:  # startup failures must not strand the UI in Running
            self._on_failed(exc)
            return False
        self._worker = PipelineWorker(runner, request, self._token)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self._on_event)
        self._worker.finished.connect(self._on_finished)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failed)
        # ``quit`` is thread-safe.  A direct connection avoids waiting for
        # the GUI event loop when callers close the window and wait for the
        # cooperative worker to finish.
        self._worker.finished.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.cancelled.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.failed.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._update_migration_action)
        self._thread.start()
        self.task_started.emit(request)
        self.taskStarted.emit(request)
        return True

    def resume_task(self, record: object | None = None) -> bool:
        # Keep the historical checkpoint-resume API while allowing the
        # progress page to use the same verb for an in-place pause/resume.
        if record is None:
            return self.resume_current_task()
        if not isinstance(record, TaskRecord) or self.is_running or self._migration_active:
            return False
        if record.input_path is None or record.images_only:
            return False
        input_path = Path(record.input_path)
        output_dir = Path(record.task_root).parent
        options = self.task_store.load_request_options(record)
        request = TaskRequest(
            input_path=input_path,
            issue_id=record.issue_id,
            output_dir=output_dir,
            offline=bool(options.get("offline", False)),
            no_videos=bool(options.get("no_videos", False)),
            use_socialdata_x=bool(options.get("use_socialdata_x", False)),
            selected_sections=options.get("selected_sections", ["x", "h", "z"]),
            # Older request_options.json files may contain images_only=true.
            # The option is retained for reading compatibility but must never
            # cause an existing task to discard its recovery data on resume.
            images_only=False,
            config_path=options.get("config_path"),
            history_db=options.get("history_db"),
            max_images=options.get("max_images"),
            llm_provider=options.get("llm_provider"),
            llm_model=options.get("llm_model"),
            resume=True,
            task_dir=Path(record.task_root),
            task_id=record.task_id,
        )
        return self.start_task(request)

    @Slot(object)
    def _on_event(self, event: object) -> None:
        if self._force_closing:
            return
        if isinstance(event, ProgressEvent):
            if event.kind == "task_workspace_ready" and self.current_record is not None and self.current_request is not None:
                root = Path(event.payload.get("task_dir", ""))
                if (event.task_id == self.current_record.task_id and root.is_absolute()
                        and root.resolve().is_relative_to(self.current_request.output_dir.resolve())
                        and root.is_dir()):
                    updated = self.task_store.update_task_root(self.current_record, root, materialize=False)
                    if updated is not None:
                        self.current_record = updated
                        if not updated.images_only:
                            self._save_request_options(updated, self.current_request)
            self.progress_page.handle_event(event)
            self.progress_event.emit(event)
            self.progressEvent.emit(event)

    @Slot(object)
    def _on_finished(self, result: object) -> None:
        if self._force_closing:
            self._quit_thread()
            return
        self.last_result = result
        self._bind_result_task_dir(result)
        self.progress_page.set_finished("Completed")
        self.progress_page.log.appendPlainText(
            "任务目录保留 raw、索引、检查点、缓存、审核状态和 final 图片交付。"
        )
        self.new_task_page.set_busy(False)
        self.task_finished.emit(result)
        self.taskFinished.emit(result)
        self._quit_thread()

    @Slot(object)
    def _on_cancelled(self, value: object) -> None:
        if self._force_closing:
            self._quit_thread()
            return
        self.last_result = value
        self._bind_result_task_dir(value)
        self.progress_page.set_finished("Cancelled")
        self.new_task_page.set_busy(False)
        self.task_cancelled.emit(value)
        self.taskCancelled.emit(value)
        self._quit_thread()

    @Slot(object)
    def _on_failed(self, error: object) -> None:
        if self._force_closing:
            self._quit_thread()
            return
        self.last_error = error if isinstance(error, BaseException) else RuntimeError(str(error))
        self._bind_result_task_dir(error)
        if isinstance(error, BaseException):
            message = str(error) or type(error).__name__
            self.progress_page.report_failure(message)
        else:
            failures = getattr(error, "failures", None) or []
            failure = next(iter(failures), None)
            if failure is not None:
                self.progress_page.report_failure(str(getattr(failure, "message", failure)), count=False)
        self.progress_page.set_finished("Failed")
        self.new_task_page.set_busy(False)
        self.task_failed.emit(error)
        self.taskFailed.emit(error)
        self._quit_thread()

    def _quit_thread(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()

    @Slot()
    def cancel_task(self) -> bool:
        if not self.is_running:
            return False
        self.stop_requested.emit()
        if self._worker is not None:
            self._worker.cancel()
        elif self._token is not None:
            self._token.cancel()
        return True

    stop_task = cancel_task

    @Slot()
    def pause_task(self) -> bool:
        if not self.is_running or self._token is None or self._token.is_cancelled:
            return False
        if self._token.is_paused:
            return True
        if self._worker is not None:
            self._worker.pause()
        else:
            self._token.pause()
        self.progress_page.set_paused(True)
        self.task_paused.emit()
        return True

    @Slot()
    def resume_current_task(self) -> bool:
        if not self.is_running or self._token is None or self._token.is_cancelled:
            return False
        if not self._token.is_paused:
            return True
        if self._worker is not None:
            self._worker.resume()
        else:
            self._token.resume()
        self.progress_page.set_paused(False)
        self.task_resumed.emit()
        return True

    resume_paused_task = resume_current_task

    def wait_for_task(self, timeout_ms: int = 5000) -> bool:
        if self._thread is None:
            return True
        finished = self._thread.wait(max(0, int(timeout_ms)))
        if finished:
            self._on_thread_finished()
        return bool(finished)

    @Slot()
    def _on_thread_finished(self) -> None:
        # A queued signal from a previous run can arrive after resume starts.
        # Qt's sender identity lets that stale signal leave the new run alone
        # without retaining a Python closure around the old QThread.
        sender = self.sender()
        if sender is not None and sender is not self._thread:
            return
        if self._thread is None and self._worker is None and self._token is None:
            return
        if self._thread is not None and not self._thread.wait(0):
            QTimer.singleShot(10, self._on_thread_finished)
            return
        self.progress_page.set_running(False)
        self.new_task_page.set_busy(False)
        completed_thread = self._thread
        self._thread = None
        self._worker = None
        self._token = None
        if completed_thread is not None:
            completed_thread.deleteLater()
        # Closing callers may already have closed the catalog before a queued
        # thread-finished signal arrives. Only release thread state at shutdown.
        if not self._closing:
            self.history_page.refresh()
            self.history_changed.emit()

    def import_output(self, path: str | Path) -> TaskRecord:
        if self._migration_active:
            raise RuntimeError("任务存储迁移期间不能导入或切换审核任务")
        record = self.task_store.import_output(path)
        self.history_page.refresh()
        self.load_review(record)
        return record

    def _import_from_page(self, path: str) -> None:
        try:
            self.import_output(path)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self.new_task_page, "无法导入图片结果", str(exc))

    def load_review(self, record: object) -> ReviewSession | None:
        if (self._migration_active or self.is_supplement_running or self.review_page.exporter.busy or self.review_page.state_writer.busy
                or self.review_page.state_writer.has_failure()):
            return self.review_page.session
        if not isinstance(record, TaskRecord):
            return None
        self.review_record = record
        if record.images_only:
            self.review_page.set_session(None)
            self.review_record = None
            self.progress_page.log.appendPlainText("此任务仅保留图片，请从历史任务打开图片目录人工选择；不支持审核或续跑。")
            return None
        source = record.source_output_path
        if source is None:
            raw = record.root_path / "raw"
            source = raw if raw.is_dir() else record.root_path
        try:
            source = resolve_review_index_root(source)
            # Pipeline indexes live in raw; review export belongs to that
            # same E:/... task, independently of the AppData catalog/state.
            export_root = record.root_path
            if source.name.casefold() == "raw":
                export_root = source.parent
            elif record.imported:
                export_root = source.parent / f"{source.name}_审核结果"
            session = ReviewSession.from_output(
                source,
                task_root=export_root,
                state_path=record.review_state_path,
            )
            session.retry_root = self.task_store.storage_root(record) / "retries"
            if record.imported and session.retry_root.parent != record.root_path.resolve():
                session.legacy_retry_roots = (record.root_path.resolve() / "retries",)
            for attempt in self.task_store.list_retry_attempts(record):
                try:
                    session.merge_retry_attempt(attempt, force_pending=attempt.kind == "supplement")
                except (OSError, ValueError, TypeError) as exc:
                    self.progress_page.log.appendPlainText(format_error(exc, stage="恢复补抓批次"))
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            self.review_page.set_session(None)
            self.review_record = None
            QMessageBox.warning(self.review_page, "无法打开图片审核", str(exc))
            return None
        self.review_page.set_session(session)
        warnings = getattr(self.task_store, "retry_diagnostics", [])
        if warnings:
            self.review_page.show_diagnostics(warnings)
        self.review_loaded.emit(session)
        return session

    @Slot(object)
    def _load_review_from_page(self, record):
        """History clicks load/validate their indexes off the GUI thread."""
        if self._closing or self._migration_active or self.is_supplement_running or self.review_page.exporter.session is not None:
            return
        if not isinstance(record, TaskRecord):
            return
        self._review_load_generation += 1
        self._queue_review_load(record, self._review_load_generation)

    def _queue_review_load(self, record, generation):
        if self._closing or self._migration_active or generation != self._review_load_generation:
            return
        if self.review_page.state_writer.busy:
            QTimer.singleShot(30, lambda: self._queue_review_load(record, generation))
            return
        if self.review_page.state_writer.has_failure():
            self.review_page.operation_status.setText("审核决定尚未保存，请重试保存后再打开其他任务")
            return
        self.review_page.operation_status.setText("正在读取索引与校验图片…")
        identity = (generation, record.task_id)
        app_data = self.task_store.root

        def load():
            source = record.source_output_path or (record.root_path / "raw" if (record.root_path / "raw").is_dir() else record.root_path)
            source = resolve_review_index_root(source)
            export_root = source.parent if source.name.casefold() == "raw" else record.root_path
            if record.imported and source.name.casefold() != "raw":
                export_root = source.parent / f"{source.name}_审核结果"
            session = ReviewSession.from_output(source, task_root=export_root, state_path=record.review_state_path)
            store = TaskStore(app_data, recover_on_startup=False)
            try:
                session.retry_root = store.storage_root(record) / "retries"
                if record.imported and session.retry_root.parent != record.root_path.resolve():
                    session.legacy_retry_roots = (record.root_path.resolve() / "retries",)
                for attempt in store.list_retry_attempts(record):
                    try:
                        session.merge_retry_attempt(attempt, force_pending=attempt.kind == "supplement", persist=False)
                    except (ValueError, OSError, TypeError) as exc:
                        store.retry_diagnostics.append(diagnose_error(exc, stage="恢复批次", source=str(attempt.path)))
                return record, session, list(store.retry_diagnostics)
            finally:
                store.close()
        self._review_loader.request(identity, load)
        self.work_changed.emit()

    @Slot(object, object, object)
    def _review_load_ready(self, identity, result, error):
        if self._closing or identity[0] != self._review_load_generation:
            return
        if error is not None:
            self.review_page.operation_status.setText(format_error(error, stage="载入审核"))
            self.review_page.show_diagnostics([diagnose_error(error, stage="载入审核")])
            return
        if self.review_page.state_writer.busy:
            QTimer.singleShot(10, self, lambda: self._review_load_ready(identity, result, error))
            return
        if self.review_page.state_writer.has_failure():
            self.review_page.operation_status.setText("审核决定尚未保存，请重试保存后再打开其他任务")
            return
        record, session, warnings = result
        previous = self.review_page.session
        if previous is not None and previous.state_path == session.state_path:
            # Loading validates an independent session. Decisions made while
            # validation ran remain owned by the active GUI session.
            for decision in set(previous._decisions.values()):
                ids = [media_id for media_id in previous._manual_decisions
                       if media_id in session._decisions and previous.decision(media_id) is decision]
                session.set_decisions(ids, decision, persist=False)
        self.review_record = record
        self.review_page.set_session(session)
        self.review_page.state_writer.enqueue(session)
        if warnings:
            self.review_page.show_diagnostics(warnings)
        self.review_loaded.emit(session)

    def _bind_result_task_dir(self, result: object) -> None:
        record = self.current_record
        task_dir = getattr(result, "task_dir", None)
        if record is None or task_dir is None:
            return
        try:
            if getattr(result, "images_only", False):
                updated = self.task_store.update_task_root(record.task_id, Path(task_dir), materialize=False)
            else:
                updated = self.task_store.update_task_root(record.task_id, Path(task_dir))
        except (OSError, TypeError, ValueError):
            return
        if updated is None:
            return
        self.current_record = updated
        if self.current_request is not None and not getattr(result, "images_only", False):
            self._save_request_options(updated, self.current_request)
        if self.review_record is not None and self.review_record.task_id == updated.task_id:
            self.review_record = updated
        self.history_page.refresh()

    def _save_request_options(
        self, record: TaskRecord | None, request: TaskRequest
    ) -> None:
        if record is None:
            return
        self.task_store.save_request_options(
            record,
            {
                "offline": request.offline,
                "no_videos": request.no_videos,
                "use_socialdata_x": request.use_socialdata_x,
                "selected_sections": list(request.selected_sections),
                "images_only": request.images_only,
                "config_path": str(request.config_path) if request.config_path else None,
                "history_db": str(request.history_db) if request.history_db else None,
                "max_images": request.max_images,
                "llm_provider": request.llm_provider,
                "llm_model": request.llm_model,
            },
        )

    def retry_news(self, news_id: object, official_url: str | None = None) -> bool:
        """Start a retry; completion is reported asynchronously via retry_finished."""
        if isinstance(news_id, dict):
            official_url = str(news_id.get("official_url") or official_url or "")
            news_id = news_id.get("news_id") or news_id.get("id")
        elif isinstance(news_id, (tuple, list)):
            official_url = str(news_id[1]) if len(news_id) > 1 and official_url is None else official_url
            news_id = news_id[0] if news_id else ""
        return self._start_review_grab("retry", str(news_id or ""),
                                       str(official_url or self.review_page.retry_url()))

    @Slot(str, str)
    def supplement_news(self, news_id: str, source_url: str) -> bool:
        return self._start_review_grab("supplement", news_id, source_url)

    def _start_review_grab(self, kind: str, news_id: str, source_url: str) -> bool:
        if (self._closing or self._migration_active or self.is_running or self.is_supplement_running or self._review_loader.busy
                or self.review_page.exporter.busy or self.review_page._export_waiting_session is not None):
            self._show_grab_error(kind, RuntimeError("已有抓取或导出操作正在运行，请稍后重试"))
            if self.is_supplement_running:
                self.review_page.set_supplement_busy(True)
            return False
        record, session = self.review_record or self.current_record, self.review_page.session
        if record is None or session is None:
            return False
        news = next((n for n in session.news_items if str(n.get("news_id")) == str(news_id)), None)
        parsed = urlsplit(source_url.strip())
        if news is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            self._show_grab_error(kind, ValueError("请选择具体新闻并输入有效的 HTTP 或 HTTPS 来源链接"))
            return False
        try:
            options = self.task_store.load_request_options(record)
            storage_root = self.task_store.storage_root(record)
            request = TaskRequest(input_path=record.input_path or storage_root / "input.docx",
                issue_id=record.issue_id, output_dir=storage_root, task_id=record.task_id,
                no_videos=True, offline=bool(options.get("offline", False)),
                use_socialdata_x=bool(options.get("use_socialdata_x", False)),
                selected_sections=options.get("selected_sections", ["x", "h", "z"]),
                config_path=options.get("config_path"), history_db=options.get("history_db"), max_images=options.get("max_images"))
            runner = self.runner_factory()
            self._configure_runner_browser(runner)
            runner._supplement_credential_store = self.credential_store
            token = CancellationToken()
            identity = (record.task_id, id(session), uuid.uuid4().hex)
            kwargs = dict(task_root=storage_root, task_id=record.task_id,
                          issue_id=record.issue_id, news_id=str(news_id),
                          news_item=dict(news), request=request)
            kwargs["source_url" if kind == "supplement" else "official_url"] = source_url.strip()
            if kind == "retry":
                kwargs["input_path"] = record.input_path
            snapshot = session.query_snapshot()
            self._supplement_token = token
            self._supplement_context = (identity, record, session, kind)
            self._grab_result_done = self._grab_thread_done = False
            self._supplement_worker = ReviewGrabWorker(runner, kind, kwargs, token, snapshot, identity)
            self._supplement_thread = QThread(self)
            self._supplement_worker.moveToThread(self._supplement_thread)
            self._supplement_thread.started.connect(self._supplement_worker.run)
            self._supplement_worker.event.connect(self._on_supplement_event)
            self._supplement_worker.finished.connect(self._on_supplement_finished)
            self._supplement_worker.failed.connect(self._on_supplement_failed)
            self._supplement_worker.finished.connect(self._supplement_thread.quit, Qt.ConnectionType.DirectConnection)
            self._supplement_worker.failed.connect(self._supplement_thread.quit, Qt.ConnectionType.DirectConnection)
            self._supplement_thread.finished.connect(self._supplement_worker.deleteLater)
            self._supplement_thread.finished.connect(self._on_supplement_thread_finished)
            self._supplement_active = True
            self.review_page.set_supplement_busy(True)
            self.review_page.supplement_cancel_button.setText("取消抓取")
            self.review_page.operation_status.setText("正在采集补充来源…" if kind == "supplement" else "正在重试采集新闻…")
            self.new_task_page.set_busy(True)
            self._supplement_thread.start()
            self.work_changed.emit()
            return True
        except Exception as exc:
            self._show_grab_error(kind, exc)
            return False

    def _show_grab_error(self, kind, error):
        if kind == "retry":
            self.retry_failed.emit(error)
        else:
            self.review_page.show_supplement_failure(error)
            self.supplement_failed.emit(error)

    @Slot(object)
    def _on_supplement_event(self, event):
        if self._force_closing:
            return
        if self.sender() is self._supplement_worker and self._supplement_context is not None:
            self.review_page.operation_status.setText(str(event.message))

    @Slot(object)
    def _on_supplement_finished(self, value):
        if self._force_closing:
            self._grab_result_done = True
            self._finish_grab_if_ready()
            return
        identity, result, delta = value
        context = self._supplement_context
        if context is None or context[0] != identity:
            return
        _, record, session, kind = context
        try:
            # No manifest reading, hashing or decoding in the GUI coordinator.
            registration_errors = []
            try:
                self.task_store.register_retry_attempt(record, delta.news_id, delta.attempt_id, kind=kind)
            except sqlite3.Error as exc:
                # The confined manifest remains durable and can be recovered
                # as an orphan batch on reopen even if the catalog is locked.
                registration_errors.append(diagnose_error(exc, stage="登记抓取批次", candidate_id=delta.attempt_id))
            if self.review_page.session is session and (self.review_record or self.current_record).task_id == identity[0]:
                self.review_page.operation_status.setText("正在合并新增候选…")
                merged = session.apply_retry_delta(delta, force_pending=kind == "supplement", persist=False)
                self.review_page.state_writer.enqueue(session)
                self.review_page.refresh(preserve_position=True)
                summary = {"added_count": len(merged.added_image_ids),
                    "success_count": getattr(result, "success_count", sum(entry.source is not None for entry in delta.images)),
                    "failed_count": getattr(result, "failed_count", sum(entry.source is None for entry in delta.images)),
                    "status": getattr(result, "status", delta.status),
                    "failures": [f.model_dump(mode="json") if hasattr(f, "model_dump") else f
                                 for f in getattr(result, "failures", delta.failures)]}
                if kind == "supplement":
                    self.review_page.show_supplement_result(summary)
                    self.supplement_finished.emit(summary)
                else:
                    self.review_page.show_retry_summary(summary)
                    self.retry_finished.emit(merged)
                if delta.diagnostics or registration_errors:
                    summary_text = self.review_page.operation_status.text()
                    self.review_page.show_diagnostics([*delta.diagnostics, *registration_errors])
                    self.review_page.operation_status.setText(summary_text + "；部分资产有诊断，请查看技术详情")
                self.review_page.set_supplement_busy(True)
        except Exception as exc:
            self._show_grab_error(kind, exc)
        finally:
            self._grab_result_done = True
            self._finish_grab_if_ready()

    @Slot(object)
    def _on_supplement_failed(self, value):
        if self._force_closing:
            self._grab_result_done = True
            self._finish_grab_if_ready()
            return
        identity, error = value
        if self._supplement_context is None or self._supplement_context[0] != identity:
            return
        self._show_grab_error(self._supplement_context[3], error)
        self.review_page.set_supplement_busy(True)
        self._grab_result_done = True
        self._finish_grab_if_ready()

    @Slot()
    def cancel_supplement(self):
        if self._supplement_token is not None:
            self._supplement_token.cancel()
            self.review_page.operation_status.setText("正在取消抓取，保留已下载图片…")

    @Slot()
    def _on_supplement_thread_finished(self):
        if self.sender() is not self._supplement_thread:
            return
        self._grab_thread_done = True
        self._finish_grab_if_ready()

    def _finish_grab_if_ready(self):
        if not (self._grab_result_done and self._grab_thread_done):
            return
        if self._supplement_thread is not None and not self._supplement_thread.wait(0):
            QTimer.singleShot(10, self._finish_grab_if_ready)
            return
        completed_thread = self._supplement_thread
        self._supplement_active = False
        self._supplement_thread = self._supplement_worker = self._supplement_token = None
        self._supplement_context = None
        if completed_thread is not None:
            completed_thread.deleteLater()
        if self._force_closing:
            return
        self.review_page.set_supplement_busy(False)
        self.new_task_page.set_busy(self.is_running)
        self.work_changed.emit()

    @Slot(str)
    def _retry_from_page(self, news_id: str) -> bool:
        before = self.review_page.operation_status.text()
        started = self.retry_news(news_id)
        if not started and self.review_page.operation_status.text() == before:
            self.review_page.show_retry_result(False)
        return started

    def apply_theme(self, theme: str) -> str:
        return apply_theme(theme)

    def _configure_runner_browser(self, runner) -> None:
        config = getattr(runner, "config", None)
        if config is not None and hasattr(config, "browser"):
            config = config.model_copy(deep=True)
            config.browser.enabled = self.settings_store.load().browser_enabled
            runner.config = config

    def persist_state(self) -> None:
        # SettingsPage saves the durable settings.  Refreshing history here is
        # intentionally non-destructive and ensures a close/stop sees a fresh
        # catalog in the next process.
        self.history_page.refresh()

    def begin_force_close(self) -> None:
        """Invalidate UI work and request cancellation without joining anything.

        The actual entry point owns process termination; this hook performs no
        file IO, database access or wait, even when a writer has failed.
        """
        if self._force_closing:
            return
        self._closing = self._force_closing = True
        self._review_load_generation += 1
        self._review_loader.invalidate()
        self.review_page.begin_force_close()
        self.new_task_page.setEnabled(False)
        self.progress_page.setEnabled(False)
        self.history_page.setEnabled(False)
        self.settings_page.setEnabled(False)
        if self._migration_worker is not None:
            self._migration_worker.cancel()
        self.cancel_supplement()
        self.cancel_task()

    def close(self, timeout_ms: int = 1000) -> bool:
        self._closing = True
        self.review_page.begin_close()
        self._review_load_generation += 1
        self._review_loader.invalidate()
        self.cancel_supplement()
        self.cancel_task()
        # Explicit graceful callers may request a bounded wait; the desktop
        # close button uses begin_force_close instead. Queued completion and
        # state-write signals still get delivered on the GUI thread.
        timer = QElapsedTimer()
        timer.start()
        while self.has_pending_work and timer.elapsed() < max(0, timeout_ms):
            QCoreApplication.processEvents()
            QThread.msleep(1)
        if self.has_pending_work or self.review_page.state_writer.has_failure():
            return False
        self._review_loader.pool.close()
        self.review_page.finish_close()
        self.persist_state()
        return True


__all__ = ["DesktopController"]
