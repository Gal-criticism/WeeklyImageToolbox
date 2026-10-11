"""Bounded review queries and a single, ordered atomic state writer.

Only immutable snapshots cross this boundary. Live sessions and widgets remain
owned by the GUI thread. Queued work is coalesced instead of growing unbounded.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from weakref import WeakKeyDictionary
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal, Slot, QTimer

from ..review import ReviewSession


class _Job:
    def __init__(self, identity, function):
        self.identity, self.function = identity, function
        self.future = None


class _SnapshotPool:
    """One Python worker; completed results are consumed only by the GUI timer.

    No QObject or native runnable ownership crosses the thread boundary.
    The coordinator keeps at most one running and one coalesced request.
    """
    def __init__(self, owner):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-snapshot")
        self.future = None
        executor = self.executor
        owner.destroyed.connect(lambda *_: executor.shutdown(wait=False, cancel_futures=True))

    def activeThreadCount(self):
        return int(self.future is not None and not self.future.done())

    def start(self, job):
        job.future = self.executor.submit(job.function)
        self.future = job.future

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


def _poll(owner):
    job = owner._job
    if job is None:
        owner._poll_timer.stop()
        return
    if not job.future.done():
        return
    try:
        result, error = job.future.result(), None
    except BaseException as exc:
        result, error = None, exc
    owner._done(job.identity, result, error)


def _retire(owner):
    # A queued result can arrive before QRunnable.run has returned to Qt.
    # Keep its C++ wrapper alive until the pool acknowledges completion.
    if owner.pool.activeThreadCount():
        QTimer.singleShot(10, owner, lambda: _retire(owner))
        return
    for job in owner._retired:
        job.signals.deleteLater()
    owner._retired.clear()
    if hasattr(owner, "idle") and not owner.busy:
        owner.idle.emit()


class ReviewStateWriter(QObject):
    saved = Signal(object, int)
    failed = Signal(object, int, object)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = _SnapshotPool(self)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(5)
        self._poll_timer.timeout.connect(lambda: _poll(self))
        self._pending = OrderedDict()
        self._job = None
        self._failed = {}
        self._saved = WeakKeyDictionary()
        self._retired = []

    @property
    def busy(self):
        return self._job is not None or bool(self._pending) or bool(self.pool.activeThreadCount())

    def is_saved(self, session):
        return self._saved.get(session, -1) >= session.revision

    def has_failure(self, session=None):
        return bool(self._failed) if session is None else id(session) in self._failed

    def enqueue(self, session):
        # Payload construction uses the loaded state, with no GUI disk access.
        version = session.revision
        payload = session.decision_state_payload()
        self._pending[id(session)] = (session, version, Path(session.state_path), payload)
        self._failed.pop(id(session), None)
        self._dispatch()

    def retry(self, session):
        self.enqueue(session)

    def _dispatch(self):
        if self._job is not None or not self._pending:
            return
        _, (session, version, path, payload) = self._pending.popitem(last=False)
        identity = (session, version)
        self._job = _Job(identity, lambda: ReviewSession.persist_state_payload(path, payload))
        self.pool.start(self._job)
        self._poll_timer.start()

    @Slot(object, object, object)
    def _done(self, identity, _result, error):
        session, version = identity
        self._job = None
        if error is None:
            self._saved[session] = max(version, self._saved.get(session, -1))
            self._failed.pop(id(session), None)
            self.saved.emit(session, version)
        else:
            pending = self._pending.get(id(session))
            if pending is None or pending[1] <= version:
                self._failed[id(session)] = (session, version, error)
                self.failed.emit(session, version, error)
        self._dispatch()
        if not self.busy:
            self.idle.emit()
        if self._job is None:
            self._poll_timer.stop()


class ReviewQuery(QObject):
    ready = Signal(object, object, object)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = _SnapshotPool(self)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(5)
        self._poll_timer.timeout.connect(lambda: _poll(self))
        self._job = None
        self._pending = None
        self._retired = []

    @property
    def busy(self):
        return self._job is not None or self._pending is not None or bool(self.pool.activeThreadCount())

    def request(self, identity, function):
        self._pending = (identity, function)
        self._dispatch()

    def invalidate(self):
        self._pending = None

    def _dispatch(self):
        if self._job is not None or self._pending is None:
            return
        identity, function = self._pending
        self._pending = None
        self._job = _Job(identity, function)
        self.pool.start(self._job)
        self._poll_timer.start()

    @Slot(object, object, object)
    def _done(self, identity, result, error):
        self._job = None
        self.ready.emit(identity, result, error)
        self._dispatch()
        if not self.busy:
            self.idle.emit()
        if self._job is None:
            self._poll_timer.stop()
