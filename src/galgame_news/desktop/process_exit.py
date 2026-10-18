"""Process-level desktop exit, independent of Qt and worker shutdown waits.

Only the real desktop entry point constructs this owner. Widget tests inject
an exit callback instead; importing this module never changes process state.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IoCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]


class _WindowsProcesses:
    """Typed Win32 calls; handles, not executable names, identify ownership."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessTimes": ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def create_job(self):
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(job, self.kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            # Assignment failed: this still-empty job is safe to close.
            self.kernel.CloseHandle(job)
            raise
        return job

    def terminate_job(self, job, code):
        return bool(self.kernel.TerminateJobObject(job, code))

    def terminate_self(self, code):
        # TerminateProcess bypasses DLL detach. CRT _exit/ExitProcess can
        # deadlock on a lock held by one of the deliberately abandoned workers.
        return bool(self.kernel.TerminateProcess(self.kernel.GetCurrentProcess(), code))

    def _created(self, handle):
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            return None
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def _snapshot(self):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        try:
            entry = _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            result = []
            available = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while available:
                result.append((int(entry.pid), int(entry.parent)))
                available = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            return result
        finally:
            self.kernel.CloseHandle(snapshot)

    def terminate_descendants(self, root_pid, code=0):
        """Bounded fallback when a job cannot be assigned (e.g. a host policy).

        Keep process handles open throughout cleanup to avoid PID reuse. Each
        link is checked against its parent's creation time before termination.
        Never touch siblings, ancestors or other instances of the toolbox.
        """
        root_created = self._created(self.kernel.GetCurrentProcess())
        if root_created is None or root_pid != os.getpid():
            return
        owned = {root_pid: (None, root_created)}
        terminated = set()
        deadline = time.monotonic() + 0.25
        try:
            # Repeat after killing owned parents to catch their last children.
            for _ in range(3):
                rows = self._snapshot()
                changed = True
                while changed and time.monotonic() < deadline:
                    changed = False
                    for pid, parent in rows:
                        if pid in owned or parent not in owned:
                            continue
                        handle = self.kernel.OpenProcess(0x1001, False, pid)
                        if not handle:
                            continue
                        created = self._created(handle)
                        if created is None or created < owned[parent][1]:
                            self.kernel.CloseHandle(handle)
                            continue
                        owned[pid] = (handle, created)
                        changed = True
                for pid, (handle, _) in owned.items():
                    if handle is not None and pid not in terminated:
                        self.kernel.TerminateProcess(handle, code)
                        terminated.add(pid)
                if time.monotonic() >= deadline:
                    break
        finally:
            for handle, _ in owned.values():
                if handle is not None:
                    self.kernel.CloseHandle(handle)


class DesktopProcessExit:
    """Own this desktop instance and end it without Python/Qt finalizers."""

    def __init__(self, *, exit_function=None, windows_api=None):
        self._exit = exit_function or os._exit
        self._native_exit = exit_function is None
        self._windows = None
        self._job = None
        self._exiting = False
        self.initialization_error = None
        if os.name == "nt" or windows_api is not None:
            try:
                self._windows = windows_api or _WindowsProcesses()
                self._job = self._windows.create_job()
            except Exception as exc:
                self.initialization_error = type(exc).__name__

    def force_exit(self, code=0):
        if self._exiting:
            return
        self._exiting = True
        try:
            if self._windows is not None:
                try:
                    if self._job is not None and self._windows.terminate_job(self._job, code):
                        return  # The real Win32 call has terminated this process.
                except Exception:
                    pass
                self._windows.terminate_descendants(os.getpid(), code)
        finally:
            if self._native_exit and self._windows is not None:
                try:
                    if self._windows.terminate_self(code):
                        return
                except Exception:
                    pass
            self._exit(code)


__all__ = ["DesktopProcessExit"]
