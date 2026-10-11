"""Durable task catalog and non-destructive task-directory helpers.

The desktop toolbox keeps its catalog separate from pipeline output.  A task
directory is therefore safe to remove from the catalog without removing any
user media, and imported legacy output is only ever read by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..delivery.asset_paths import resolve_review_index_root


_TASK_SCHEMA_VERSION = 1
_SAFE_COMPONENT = re.compile(r"[^0-9A-Za-z._-]+")


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value.strip()).strip("._")
    return cleaned or fallback


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return default
    return value


@dataclass(frozen=True)
class TaskRecord:
    """A catalog row for one task.

    Paths are serialized as strings deliberately: this keeps the record stable
    across process boundaries and mirrors the data consumed by the desktop UI.
    """

    task_id: str
    issue_id: str
    task_root: str
    input_path: str | None = None
    source_output: str | None = None
    input_sha256: str | None = None
    state_path: str | None = None
    imported: bool = False
    created_at: str = ""
    active: bool = True

    @property
    def root_path(self) -> Path:
        return Path(self.task_root)

    @property
    def id(self) -> str:
        return self.task_id

    @property
    def path(self) -> Path:
        return self.root_path

    @property
    def review_state_path(self) -> Path:
        return self.root_path / "review_state.json"

    @property
    def source_output_path(self) -> Path | None:
        return Path(self.source_output) if self.source_output else None

    @property
    def images_only(self) -> bool:
        root = self.root_path
        return (not self.imported and (root / "final" / "images").is_dir()
                and not (root / "raw" / "image_index.json").is_file()
                and not (root / "checkpoint.json").is_file())


@dataclass
class RetryAttempt:
    """Raw retry data read from one attempt directory.

    This object intentionally contains plain dictionaries.  It is a data
    seam between retry storage and review code and never calls PipelineRunner.
    """

    task_id: str
    news_id: str
    attempt_id: str
    path: Path
    images: list[dict[str, Any]]
    videos: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    kind: str = "retry"

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return self.images


class TaskStore:
    """Catalog one active task and its historical task records.

    ``app_data`` is injectable for tests and portable installations.  Without
    it, Windows uses ``%LOCALAPPDATA%/GalgameNewsToolbox`` as the persistent
    root.
    """

    def __init__(self, app_data: Path | str | None = None, *, recover_on_startup: bool = True):
        if app_data is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            app_data = base / "GalgameNewsToolbox"
        self.root = Path(app_data).expanduser()
        self.app_data = self.root
        self.tasks_root = self.root / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.root / "task_catalog.sqlite3"
        self._connection = sqlite3.connect(self.catalog_path, timeout=0.1)
        self._connection.row_factory = sqlite3.Row
        self._initialize_catalog()
        self.retry_diagnostics: list[dict[str, Any]] = []
        # Run only at startup, never during an active worker's directory switch.
        if recover_on_startup:
            from ..pipeline.compact_output import recover_interrupted_compaction
            for record in self.list_tasks():
                if not record.imported:
                    try:
                        recover_interrupted_compaction(record.root_path, task_id=record.task_id)
                    except (OSError, ValueError):
                        pass  # Leave all data untouched for a later recovery.

    def _initialize_catalog(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL,
                task_root TEXT NOT NULL,
                input_path TEXT,
                source_output TEXT,
                input_sha256 TEXT,
                state_path TEXT,
                imported INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_active_created
                ON tasks(active, created_at DESC);
            """
        )
        self._connection.commit()

    @staticmethod
    def _materialize_task_root(task_root: Path) -> None:
        for name in ("raw", "retries", "final", "logs", ".work"):
            (task_root / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _initialize_task_state(path: Path, record: TaskRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=0.1)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retry_attempts (
                    news_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(news_id, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS review_decisions (
                    media_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            values = {
                "schema_version": str(_TASK_SCHEMA_VERSION),
                "task_id": record.task_id,
                "issue_id": record.issue_id,
                "imported": "1" if record.imported else "0",
            }
            connection.executemany(
                "INSERT INTO task_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values.items(),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _ensure_task_state_schema(path: Path) -> None:
        """Upgrade older managed task state files before retry access."""
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=0.1)
        try:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS task_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retry_attempts (
                    news_id TEXT NOT NULL, attempt_id TEXT NOT NULL, path TEXT NOT NULL,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'retry',
                    PRIMARY KEY(news_id, attempt_id));
                CREATE TABLE IF NOT EXISTS review_decisions (
                    media_id TEXT PRIMARY KEY, decision TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(retry_attempts)")}
            if "kind" not in columns:
                connection.execute("ALTER TABLE retry_attempts ADD COLUMN kind TEXT NOT NULL DEFAULT 'retry'")
            connection.commit()
        finally:
            connection.close()

    def _default_task_root(self, issue_id: str, task_id: str) -> Path:
        label = _safe_component(issue_id, "task")
        return self.tasks_root / f"{label}-{task_id}"

    def _record(
        self,
        *,
        task_id: str | None = None,
        issue_id: str,
        input_path: Path | None,
        source_output: Path | None,
        task_root: Path,
        imported: bool,
    ) -> TaskRecord:
        task_id = task_id or uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        state_path = task_root / "task_state.sqlite3"
        record = TaskRecord(
            task_id=task_id,
            issue_id=str(issue_id),
            task_root=str(task_root),
            input_path=str(input_path) if input_path else None,
            source_output=str(source_output) if source_output else None,
            input_sha256=_sha256(input_path) if input_path else None,
            state_path=str(state_path),
            imported=imported,
            created_at=created_at,
            active=True,
        )
        self._materialize_task_root(task_root)
        self._initialize_task_state(state_path, record)
        self._connection.execute("UPDATE tasks SET active = 0 WHERE active = 1")
        self._connection.execute(
            """INSERT INTO tasks(
                task_id, issue_id, task_root, input_path, source_output,
                input_sha256, state_path, imported, created_at, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                record.task_id,
                record.issue_id,
                record.task_root,
                record.input_path,
                record.source_output,
                record.input_sha256,
                record.state_path,
                int(record.imported),
                record.created_at,
            ),
        )
        self._connection.commit()
        return record

    def create_task(self, issue_id: str, input_path: Path | str, *, task_root: Path | str | None = None) -> TaskRecord:
        """Create and catalog a managed task directory."""

        source = Path(input_path).expanduser()
        task_id = uuid.uuid4().hex
        root = Path(task_root).expanduser() if task_root is not None else self._default_task_root(issue_id, task_id)
        # ``_record`` creates its own catalog id; reserve the generated root
        # with a fresh id only when no caller-supplied root was given.
        return self._record(
            task_id=task_id,
            issue_id=issue_id,
            input_path=source,
            source_output=None,
            task_root=root,
            imported=False,
        )

    def import_output(
        self,
        output_dir: Path | str,
        *,
        issue_id: str | None = None,
        task_root: Path | str | None = None,
    ) -> TaskRecord:
        """Catalog an existing output directory without writing into it."""

        source = Path(output_dir).expanduser()
        if not source.is_dir():
            raise FileNotFoundError(source)
        source = resolve_review_index_root(source)
        inferred_issue = issue_id or self._infer_issue_id(source) or source.name or "imported"
        task_id = uuid.uuid4().hex
        if task_root is not None:
            root = Path(task_root).expanduser()
            source_resolved = source.resolve()
            root_resolved = root.resolve()
            try:
                root_resolved.relative_to(source_resolved)
            except ValueError:
                pass
            else:
                raise ValueError("task_root must be outside legacy source output")
        else:
            root = self._default_task_root(inferred_issue, task_id)
        return self._record(
            task_id=task_id,
            issue_id=inferred_issue,
            input_path=None,
            source_output=source.resolve(),
            task_root=root,
            imported=True,
        )

    @staticmethod
    def _infer_issue_id(source: Path) -> str | None:
        for filename in ("image_index.json", "video_index.json"):
            payload = _read_json(source / filename, {})
            if isinstance(payload, dict) and payload.get("issue_id"):
                return str(payload["issue_id"])
        return None

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> TaskRecord | None:
        if row is None:
            return None
        return TaskRecord(
            task_id=row["task_id"],
            issue_id=row["issue_id"],
            task_root=row["task_root"],
            input_path=row["input_path"],
            source_output=row["source_output"],
            input_sha256=row["input_sha256"],
            state_path=row["state_path"],
            imported=bool(row["imported"]),
            created_at=row["created_at"],
            active=bool(row["active"]),
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        return self._row_to_record(row)

    def list_tasks(self) -> list[TaskRecord]:
        rows = self._connection.execute("SELECT * FROM tasks ORDER BY active DESC, created_at DESC, task_id DESC").fetchall()
        return [record for row in rows if (record := self._row_to_record(row)) is not None]

    @property
    def tasks(self) -> list[TaskRecord]:
        return self.list_tasks()

    @property
    def active_task(self) -> TaskRecord | None:
        row = self._connection.execute("SELECT * FROM tasks WHERE active = 1 ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._row_to_record(row)

    @property
    def active(self) -> TaskRecord | None:
        return self.active_task

    def list_history(self) -> list[TaskRecord]:
        rows = self._connection.execute("SELECT * FROM tasks WHERE active = 0 ORDER BY created_at DESC, task_id DESC").fetchall()
        return [record for row in rows if (record := self._row_to_record(row)) is not None]

    def history(self) -> list[TaskRecord]:
        return self.list_history()

    def set_active(self, task_id: str) -> TaskRecord | None:
        if self.get_task(task_id) is None:
            return None
        self._connection.execute("UPDATE tasks SET active = 0")
        self._connection.execute("UPDATE tasks SET active = 1 WHERE task_id = ?", (str(task_id),))
        self._connection.commit()
        return self.get_task(task_id)

    def update_task_root(self, task: TaskRecord | str, task_root: Path | str, *, materialize: bool = True) -> TaskRecord | None:
        """Bind a catalog row to the pipeline's per-run directory.

        A desktop task is catalogued before the worker starts so it is visible
        while running.  The pipeline chooses the final timestamped directory;
        this method records that directory once the worker returns without
        touching any pipeline output files.
        """

        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            return None
        root = Path(task_root).expanduser()
        if not root.is_absolute():
            root = root.resolve()
        if root.resolve() == record.root_path.resolve():
            return record
        bound = TaskRecord(
            task_id=record.task_id,
            issue_id=record.issue_id,
            task_root=str(root),
            input_path=record.input_path,
            source_output=record.source_output,
            input_sha256=record.input_sha256,
            state_path=str(root / "task_state.sqlite3"),
            imported=record.imported,
            created_at=record.created_at,
            active=record.active,
        )
        if materialize:
            self._materialize_task_root(root)
            self._initialize_task_state(Path(bound.state_path), bound)
        self._connection.execute(
            "UPDATE tasks SET task_root = ?, state_path = ? WHERE task_id = ?",
            (bound.task_root, bound.state_path, bound.task_id),
        )
        self._connection.commit()
        return self.get_task(bound.task_id)

    def save_request_options(self, task: TaskRecord | str, options: dict[str, Any]) -> None:
        """Persist the non-secret run options needed for an exact resume."""

        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            raise ValueError("unknown task")
        path = record.root_path / "request_options.json"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(options, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def load_request_options(self, task: TaskRecord | str) -> dict[str, Any]:
        """Return saved non-secret run options, or an empty mapping."""

        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            return {}
        value = _read_json(record.root_path / "request_options.json", {})
        return value if isinstance(value, dict) else {}

    def remove_from_history(self, task_id: str) -> bool:
        """Forget a catalog row while leaving its task directory untouched."""

        cursor = self._connection.execute("DELETE FROM tasks WHERE task_id = ?", (str(task_id),))
        self._connection.commit()
        return cursor.rowcount > 0

    def remove_task(self, task_id: str) -> bool:
        return self.remove_from_history(task_id)

    def remove(self, task_id: str) -> bool:
        return self.remove_from_history(task_id)

    @staticmethod
    def _confined(path: Path, root: Path) -> Path:
        resolved_root = root.resolve()
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("retry path escapes task retries directory") from exc
        return resolved_path

    @staticmethod
    def _valid_retry_component(value: str, label: str) -> str:
        value = str(value)
        if not value or value in {".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
            raise ValueError(f"invalid retry {label}")
        return value

    def register_retry_attempt(self, task: TaskRecord | str, news_id: str, attempt_id: str,
                               kind: str = "retry", path: Path | str | None = None) -> Path:
        """Register a completed batch using only SQLite and confined path checks.

        Intentionally does not inspect or hash manifests; safe to call on the GUI thread.
        """
        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            raise KeyError(f"unknown task: {task}")
        news_id = self._valid_retry_component(news_id, "news id")
        attempt_id = self._valid_retry_component(attempt_id, "attempt id")
        retries = Path(record.task_root) / "retries"
        attempt = self._confined(Path(path) if path is not None else retries / news_id / attempt_id, retries)
        state_path = Path(record.state_path or Path(record.task_root) / "task_state.sqlite3")
        self._ensure_task_state_schema(state_path)
        with sqlite3.connect(state_path, timeout=0.1) as connection:
            connection.execute(
                "INSERT INTO retry_attempts(news_id, attempt_id, path, created_at, kind) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(news_id, attempt_id) DO UPDATE SET path=excluded.path, kind=excluded.kind",
                (news_id, attempt_id, str(attempt), datetime.now(timezone.utc).isoformat(), str(kind or "retry")),
            )
        return attempt

    def merge_retry_attempt(self, task: TaskRecord | str, news_id: str, attempt_id: str) -> RetryAttempt:
        """Read one retry attempt as data without mutating raw output.

        The caller can hand the returned plain records to ``ReviewSession``;
        this seam intentionally has no PipelineRunner dependency.
        """

        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            raise KeyError(f"unknown task: {task}")
        state_path = Path(record.state_path or Path(record.task_root) / "task_state.sqlite3")
        self._ensure_task_state_schema(state_path)
        retries = Path(record.task_root) / "retries"
        attempt = self._confined(retries / self._valid_retry_component(news_id, "news id") / self._valid_retry_component(attempt_id, "attempt id"), retries)
        if not attempt.is_dir():
            raise FileNotFoundError(attempt)
        image_payload = _read_json(attempt / "image_index.json", {})
        video_payload = _read_json(attempt / "video_index.json", {})
        images = self._items(image_payload, ("candidates", "images", "image_candidates"))
        videos = self._items(video_payload, ("videos", "video_candidates", "candidates"))
        failures = self._items(image_payload, ("failures",)) + self._items(video_payload, ("failures",))
        manifest = image_payload if isinstance(image_payload, dict) else {}
        kind = str(manifest.get("kind") or manifest.get("attempt_kind") or "retry")
        retry = RetryAttempt(
            task_id=record.task_id,
            news_id=str(news_id),
            attempt_id=str(attempt_id),
            path=attempt,
            images=images,
            videos=videos,
            failures=failures,
            kind=kind,
        )
        self.register_retry_attempt(record, retry.news_id, retry.attempt_id, kind, attempt)
        return retry

    def list_retry_attempts(self, task: TaskRecord | str) -> list[RetryAttempt]:
        """Reload every registered retry batch in stable creation order."""
        from ..diagnostics import diagnose_error
        self.retry_diagnostics = []
        record = task if isinstance(task, TaskRecord) else self.get_task(str(task))
        if record is None:
            raise KeyError(f"unknown task: {task}")
        state_path = Path(record.state_path or Path(record.task_root) / "task_state.sqlite3")
        self._ensure_task_state_schema(state_path)
        # A run can be interrupted after publishing its manifest but before the
        # GUI registers the completed batch. Recover only valid supplement
        # manifests directly under this task's confined retries directory.
        retries_root = (Path(record.task_root) / "retries").resolve()
        if retries_root.is_dir():
            for index_path in sorted(retries_root.glob("*/*/image_index.json")):
                try:
                    attempt = self._confined(index_path.parent, retries_root)
                    directory_news_id = self._valid_retry_component(attempt.parent.name, "news id")
                    attempt_id = self._valid_retry_component(attempt.name, "attempt id")
                    with sqlite3.connect(state_path, timeout=0.1) as connection:
                        registered = connection.execute("SELECT 1 FROM retry_attempts WHERE news_id=? AND attempt_id=?", (directory_news_id, attempt_id)).fetchone()
                    if registered is not None:
                        continue
                    payload = self._load_retry_manifest(index_path)
                    kind = str(payload.get("kind") or payload.get("attempt_kind") or "")
                    if kind not in {"supplement", "retry"}:
                        continue
                    if not self._valid_supplement(payload, directory_news_id, attempt):
                        self.retry_diagnostics.append(diagnose_error(ValueError("invalid retry batch"), "retry_list", source=str(index_path), candidate_id=attempt_id))
                        continue
                    self.register_retry_attempt(record, directory_news_id, attempt_id, kind, attempt)
                except Exception as error:
                    self.retry_diagnostics.append(diagnose_error(error, "retry_list", source=str(index_path), candidate_id=index_path.parent.name))
        with sqlite3.connect(state_path, timeout=0.1) as connection:
            rows = connection.execute(
                "SELECT news_id, attempt_id, path, kind FROM retry_attempts ORDER BY created_at, attempt_id"
            ).fetchall()
        result = []
        for news_id, attempt_id, raw_path, kind in rows:
            try:
                news_id = self._valid_retry_component(news_id, "news id")
                attempt_id = self._valid_retry_component(attempt_id, "attempt id")
                attempt = self._confined(Path(raw_path), retries_root)
                if attempt.parent.name != news_id or attempt.name != attempt_id:
                    raise ValueError("retry batch path does not match its catalog ids")
                if not attempt.is_dir():
                    raise FileNotFoundError(attempt)
                image_payload = self._load_retry_manifest(attempt / "image_index.json")
                video_path = attempt / "video_index.json"
                video_payload = self._load_retry_manifest(video_path, optional=True)
                self._validate_manifest(image_payload, "image")
                self._validate_manifest(video_payload, "video")
                result.append(RetryAttempt(
                    record.task_id, str(news_id), str(attempt_id), attempt,
                    self._validated_retry_items(image_payload, ("candidates", "images", "image_candidates"), news_id, attempt),
                    self._validated_retry_items(video_payload, ("videos", "video_candidates", "candidates"), news_id, attempt),
                    self._items(image_payload, ("failures",)) + self._items(video_payload, ("failures",)),
                    str(kind or "retry"),
                ))
            except Exception as error:
                self.retry_diagnostics.append(diagnose_error(error, "retry_list", source=raw_path, candidate_id=str(attempt_id)))
        return result

    @staticmethod
    def _load_retry_manifest(path: Path, optional: bool = False) -> dict[str, Any]:
        if optional and not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("retry manifest must be a JSON object")
        return payload

    @staticmethod
    def _validate_manifest(payload: dict[str, Any], kind: str) -> None:
        version = payload.get("schema_version", payload.get("schemaVersion"))
        if version is not None and version not in (1, "1"):
            raise ValueError(f"unsupported retry {kind} manifest schema")
        for key in (("candidates", "images", "image_candidates", "failures") if kind == "image" else ("videos", "video_candidates", "candidates", "failures")):
            if key in payload and not isinstance(payload[key], list):
                raise ValueError(f"retry manifest {key} must be a list")

    def _validated_retry_items(self, payload, keys, news_id, attempt):
        from ..diagnostics import diagnose_error
        rows = next((payload[key] for key in keys if isinstance(payload.get(key), list)), [])
        result = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError("候选记录必须为 JSON 对象")
                if str(row.get("news_id") or row.get("newsId") or "") != str(news_id):
                    raise ValueError("候选记录所属新闻与批次不一致")
                for key in ("local_path", "original_path", "output_path"):
                    value = row.get(key)
                    if value:
                        path = Path(value)
                        self._confined(path if path.is_absolute() else attempt / path, attempt.parents[2])
                result.append(row)
            except (OSError, TypeError, ValueError) as exc:
                self.retry_diagnostics.append(diagnose_error(exc, stage="恢复候选", source=str(attempt),
                    candidate_id=str(row.get("id", "")) if isinstance(row, dict) else None))
        return result

    @classmethod
    def _valid_supplement(cls, payload: dict[str, Any], news_id: str, attempt: Path) -> bool:
        cls._validate_manifest(payload, "image")
        rows = payload.get("news_items")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or str(rows[0].get("news_id", rows[0].get("newsId", ""))) != news_id:
            return False
        image_candidates = cls._items(payload, ("candidates", "images", "image_candidates"))
        video_payload = cls._load_retry_manifest(attempt / "video_index.json", optional=True)
        cls._validate_manifest(video_payload, "video")
        candidates = image_candidates + cls._items(video_payload, ("videos", "video_candidates", "candidates"))
        # A batch containing only foreign news cannot be recovered as this
        # news. Mixed batches retain valid rows and isolate foreign records.
        return not candidates or any(str(item.get("news_id") or item.get("newsId") or "") == news_id for item in candidates)

    def read_retry_attempt(self, task: TaskRecord | str, news_id: str, attempt_id: str) -> RetryAttempt:
        return self.merge_retry_attempt(task, news_id, attempt_id)

    @staticmethod
    def _items(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
