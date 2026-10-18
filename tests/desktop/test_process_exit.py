"""Real process tests keep forced termination outside the pytest process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from galgame_news.desktop.process_exit import DesktopProcessExit, _WindowsProcesses


class _FakeWindows:
    def __init__(self, *, init_fails=False, job_succeeds=True, cleanup_fails=False):
        self.init_fails = init_fails
        self.job_succeeds = job_succeeds
        self.cleanup_fails = cleanup_fails
        self.calls = []

    def create_job(self):
        if self.init_fails:
            raise OSError("job unavailable")
        return 123

    def terminate_job(self, job, code):
        self.calls.append(("job", job, code))
        return self.job_succeeds

    def terminate_descendants(self, pid, code):
        self.calls.append(("tree", pid, code))
        if self.cleanup_fails:
            raise OSError("cleanup unavailable")


@pytest.mark.parametrize("init_fails,job_succeeds", [(False, True), (False, False), (True, False)])
def test_exit_uses_owned_job_or_current_process_tree_once(init_fails, job_succeeds):
    api = _FakeWindows(init_fails=init_fails, job_succeeds=job_succeeds)
    exited = []
    owner = DesktopProcessExit(exit_function=exited.append, windows_api=api)
    owner.force_exit(7)
    owner.force_exit(9)
    assert exited == [7]
    if init_fails:
        assert owner.initialization_error == "OSError"
    if init_fails or not job_succeeds:
        assert api.calls[-1] == ("tree", os.getpid(), 7)
    else:
        assert api.calls == [("job", 123, 7)]


def test_cleanup_failure_still_ends_main_process():
    exited = []
    owner = DesktopProcessExit(exit_function=exited.append,
                              windows_api=_FakeWindows(init_fails=True, cleanup_fails=True))
    with pytest.raises(OSError):
        owner.force_exit()
    assert exited == [0]


def test_desktop_entry_binds_exit_without_changing_cli(monkeypatch):
    from galgame_news.desktop import app as entry

    events = []
    owner = type("Owner", (), {"force_exit": lambda self, code=0: events.append(("exit", code))})()
    monkeypatch.setattr(entry, "DesktopProcessExit", lambda: owner)

    def create(argv, *, exit_handler):
        assert exit_handler == owner.force_exit
        events.append(("create", argv))
        return (type("App", (), {"exec": lambda self: 3})(),
                type("Window", (), {"show": lambda self: events.append("show")})())

    monkeypatch.setattr(entry, "create_application", create)
    assert entry.main(["toolbox"]) == 3
    assert events == [("create", ["toolbox"]), "show", ("exit", 3)]


_DESKTOP_CHILD = r'''
import json, pathlib, sqlite3, subprocess, sys, threading, time
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication
from galgame_news.desktop.process_exit import DesktopProcessExit, _WindowsProcesses
from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.window import MainWindow
from galgame_news.pipeline import CancellationToken
from galgame_news.tasks.repository import TaskRepository

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
api = _WindowsProcesses()
if mode == "fallback":
    def unavailable():
        raise OSError("simulated host job restriction")
    api.create_job = unavailable
owner = DesktopProcessExit(windows_api=api)
descendant = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"],
    stdout=subprocess.PIPE, text=True)
grandchild_pid = int(descendant.stdout.readline())
repo = TaskRepository(root / "task")
repo.save_review_state({"revision": 1, "decisions": {"committed": "accepted"}, "manual_decisions": ["committed"]})
final = root / "task" / "final" / "images"
final.mkdir(parents=True)
(final / "existing.bin").write_bytes(b"completed asset - must survive exit")
app = QApplication([])
controller = DesktopController(app_data=root / "app")
window = MainWindow(controller, exit_handler=owner.force_exit)
window._confirm_force_close = lambda: True
# A paused request with an unresponsive running worker must not block exit.
thread = QThread()
thread.run = lambda: time.sleep(60)
thread.start()
controller._thread = thread
controller._token = CancellationToken()
controller._token.pause()
controller.review_page.state_writer._failed[id(object())] = RuntimeError("save failed")
locked = threading.Event()
def lock_database():
    db = sqlite3.connect(repo.path)
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE review_decisions SET decision='rejected' WHERE media_id='committed'")
    locked.set()
    time.sleep(60)
threading.Thread(target=lock_database, daemon=False).start()
assert locked.wait(5)
window.show()
(root / "ready.json").write_text(json.dumps({"child": descendant.pid, "grandchild": grandchild_pid,
    "job": owner._job is not None}), encoding="utf-8")
timer = QTimer()
def trigger():
    if (root / "exit").exists():
        timer.stop()
        window.close()
timer.timeout.connect(trigger)
timer.start(10)
app.exec()
raise RuntimeError("close returned without force exit")
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop process ownership")
@pytest.mark.parametrize("mode", ["job", "fallback"])
def test_real_desktop_forces_exit_with_locked_database_and_owned_children(tmp_path, mode):
    from galgame_news.tasks.repository import TaskRepository

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env)
    proc = subprocess.Popen([sys.executable, "-c", _DESKTOP_CHILD, str(tmp_path), mode],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    api = _WindowsProcesses()
    wait = api.kernel.WaitForSingleObject
    wait.argtypes, wait.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    handles = []
    try:
        deadline = time.monotonic() + 20
        ready = tmp_path / "ready.json"
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            proc.kill()
            out, err = proc.communicate(timeout=5)
            pytest.fail(f"isolated desktop did not start: {out!r} {err!r}")
        data = json.loads(ready.read_text(encoding="utf-8"))
        assert data["job"] == (mode == "job")
        for pid in (data["child"], data["grandchild"]):
            handle = api.kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
            assert handle
            handles.append(handle)
        started = time.monotonic()
        (tmp_path / "exit").touch()
        proc.wait(timeout=1)
        for handle in handles:
            assert wait(handle, max(0, int((1 - (time.monotonic() - started)) * 1000))) == 0
        elapsed = time.monotonic() - started
        assert elapsed < 1
        assert unrelated.poll() is None
        assert proc.returncode == 0
        print(f"{mode}: desktop and both descendants exited in {elapsed * 1000:.1f} ms")
        reopened = TaskRepository(tmp_path / "task", create=False)
        assert reopened.load_review_state()["decisions"] == {"committed": "accepted"}
        assert reopened.integrity_check()["ok"]
        assert (tmp_path / "task" / "final" / "images" / "existing.bin").read_bytes() == b"completed asset - must survive exit"
    finally:
        if proc.poll() is None:
            proc.kill()
        for handle in handles:
            if wait(handle, 0) != 0:
                api.kernel.TerminateProcess(handle, 1)
                wait(handle, 1000)
            api.kernel.CloseHandle(handle)
        proc.communicate(timeout=5)
        unrelated.kill()
        unrelated.wait(timeout=5)
