"""Bounded background image decoding. QPixmap creation belongs to the UI."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, Qt, Signal, Slot, QTimer
from .review_jobs import _retire
from PySide6.QtGui import QImage, QImageReader


@dataclass(frozen=True)
class ImageRequest:
    key: str
    path: Path
    size: QSize
    preview: bool = False


class _Result(QObject):
    done = Signal(int, object, object, str)


class _Decode(QRunnable):
    def __init__(self, generation: int, request: ImageRequest):
        super().__init__()
        self.generation, self.request = generation, request
        self.setAutoDelete(False)
        self.signals = _Result()

    def run(self) -> None:
        image, error = QImage(), ""
        try:
            reader = QImageReader(str(self.request.path))
            reader.setAutoTransform(True)
            original = reader.size()
            if original.isValid() and (original.width() > self.request.size.width() or original.height() > self.request.size.height()):
                reader.setScaledSize(original.scaled(self.request.size, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if image.isNull():
                error = reader.errorString()
            elif image.width() > self.request.size.width() or image.height() > self.request.size.height():
                image = image.scaled(self.request.size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.signals.done.emit(self.generation, self.request, image, error)


class AsyncImages(QObject):
    """At most two running decodes and a replaceable visible-range queue.

    Old work may finish, but is never delivered after ``invalidate``. Only
    runnable work is sent to QThreadPool, so it has no unbounded hidden queue.
    """

    ready = Signal(int, object, object, str)

    def __init__(self, parent: QObject | None = None, max_pending: int = 48):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.max_pending = max_pending
        self.generation = 0
        self._pending: OrderedDict[str, ImageRequest] = OrderedDict()
        self._running: dict[tuple[int, str], _Decode] = {}
        self._retired = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def running_count(self) -> int:
        return max(len(self._running), self.pool.activeThreadCount())

    def invalidate(self) -> int:
        self.generation += 1
        self._pending.clear()
        return self.generation

    def request_preview(self, request: ImageRequest) -> None:
        self._pending = OrderedDict((key, value) for key, value in self._pending.items() if not value.preview)
        if (self.generation, request.key) not in self._running:
            self._pending[request.key] = request
            self._pending.move_to_end(request.key, last=False)
        while len(self._pending) > self.max_pending:
            self._pending.popitem()
        self._pump()

    def request_visible(self, requests: list[ImageRequest]) -> None:
        pending = OrderedDict((key, value) for key, value in self._pending.items() if value.preview)
        for request in requests:
            if len(pending) >= self.max_pending:
                break
            if (self.generation, request.key) not in self._running:
                pending[request.key] = request
        self._pending = pending
        self._pump()

    def _pump(self) -> None:
        while self._pending and len(self._running) < 2:
            _, request = self._pending.popitem(last=False)
            job = _Decode(self.generation, request)
            job.signals.setParent(self)
            self._running[(self.generation, request.key)] = job
            job.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
            self.pool.start(job)

    @Slot(int, object, object, str)
    def _done(self, generation: int, request: ImageRequest, image: QImage, error: str) -> None:
        job = self._running.pop((generation, request.key), None)
        if job is not None:
            self._retired.append(job)
        if generation == self.generation:
            self.ready.emit(generation, request, image, error)
        self._pump()
        QTimer.singleShot(0, self, lambda: _retire(self))


__all__ = ["AsyncImages", "ImageRequest"]
