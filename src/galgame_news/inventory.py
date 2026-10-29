"""Conservative local media inventory with content based change detection.

Public API: ``ScanRoot(path, role)``, ``scan_inventory(roots, previous=None, ...)``
returns an ``InventoryResult`` with ``manifest``, ``stats``, ``changes`` and
``complete``. Only complete results should be passed to ``save_manifest``.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
SKIP_NAMES = {"cache", "caches", "preview", "previews", "thumbnail", "thumbnails", "thumbs", "tmp", "temp", "temporary", "__pycache__", ".git", ".pytest_cache", ".ruff_cache"}
TEMP_SUFFIXES = (".tmp", ".temp", ".part", ".crdownload", ".download", "~")


def _is_link_or_junction(path: Path) -> bool:
    """Detect POSIX symlinks and Windows reparse-point directories/files."""
    try:
        info = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _is_temporary_name(name: str) -> bool:
    folded = name.casefold()
    return folded.endswith(TEMP_SUFFIXES) or any(marker in folded for marker in (".tmp.", ".temp.", ".part."))


@dataclass(frozen=True)
class ScanRoot:
    path: Path | str
    role: str  # originals, review, or final


@dataclass
class FileRecord:
    path: str
    role: str
    size: int
    mtime_ns: int
    sha256: str
    kind: str
    usable: bool | None = None
    validation_error: str | None = None
    relative_path: str | None = None


@dataclass
class InventoryStats:
    files: int = 0
    physical_unique: int = 0
    usable_files: int = 0
    bytes: int = 0
    by_role: dict[str, int] = field(default_factory=dict)
    usable_by_role: dict[str, int] = field(default_factory=dict)
    content_unique: int = 0
    usable_unique: int = 0
    content_unique_by_role: dict[str, int] = field(default_factory=dict)


@dataclass
class InventoryChanges:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


@dataclass
class InventoryManifest:
    version: int = 1
    records: list[FileRecord] = field(default_factory=list)


@dataclass
class InventoryResult:
    manifest: InventoryManifest
    stats: InventoryStats
    changes: InventoryChanges
    complete: bool
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def load_manifest(path: Path | str) -> InventoryManifest:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return InventoryManifest(version=int(data.get("version", 1)), records=[FileRecord(**r) for r in data.get("records", [])])
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid inventory manifest: {exc}") from exc


def save_manifest(path: Path | str, manifest: InventoryManifest, *, complete: bool) -> None:
    """Atomically replace baseline; refuses evidence from a partial scan."""
    if not complete:
        raise ValueError("partial inventory cannot replace the baseline manifest")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": manifest.version, "records": [asdict(r) for r in manifest.records]}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scan_inventory(
    roots: Iterable[ScanRoot], *, previous: InventoryManifest | None = None,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, str], None] | None = None,
    timeout: float | None = None,
    ffprobe: Callable[[Path, float | None], bool | None] | None = None,
) -> InventoryResult:
    """Hash all discovered files; role overlap is retained as separate records.

    ``ffprobe`` is an optional (path, timeout_seconds)->bool validator. Symlinked
    directories/files are skipped to avoid junction traversal and root escape.
    """
    started = time.monotonic()
    errors: list[str] = []
    found: dict[tuple[str, str], FileRecord] = {}
    cancelled = False
    for scan_root in roots:
        if scan_root.role not in {"originals", "review", "final"}:
            raise ValueError(f"unknown inventory role: {scan_root.role}")
        root = Path(scan_root.path)
        if not root.exists():
            errors.append(f"missing root: {root}")
            continue
        if _is_link_or_junction(root):
            errors.append(f"link or junction root skipped: {root}")
            continue
        def walk_error(exc: OSError) -> None:
            errors.append(f"directory scan failed: {exc}")

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            base = Path(directory)
            if cancel and cancel():
                cancelled = True
                break
            if timeout is not None and time.monotonic() - started >= timeout:
                errors.append("scan timeout")
                cancelled = True
                break
            dirs[:] = sorted(d for d in dirs if d.casefold() not in SKIP_NAMES and not _is_link_or_junction(base / d))
            for name in sorted(files):
                if cancel and cancel():
                    cancelled = True
                    break
                if timeout is not None and time.monotonic() - started >= timeout:
                    errors.append("scan timeout")
                    cancelled = True
                    break
                path = base / name
                if path.suffix.casefold() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS or _is_temporary_name(name):
                    continue
                if _is_link_or_junction(path):
                    continue
                try:
                    before = path.stat()
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        while True:
                            if cancel and cancel():
                                cancelled = True
                                break
                            if timeout is not None and time.monotonic() - started >= timeout:
                                errors.append("scan timeout")
                                cancelled = True
                                break
                            block = stream.read(1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                    if cancelled:
                        break
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        errors.append(f"file changed while scanning: {path}")
                        continue
                    suffix = path.suffix.casefold()
                    kind = "image" if suffix in IMAGE_EXTENSIONS else "video" if suffix in VIDEO_EXTENSIONS else "other"
                    usable: bool | None = None
                    validation_error = None
                    if kind == "image":
                        try:
                            from PIL import Image
                            with Image.open(path) as image:
                                image.verify()
                            usable = True
                        except Exception as exc:
                            usable, validation_error = False, str(exc)
                    elif kind == "video" and ffprobe is not None:
                        try:
                            remaining_timeout = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
                            probe_result = ffprobe(path, remaining_timeout)
                            usable = None if probe_result is None else bool(probe_result)
                            if usable is False:
                                validation_error = "ffprobe rejected media"
                        except Exception as exc:
                            usable, validation_error = False, str(exc)
                    final_stat = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
                        errors.append(f"file changed while validating: {path}")
                        continue
                    record = FileRecord(str(path.resolve()), scan_root.role, final_stat.st_size, final_stat.st_mtime_ns, digest.hexdigest(), kind, usable, validation_error, path.relative_to(root).as_posix())
                    found[(record.path.casefold(), record.role)] = record
                    if progress:
                        progress(len(found), record.path)
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
            if cancelled:
                break
        if cancelled:
            break
    records = sorted(found.values(), key=lambda r: (r.path.casefold(), r.role))
    counts = Counter(r.role for r in records)
    valid_counts = Counter(r.role for r in records if r.usable is True)
    stats = InventoryStats(
        files=len(records),
        physical_unique=len({r.path.casefold() for r in records}),
        usable_files=sum(r.usable is True for r in records),
        bytes=sum(r.size for r in records),
        by_role=dict(counts),
        usable_by_role=dict(valid_counts),
        content_unique=len({r.sha256 for r in records}),
        usable_unique=len({r.sha256 for r in records if r.usable is True}),
        content_unique_by_role={role: len({r.sha256 for r in records if r.role == role}) for role in counts},
    )
    manifest = InventoryManifest(records=records)
    changes = _diff(previous, manifest, complete=not cancelled and not errors)
    return InventoryResult(manifest, stats, changes, not cancelled and not errors, errors, cancelled)


def _diff(previous: InventoryManifest | None, current: InventoryManifest, *, complete: bool = True) -> InventoryChanges:
    if previous is None:
        return InventoryChanges(added=[r.path for r in current.records])
    old = {(r.path.casefold(), r.role): r for r in previous.records}
    new = {(r.path.casefold(), r.role): r for r in current.records}
    changes = InventoryChanges()
    old_hash_paths: dict[str, list[str]] = defaultdict(list)
    new_hash_paths: dict[str, list[str]] = defaultdict(list)
    for record in previous.records:
        old_hash_paths[record.sha256].append(record.path)
    for record in current.records:
        new_hash_paths[record.sha256].append(record.path)
    disappeared = [key for key in old if complete and key not in new]
    appeared = [key for key in new if key not in old]
    matched_new: set[tuple[str, str]] = set()
    for key in disappeared:
        prior = old[key]
        if any(k not in appeared and r.sha256 == prior.sha256 and r.role == prior.role for k, r in new.items()):
            changes.ambiguous.append(prior.path)
            continue
        candidates = [(k, r) for k, r in new.items() if k in appeared and r.sha256 == prior.sha256 and r.role == prior.role and k not in matched_new]
        if len(candidates) == 1:
            new_key, moved_record = candidates[0]
            changes.moved.append((prior.path, moved_record.path)); matched_new.add(new_key)
        elif len(candidates) > 1:
            changes.ambiguous.append(prior.path)
        elif prior.sha256 in new_hash_paths:
            changes.ambiguous.append(prior.path)
        else:
            changes.removed.append(prior.path)
    for key in appeared:
        if key in matched_new:
            continue
        record = new[key]
        same_path = old.get(key)
        if same_path is not None and same_path.sha256 != record.sha256:
            changes.modified.append(record.path)
        elif record.sha256 in old_hash_paths:
            changes.ambiguous.append(record.path)
        else:
            changes.added.append(record.path)
    # Same key is modified in-place.
    for key in old.keys() & new.keys():
        if old[key].sha256 != new[key].sha256:
            changes.modified.append(new[key].path)
    return changes
