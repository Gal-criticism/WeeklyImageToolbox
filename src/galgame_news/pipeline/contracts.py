"""Stable orchestration contracts shared by the CLI and desktop frontends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..domain import (
    FailureRecord,
    ImageCandidate,
    Issue,
    PipelineResult,
    SourceRef,
    VideoCandidate,
)


class CancellationRequested(RuntimeError):
    """Raised internally when cooperative task cancellation was requested."""


class CancellationToken:
    """Small thread-safe cancellation primitive.

    Pipeline stages check this token between network operations and after each
    news item.  It intentionally does not forcefully terminate a worker or an
    in-flight third-party request.
    """

    def __init__(self) -> None:
        self._event = Event()
        self._pause_condition = Condition()
        self._paused = False

    def cancel(self) -> None:
        # Wake a worker that is waiting in ``wait_if_paused``.  Cancellation
        # remains cooperative: an in-flight request is allowed to finish and
        # the runner observes this flag at its next safe point.
        self._event.set()
        with self._pause_condition:
            self._pause_condition.notify_all()

    def pause(self) -> None:
        """Pause at the next pipeline safe point without ending the worker."""

        with self._pause_condition:
            if not self._event.is_set():
                self._paused = True

    def resume(self) -> None:
        """Release a paused worker and let it continue from its current point."""

        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()

    @property
    def is_paused(self) -> bool:
        with self._pause_condition:
            return self._paused

    def wait_if_paused(self) -> None:
        """Block while paused, while still allowing cancellation to wake us."""

        with self._pause_condition:
            while self._paused and not self._event.is_set():
                self._pause_condition.wait()
        self.raise_if_cancelled()

    async def wait_if_paused_async(self) -> None:
        """Yield the loop while the existing thread-safe flag is paused."""
        while self.is_paused and not self.is_cancelled:
            await asyncio.sleep(0.05)
        self.raise_if_cancelled()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested("pipeline cancellation requested")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A serializable progress notification; no UI concerns are embedded."""

    kind: str
    task_id: str
    issue_id: str
    news_id: str | None = None
    news_index: int | None = None
    total_news: int | None = None
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def stage(self) -> str:
        """Compatibility alias for consumers that call the event stage."""
        return self.kind


class ProgressEventSink(Protocol):
    def __call__(self, event: ProgressEvent) -> None: ...


class Checkpoint(BaseModel):
    """Strict, versioned on-disk state for one pipeline task."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(default=1, frozen=True)
    status: Literal["running", "cancelled", "completed", "failed"]
    task_id: str = Field(min_length=1)
    issue_id: str = Field(min_length=1)
    input_sha256: str = Field(min_length=1)
    config_sha256: str = Field(min_length=1)
    issue: Issue
    completed_news_ids: list[str] = Field(default_factory=list)
    retryable_candidate_ids: list[str] = Field(default_factory=list)
    x_query_count: int = Field(default=0, ge=0)
    media_cache_version: Literal[0, 1] = 0
    candidates: list[ImageCandidate] = Field(default_factory=list)
    videos: list[VideoCandidate] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    source_map: dict[str, list[SourceRef]] = Field(default_factory=dict)
    result: PipelineResult | None = None


@dataclass(slots=True)
class TaskRequest:
    """Input for one pipeline run.

    Normal runner calls create an isolated ``raw`` directory beneath
    ``output_dir``.  ``legacy_output`` is used only by the compatibility
    ``Application.run`` wrapper so existing CLI output paths remain unchanged.
    """

    input_path: Path | str
    issue_id: str
    output_dir: Path | str
    offline: bool = False
    no_videos: bool = False
    use_socialdata_x: bool = False
    config_path: Path | str | None = None
    history_db: Path | str | None = None
    max_images: int | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    resume: bool = False
    task_dir: Path | str | None = None
    checkpoint_path: Path | str | None = None
    task_id: str | None = None
    selected_sections: list[Literal["x", "h", "z"]] = field(default_factory=lambda: ["x", "h", "z"])
    # Deprecated compatibility field: older serialized requests may still
    # supply it, but a request can no longer opt into deleting task internals.
    images_only: bool = False
    use_archive_materials: bool = False
    archive_range: Literal["year", "all"] = "year"
    use_image_supplement: bool = False
    supplement_target_images: int = 5
    supplement_sources: tuple[str, ...] = ("official", "store", "ymgal", "vndb")

    def __post_init__(self) -> None:
        if self.archive_range not in ("year", "all"):
            raise ValueError("往期周报范围仅支持最近一年或全部周报")
        if isinstance(self.supplement_target_images, bool) or not isinstance(self.supplement_target_images, int) or self.supplement_target_images <= 0:
            raise ValueError("补图目标数量必须是正整数")
        if self.offline and self.use_image_supplement:
            raise ValueError("离线模式不能启用联网自动补图")
        if self.use_image_supplement:
            allowed_sources = ("official", "store", "ymgal", "vndb")
            if not isinstance(self.supplement_sources, (list, tuple, set, frozenset)):
                raise ValueError("自动补图来源必须至少选择一项：官网、商店、Ymgal 或 VNDB")
            selected_sources = set(self.supplement_sources)
            if not selected_sources or selected_sources - set(allowed_sources):
                raise ValueError("自动补图来源必须至少选择一项：官网、商店、Ymgal 或 VNDB")
            self.supplement_sources = tuple(value for value in allowed_sources if value in selected_sources)
        if not isinstance(self.selected_sections, (list, tuple, set, frozenset)):
            raise ValueError("抓取栏目必须是新作、汉化、周边的选择列表")
        if not self.selected_sections:
            raise ValueError("请至少选择一个抓取栏目")
        if any(value not in {"x", "h", "z"} for value in self.selected_sections):
            raise ValueError("未知抓取栏目；仅支持新作、汉化、周边")
        self.selected_sections = [value for value in ("x", "h", "z") if value in self.selected_sections]
        self.input_path = Path(self.input_path)
        self.output_dir = Path(self.output_dir)
        if self.config_path is not None:
            self.config_path = Path(self.config_path)
        if self.history_db is not None:
            self.history_db = Path(self.history_db)
        if self.task_dir is not None:
            self.task_dir = Path(self.task_dir)
        if self.checkpoint_path is not None:
            self.checkpoint_path = Path(self.checkpoint_path)
        if not self.issue_id.strip():
            raise ValueError("issue_id must not be empty")
        # Keep accepting the legacy keyword so older callers and request files
        # remain readable, while ensuring it cannot trigger output cleanup.
        self.images_only = False


@dataclass(slots=True)
class TaskResult:
    """Result envelope returned by :class:`PipelineRunner`."""

    status: str
    task_id: str
    task_dir: Path
    output_dir: Path
    checkpoint_path: Path | None
    pipeline_result: PipelineResult
    resumed: bool = False
    images_only: bool = False

    @property
    def result(self) -> PipelineResult:
        """Short alias for callers that use ``result.result``."""
        return self.pipeline_result

    @property
    def issue(self):
        return self.pipeline_result.issue

    @property
    def candidates(self):
        return self.pipeline_result.candidates

    @property
    def failures(self):
        return self.pipeline_result.failures

    @property
    def videos(self):
        return self.pipeline_result.videos


EventSink = Callable[[ProgressEvent], None]
