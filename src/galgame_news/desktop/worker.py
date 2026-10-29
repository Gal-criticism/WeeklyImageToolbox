"""Qt worker boundary for one cooperative pipeline run."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
import inspect

from PySide6.QtCore import QObject, Signal, Slot

from ..pipeline import CancellationRequested, CancellationToken, ProgressEvent


class PipelineWorker(QObject):
    """Run ``PipelineRunner`` on a ``QThread`` without parsing stdout."""

    event = Signal(object)
    progress = Signal(object)
    finished = Signal(object)
    result = Signal(object)
    cancelled = Signal(object)
    failed = Signal(object)
    error = Signal(object)

    def __init__(self, runner: Any, request: Any, token: CancellationToken | None = None):
        super().__init__()
        self.runner = runner
        self.request = request
        self.token = token or CancellationToken()

    def cancel(self) -> None:
        """Request cancellation at the next pipeline checkpoint."""

        self.token.cancel()

    def pause(self) -> None:
        """Pause the pipeline at its next cooperative safe point."""

        self.token.pause()

    def resume(self) -> None:
        """Resume a pipeline paused at a cooperative safe point."""

        self.token.resume()

    def _emit_event(self, value: ProgressEvent) -> None:
        if not isinstance(value, ProgressEvent):
            return
        self.event.emit(value)
        self.progress.emit(value)

    @Slot()
    def run(self) -> None:
        try:
            value = self.runner.run(
                self.request,
                event_sink=self._emit_event,
                cancellation_token=self.token,
            )
        except CancellationRequested as exc:
            self.cancelled.emit(exc)
        except BaseException as exc:  # worker boundary must report all failures
            self.failed.emit(exc)
            self.error.emit(exc)
        else:
            status = str(getattr(value, "status", "")).casefold()
            if self.token.is_cancelled or status == "cancelled":
                self.cancelled.emit(value)
            elif status == "failed":
                self.failed.emit(value)
            else:
                self.finished.emit(value)
                self.result.emit(value)


TaskWorker = PipelineWorker


class SupplementWorker(QObject):
    """Image-only review addition; the GUI coordinator owns persistence."""

    event = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, runner, kwargs, token):
        super().__init__()
        self.runner, self.kwargs, self.token = runner, kwargs, token

    @Slot()
    def run(self):
        try:
            value = self.runner.supplement_news(**self.kwargs, event_sink=self.event.emit,
                                                cancellation_token=self.token)
        except BaseException as exc:
            self.failed.emit(exc)
        else:
            self.finished.emit(value)


class ReviewGrabWorker(QObject):
    """Collect and prepare assets off-thread, returning a delta, never a session."""

    event = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, runner, kind, kwargs, token, snapshot, identity):
        super().__init__()
        self.runner, self.kind, self.kwargs, self.token = runner, kind, kwargs, token
        self.snapshot, self.identity = snapshot, identity

    @Slot()
    def run(self):
        try:
            method = getattr(self.runner, "supplement_news" if self.kind == "supplement" else "retry_news")
            kwargs = dict(self.kwargs, event_sink=self.event.emit, cancellation_token=self.token)
            # Keep existing synchronous injected runners usable in this worker.
            signature = inspect.signature(method)
            if not any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
            result = method(**kwargs)
            attempt_id = getattr(result, "attempt_id", None)
            if isinstance(result, dict):
                attempt_id = result.get("attempt_id") or result.get("id")
            if attempt_id is None:
                from pathlib import Path
                attempt_id = Path(str(result)).name
            self.event.emit(SimpleNamespace(message="正在校验图片与准备合并…"))
            delta = self.snapshot.prepare_retry_delta(self.kwargs["news_id"], str(attempt_id))
            self.finished.emit((self.identity, result, delta))
        except BaseException as exc:
            self.failed.emit((self.identity, exc))


class SupplementMissingWorker(QObject):
    """Run automatic multi-source supplement without touching review widgets."""
    event = Signal(object)
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, runner, kwargs, token, identity):
        super().__init__()
        self.runner, self.kwargs, self.token, self.identity = runner, kwargs, token, identity

    @Slot()
    def run(self):
        try:
            from ..supplement.service import supplement_missing
            values = supplement_missing(self.runner, **self.kwargs, event_sink=self.event.emit,
                                        cancellation_token=self.token)
            if hasattr(values, "__await__"):
                import asyncio
                values = asyncio.run(values)
        except BaseException as exc:
            self.failed.emit((self.identity, exc))
        else:
            self.finished.emit((self.identity, values))

__all__ = ["PipelineWorker", "TaskWorker", "SupplementWorker", "ReviewGrabWorker", "SupplementMissingWorker"]
