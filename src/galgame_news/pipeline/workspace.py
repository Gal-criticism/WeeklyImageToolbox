"""Task-directory and staging-path rules for the pipeline runner."""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath


MAX_ISSUE_LABEL_LENGTH = 64
UNSAFE_TASK_LABEL_CHARS = frozenset('/\\<>:"|?*')


@dataclass(frozen=True, slots=True)
class TaskPaths:
    """Resolved directories used by one pipeline task."""

    task_dir: Path
    raw_dir: Path
    checkpoint_path: Path
    task_id: str


def safe_task_label(issue_id: str) -> str:
    """Validate an issue id before using it as one directory component."""

    value = str(issue_id)
    label = value.strip()
    if not label or len(value) > MAX_ISSUE_LABEL_LENGTH:
        raise ValueError("unsafe issue_id")
    if ".." in value or any(char in UNSAFE_TASK_LABEL_CHARS for char in value):
        raise ValueError("unsafe issue_id")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError("unsafe issue_id")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("unsafe issue_id")
    if label.endswith("."):
        raise ValueError("unsafe issue_id")
    return label


def within(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def resolve_task_paths(request, *, legacy_output: bool = False) -> TaskPaths:
    """Resolve task paths while preserving new-task and resume confinement."""

    output_root = Path(request.output_dir).resolve()
    if legacy_output:
        task_dir = output_root
        return TaskPaths(
            task_dir=task_dir,
            raw_dir=task_dir,
            checkpoint_path=task_dir / "checkpoint.json",
            task_id=request.task_id or request.issue_id,
        )

    task_label = safe_task_label(request.issue_id)
    if request.resume:
        if request.task_dir is None:
            raise ValueError("resume requires task_dir")
        task_dir = Path(request.task_dir).resolve()
        if not within(task_dir, output_root):
            raise ValueError("task_dir escapes output root")
        raw_dir = task_dir / "raw"
        from ..tasks.repository import TaskRepository
        checkpoint = task_dir / ("task_state.sqlite3" if TaskRepository.is_authoritative(task_dir) else "checkpoint.json")
        if request.checkpoint_path is not None and Path(request.checkpoint_path).resolve() not in {checkpoint, task_dir / "checkpoint.json"}:
            raise ValueError("checkpoint_path must belong to this task")
        return TaskPaths(
            task_dir=task_dir,
            raw_dir=raw_dir,
            checkpoint_path=checkpoint,
            task_id=request.task_id or task_dir.name,
        )
    if request.task_dir is not None or request.checkpoint_path is not None:
        raise ValueError("arbitrary task paths are forbidden for new tasks")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    task_dir = output_root / f"{task_label}-{stamp}-{uuid.uuid4().hex[:8]}"
    return TaskPaths(
        task_dir=task_dir,
        raw_dir=task_dir / "raw",
        checkpoint_path=task_dir / "task_state.sqlite3",
        task_id=request.task_id or task_dir.name,
    )


def prepare_task_dir(task_dir: Path, request, *, legacy_output: bool = False) -> None:
    if request.resume:
        if not task_dir.is_dir():
            raise FileNotFoundError(f"task directory is missing: {task_dir}")
        return
    if legacy_output:
        task_dir.parent.mkdir(parents=True, exist_ok=True)
        task_dir.mkdir(parents=True, exist_ok=True)
        return
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(exist_ok=False)
    (task_dir / ".work").mkdir()


def image_stage(task_dir: Path) -> Path:
    stage = Path(task_dir) / ".work" / "images"
    stage.mkdir(parents=True, exist_ok=True)
    return stage

def video_stage(task_dir: Path) -> Path:
    stage = Path(task_dir) / ".work" / "videos"
    stage.mkdir(parents=True, exist_ok=True)
    return stage
