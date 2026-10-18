"""Run the existing final export away from the GUI thread."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot, QTimer
from .review_jobs import _retire
from ..diagnostics import diagnose_error
import json


class _Signals(QObject):
    done = Signal(object, object, str)


class _Export(QRunnable):
    def __init__(self, session, *, include_pending=True):
        super().__init__()
        self.session = session
        self.snapshot = session.query_snapshot()
        self.include_pending = include_pending
        self.setAutoDelete(False)
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.session, self.snapshot.export_final(include_pending=self.include_pending), "")
        except Exception as exc:
            self.signals.done.emit(self.session, None, json.dumps(diagnose_error(exc, stage="导出图片"), ensure_ascii=False))


class AsyncExport(QObject):
    finished = Signal(object, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.session = None
        self._job = None
        self._retired = []

    @property
    def busy(self):
        return self.session is not None or bool(self.pool.activeThreadCount())

    def start(self, session, *, include_pending=True):
        if self.busy:
            return False
        self.session = session
        self._job = _Export(session, include_pending=include_pending)
        self._job.signals.setParent(self)
        self._job.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self.pool.start(self._job)
        return True

    @Slot(object, object, str)
    def _done(self, session, manifest, error):
        if self._job is not None:
            self._retired.append(self._job)
        self._job = None
        self.session = None
        self.finished.emit(session, manifest, error)
        QTimer.singleShot(0, self, lambda: _retire(self))


__all__ = ["AsyncExport"]
