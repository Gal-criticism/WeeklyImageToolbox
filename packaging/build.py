"""Build and safely publish the Windows portable onedir artifact."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "packaging" / "GalgameNewsToolbox.spec"
DIST_ROOT = PROJECT_ROOT / "dist"
BUNDLE_NAME = "WeeklyImageToolbox"
TARGET_EXECUTABLE = BUNDLE_NAME + ".exe"
SHORTCUT_NAME = "00_启动工具箱.lnk"


def _powershell_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(
    *,
    shortcut_path: Path | str | None = None,
    target_path: Path | str | None = None,
    working_directory: Path | str | None = None,
    runner: Callable[..., Any] | None = None,
) -> bool:
    """Create the root Windows shortcut without adding a console window.

    Non-Windows hosts skip the shell integration so offline packaging tests can
    exercise the build staging logic without requiring Windows COM.
    """

    if os.name != "nt":
        return False
    shortcut = Path(shortcut_path or PROJECT_ROOT / SHORTCUT_NAME)
    target = Path(target_path or DIST_ROOT / BUNDLE_NAME / TARGET_EXECUTABLE)
    workdir = Path(working_directory or PROJECT_ROOT)
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    temporary = shortcut.parent / f".{shortcut.name}.{uuid.uuid4().hex}.tmp.lnk"
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$link = $shell.CreateShortcut({_powershell_literal(temporary)}); "
        f"$link.TargetPath = {_powershell_literal(target)}; "
        f"$link.WorkingDirectory = {_powershell_literal(workdir)}; "
        "$link.WindowStyle = 7; "
        "$link.Save()"
    )
    invoke = runner or subprocess.run
    try:
        invoke(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
        )
        os.replace(temporary, shortcut)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"cannot create toolbox shortcut: {shortcut}") from exc
    return True


def _assert_executable_unlocked(path: Path) -> None:
    """Fail before copying anything when Windows has the target EXE open."""

    if not path.exists() or os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0,  # no sharing: an active executable handle is rejected
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise RuntimeError(f"target executable is unavailable or in use: {path}")
    kernel32.CloseHandle(handle)


def _staged_runtime_files(source: Path) -> list[tuple[Path, Path]]:
    """List staged runtime files while excluding user-data directories."""

    files: list[tuple[Path, Path]] = []
    for root, directories, filenames in os.walk(source, followlinks=False):
        directories[:] = [directory for directory in directories if directory.casefold() != "output"]
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            relative = path.relative_to(source)
            if relative.parts and relative.parts[0].casefold() == "output":
                continue
            files.append((path, relative))
    return sorted(files, key=lambda pair: str(pair[1]))


def _copy_tree_transactionally(source: Path, target: Path) -> None:
    """Publish staged runtime files with rollback for every file touched.

    The PyInstaller ``_internal`` tree is replaced as a unit so stale Qt
    binaries cannot survive a rebuild.  Existing files at other staged paths
    are copied into a system temporary rollback directory.  No unknown target
    files or ``output`` contents are traversed or removed.
    """

    files = _staged_runtime_files(source)
    target_was_missing = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    rollback_root = Path(tempfile.mkdtemp(prefix=".galgame-news-rollback-"))
    backups: list[tuple[Path, Path]] = []
    created_files: list[Path] = []
    created_directories: list[Path] = []
    staged_internal = source / "_internal"
    target_internal = target / "_internal"
    internal_backup = rollback_root / "_internal"
    internal_replaced = False
    try:
        if staged_internal.is_dir() and target_internal.exists():
            if not target_internal.is_dir():
                raise IsADirectoryError(target_internal)
            shutil.copytree(target_internal, internal_backup, symlinks=True)
            internal_replaced = True
            shutil.rmtree(target_internal)
        for _staged, relative in files:
            destination = target / relative
            if destination.exists() and not destination.is_file():
                raise IsADirectoryError(destination)
            if destination.is_file():
                backup = rollback_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
                backups.append((destination, backup))
        for staged, relative in files:
            destination = target / relative
            parent = destination.parent
            missing_parents: list[Path] = []
            while not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                created_directories.append(directory)
            if not destination.exists():
                created_files.append(destination)
            shutil.copy2(staged, destination)
    except Exception:
        for destination in reversed(created_files):
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        for destination, backup in reversed(backups):
            try:
                shutil.copy2(backup, destination)
            except OSError:
                pass
        if internal_replaced:
            try:
                if target_internal.exists():
                    shutil.rmtree(target_internal)
                shutil.copytree(internal_backup, target_internal, symlinks=True)
            except OSError:
                pass
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        if target_was_missing:
            try:
                target.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(rollback_root, ignore_errors=True)


def publish_bundle(staged_bundle: Path | str, target_bundle: Path | str | None = None) -> Path:
    """Overlay a staged bundle while preserving unknown files and ``output``."""

    staged = Path(staged_bundle).resolve()
    target = Path(target_bundle or DIST_ROOT / BUNDLE_NAME).resolve()
    executable = target / TARGET_EXECUTABLE
    _assert_executable_unlocked(executable)
    _copy_tree_transactionally(staged, target)
    return target


def build(*, clean: bool = True, pyinstaller_main: Any | None = None) -> int:
    """Build in a temporary directory, then safely overlay the portable bundle."""

    if pyinstaller_main is None:
        try:
            from PyInstaller import __main__ as pyinstaller_main
        except ImportError as exc:
            raise RuntimeError(
                "PyInstaller is required; install the optional build extra"
            ) from exc

    with tempfile.TemporaryDirectory(prefix=".galgame-news-build-") as temporary_root:
        staging = Path(temporary_root)
        staged_dist = staging / "dist"
        staged_build = staging / "build"
        arguments = [
            "--noconfirm",
            "--distpath",
            str(staged_dist),
            "--workpath",
            str(staged_build),
        ]
        if clean:
            arguments.append("--clean")
        arguments.append(str(SPEC_PATH))
        pyinstaller_main.run(arguments)
        staged_bundle = staged_dist / BUNDLE_NAME
        if not staged_bundle.is_dir():
            raise RuntimeError(f"PyInstaller did not produce {staged_bundle}")
        target_bundle = publish_bundle(staged_bundle)
    create_shortcut(
        target_path=target_bundle / TARGET_EXECUTABLE,
        working_directory=PROJECT_ROOT,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
