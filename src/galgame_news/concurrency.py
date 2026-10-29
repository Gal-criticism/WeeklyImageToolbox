"""Small daemon-backed executor for work that may ignore cancellation."""

from concurrent.futures import Future
from queue import Empty, Queue
from threading import Lock, Thread


class DaemonTaskPool:
    """Executor-compatible pool whose abandoned workers cannot block process exit."""

    def __init__(self, *, max_workers: int, thread_name_prefix: str):
        self._queue: Queue = Queue()
        self._closed = False
        self._state_lock = Lock()
        self._threads = [
            Thread(target=self._worker, name=f"{thread_name_prefix}_{index}", daemon=True)
            for index in range(max(1, max_workers))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn, /, *args, **kwargs):
        with self._state_lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future = Future()
            self._queue.put((future, fn, args, kwargs))
        return future

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except Empty:
                if self._closed:
                    return
                continue
            if item is None:
                self._queue.task_done()
                return
            future, fn, args, kwargs = item
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                if cancel_futures:
                    while True:
                        try:
                            item = self._queue.get_nowait()
                        except Empty:
                            break
                        if item is not None:
                            item[0].cancel()
                        self._queue.task_done()
                for _ in self._threads:
                    self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()
