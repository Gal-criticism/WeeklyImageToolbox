"""Offline SQLite storage benchmark and safe legacy-task migration audit.

The benchmark uses synthetic rows only. ``--audit-task`` accepts a legacy task
directory but migrates a disposable clone under the current working directory;
the supplied task is only read and its JSON/final hashes are checked again.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import threading
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from galgame_news.tasks.migration import migrate_task
from galgame_news.tasks.repository import TaskRepository


DEFAULT_REPORT = ROOT / "docs" / "development" / "sqlite-validation.json"


class _WindowsWorkingSetSampler:
    """Sample this process's working set without third-party dependencies."""

    def __init__(self):
        self.available = platform.system() == "Windows"
        self.peak = None
        self._stop = threading.Event()
        self._thread = None
        if not self.available:
            return
        try:

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            self._counters_type = Counters
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self._kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            self._psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            self._psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            self._handle = self._kernel32.GetCurrentProcess()
            if self._sample() is None:
                self.available = False
        except (AttributeError, OSError):
            self.available = False

    def _sample(self):
        if not self.available:
            return None
        counters = self._counters_type()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(
            self._handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)

    def start(self):
        if not self.available:
            return
        first = self._sample()
        if first is None:
            self.available = False
            return
        self.peak = first

        def sample_loop():
            while not self._stop.wait(0.005):
                value = self._sample()
                if value is not None:
                    self.peak = max(self.peak or 0, value)

        self._thread = threading.Thread(target=sample_loop, daemon=True)
        self._thread.start()

    def finish(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            final = self._sample()
            if final is not None:
                self.peak = max(self.peak or 0, final)
        return self.peak if self.available else None


def _measure(function):
    rss = _WindowsWorkingSetSampler()
    rss.start()
    tracemalloc.start()
    started = time.perf_counter()
    result = function()
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_peak = rss.finish()
    return result, {
        "seconds": round(elapsed, 6),
        "python_tracemalloc_peak_bytes": peak,
        "process_rss_peak_bytes_sampled": rss_peak,
        "process_rss_measurement": (
            "Windows working set sampled every 5 ms"
            if rss_peak is not None
            else "unavailable on this platform"
        ),
    }


def benchmark(candidate_count: int = 3000) -> dict[str, Any]:
    rows = [
        {
            "id": f"candidate-{i:05d}",
            "news_id": f"news-{i % 50:02d}",
            "section": "new" if i % 2 else "event",
            "image_url": f"https://fixture.invalid/{i}.png",
            "selected": i % 7 == 0,
            "score": i % 101 / 100,
        }
        for i in range(candidate_count)
    ]
    with tempfile.TemporaryDirectory(
        prefix=".tmp-sqlite-validation-", dir=Path.cwd()
    ) as name:
        temp = Path(name)
        json_path = temp / "snapshot.json"
        _unused, json_write = _measure(
            lambda: json_path.write_text(
                json.dumps(rows, separators=(",", ":")), encoding="utf-8"
            )
        )

        def json_query_100():
            match_count = 0
            for _ in range(100):
                loaded = json.loads(json_path.read_text(encoding="utf-8"))
                match_count += len(
                    [row for row in loaded if row["section"] == "new"][:100]
                )
            return match_count

        json_queried, json_query = _measure(json_query_100)

        task_root = temp / "task"
        repo = TaskRepository(task_root)
        repo.activate()
        _unused, sqlite_initial = _measure(
            lambda: repo.upsert_candidates(rows, kind="image")
        )
        updates = [
            {**rows[i], "score": 1.0, "selected": True}
            for i in range(min(10, candidate_count))
        ]
        _unused, sqlite_update_10 = _measure(
            lambda: repo.upsert_candidates(updates, kind="image")
        )

        def query_100():
            match_count = 0
            for _ in range(100):
                match_count += len(
                    repo.query_candidates(section="new", kind="image", limit=100)
                )
            return match_count

        queried, sqlite_query_100 = _measure(query_100)
        size = json_path.stat().st_size
        return {
            "candidate_count": candidate_count,
            "json_snapshot": {**json_write, "bytes": size},
            "json_read_100_section_filters": {
                **json_query,
                "rows_returned": json_queried,
                "news_count": 50,
            },
            "sqlite_initial_insert": sqlite_initial,
            "sqlite_update_10_rows": sqlite_update_10,
            "sqlite_query_100_section_filters": {
                **sqlite_query_100,
                "rows_returned": queried,
            },
        }


def _hash_files(root: Path, predicate) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if not path.is_file() or not predicate(path):
            continue
        result[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def _copy_task_readonly(source: Path, destination: Path) -> None:
    """Clone JSON/final data; hard-link immutable non-state media where safe."""
    for directory, subdirs, filenames in os.walk(source):
        base = Path(directory)
        relative = base.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        subdirs[:] = [name for name in subdirs if name not in {"__pycache__"}]
        for filename in filenames:
            src = base / filename
            dst = target_dir / filename
            # SQLite, JSON, and final files get independent copies. Other
            # assets are read-only migration inputs and may be hard-linked.
            protected = src.suffix.casefold() in {
                ".json",
                ".sqlite3",
                ".db",
            } or "final" in {part.casefold() for part in src.parts}
            if not protected:
                try:
                    os.link(src, dst)
                    continue
                except OSError:
                    pass
            shutil.copy2(src, dst)


def _json_file(root: Path, relative: str, default):
    path = root / relative
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _rows(payload, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = next((payload.get(key) for key in keys if key in payload), [])
    else:
        raw = []
    return (
        [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    )


_PATH_KEYS = {
    "local_path",
    "original_path",
    "output_path",
    "path",
    "source_path",
    "state_path",
}


def _normalize(value, root: Path, key: str = "", *aliases: Path):
    if isinstance(value, dict):
        return {
            name: _normalize(item, root, name, *aliases) for name, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item, root, key, *aliases) for item in value]
    if isinstance(value, str) and key in _PATH_KEYS:
        try:
            path = Path(value)
            if path.is_absolute():
                resolved = path.resolve()
                for candidate_root in (root, *aliases):
                    try:
                        return (
                            "<TASK>/"
                            + resolved.relative_to(candidate_root.resolve()).as_posix()
                        )
                    except ValueError:
                        continue
                return "<EXTERNAL>/" + path.name
            if ".." not in path.parts:
                return "<TASK>/" + path.as_posix()
        except (ValueError, OSError):
            return "<EXTERNAL>/" + Path(value).name
    return value


def _compare_rows(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    root: Path,
    *aliases: Path,
) -> dict[str, int]:
    old = {
        str(row.get("id") or row.get("candidate_id")): _normalize(
            row, root, "", *aliases
        )
        for row in before
    }
    new = {
        str(row.get("id") or row.get("candidate_id")): _normalize(
            row, root, "", *aliases
        )
        for row in after
    }
    common = old.keys() & new.keys()
    mismatches = 0
    fields_compared = 0
    mismatch_fields: dict[str, int] = {}
    for ident in common:
        keys = old[ident].keys() & new[ident].keys()
        fields_compared += len(keys)
        for key in keys:
            if old[ident][key] != new[ident][key]:
                mismatches += 1
                mismatch_fields[key] = mismatch_fields.get(key, 0) + 1
    return {
        "before": len(old),
        "after": len(new),
        "common_ids": len(common),
        "fields_compared": fields_compared,
        "field_mismatches": mismatches,
        "mismatched_fields_by_key": dict(
            sorted(mismatch_fields.items(), key=lambda item: (-item[1], item[0]))[:20]
        ),
        "missing_after": len(old.keys() - new.keys()),
        "added_after": len(new.keys() - old.keys()),
    }


def _compare_records(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    root: Path,
    *aliases: Path,
) -> dict[str, int]:
    canonical = lambda rows: sorted(
        json.dumps(
            _normalize(row, root, "", *aliases), ensure_ascii=False, sort_keys=True
        )
        for row in rows
    )
    old, new = canonical(before), canonical(after)
    return {
        "before": len(old),
        "after": len(new),
        "matching_records": sum(a == b for a, b in zip(old, new)),
    }


def audit_task(task_root: Path, temp_root: Path) -> dict[str, Any]:
    """Migrate a disposable clone and compare its legacy records and protected files."""
    source = task_root.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    original_protected_before = _hash_files(
        source,
        lambda p: p.suffix.casefold() == ".json"
        or "final" in {x.casefold() for x in p.parts},
    )
    clone = temp_root / source.name
    _copy_task_readonly(source, clone)
    clone_protected_before = _hash_files(
        clone,
        lambda p: p.suffix.casefold() == ".json"
        or "final" in {x.casefold() for x in p.parts},
    )

    old_images = _rows(
        _json_file(clone, "raw/image_index.json", {}),
        ("candidates", "images", "image_candidates"),
    )
    old_videos = _rows(
        _json_file(clone, "raw/video_index.json", {}),
        ("videos", "video_candidates", "candidates"),
    )
    old_failures = _rows(_json_file(clone, "raw/failed_items.json", []), ("failures",))
    old_review = _json_file(clone, "review_state.json", {})
    old_checkpoint = _json_file(clone, "checkpoint.json", {})
    old_sources = (
        old_checkpoint.get("source_map", {}) if isinstance(old_checkpoint, dict) else {}
    )

    migration = migrate_task(clone)
    repository = TaskRepository(clone, create=False)
    image_payload, video_payload, _review_required, failures = (
        repository.export_review_payloads()
    )
    new_images = _rows(image_payload, ("candidates", "images", "image_candidates"))
    new_videos = _rows(video_payload, ("videos", "video_candidates", "candidates"))
    new_checkpoint = repository.load_checkpoint() or {}
    new_review = repository.load_review_state() or {}
    clone_protected_after = _hash_files(
        clone,
        lambda p: p.suffix.casefold() == ".json"
        or "final" in {x.casefold() for x in p.parts},
    )
    original_protected_after = _hash_files(
        source,
        lambda p: p.suffix.casefold() == ".json"
        or "final" in {x.casefold() for x in p.parts},
    )
    protected_unchanged = (
        clone_protected_before == clone_protected_after
        and original_protected_before == original_protected_after
    )
    old_decisions = (
        old_review.get("decisions", {}) if isinstance(old_review, dict) else {}
    )
    old_manual = (
        set(map(str, old_review.get("manual_decisions", [])))
        if isinstance(old_review, dict)
        else set()
    )
    new_decisions = (
        new_review.get("decisions", {}) if isinstance(new_review, dict) else {}
    )
    new_manual = (
        set(map(str, new_review.get("manual_decisions", [])))
        if isinstance(new_review, dict)
        else set()
    )
    source_before = _normalize(old_sources, clone, "", source)
    source_after = _normalize(new_checkpoint.get("source_map", {}), clone, "", source)
    final_files = [
        name
        for name in clone_protected_before
        if "/final/" in f"/{name.casefold()}/" or name.casefold().startswith("final/")
    ]
    return {
        "migration": {
            "migrated": bool(migration.get("migrated")),
            "already_authoritative": bool(migration.get("already_authoritative")),
            "counts": migration.get("counts", {}),
            "diagnostic_count": len(migration.get("diagnostics", []))
            + len(migration.get("warnings", [])),
        },
        "legacy_comparison": {
            "images": _compare_rows(old_images, new_images, clone, source),
            "videos": _compare_rows(old_videos, new_videos, clone, source),
            "failures": _compare_records(old_failures, failures, clone, source),
            "asset_path_normalization": "absolute task paths from source and disposable clone are compared by task-relative path",
            "review_decisions_before": len(old_decisions),
            "review_decisions_after": len(new_decisions),
            "manual_decisions_before": len(old_manual),
            "manual_decisions_after": len(new_manual),
            "decision_values_match": old_decisions == new_decisions,
            "manual_decisions_match": old_manual == new_manual,
            "sources_before": len(old_sources),
            "sources_after": len(new_checkpoint.get("source_map", {})),
            "source_records_match": source_before == source_after,
        },
        "protected_files": {
            "json_count": sum(
                name.casefold().endswith(".json") for name in clone_protected_before
            ),
            "final_file_count": len(final_files),
            "unchanged_in_clone_and_source": protected_unchanged,
        },
        "integrity": repository.integrity_check()["ok"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=3000)
    parser.add_argument(
        "--audit-task",
        type=Path,
        help="audit migration of a disposable clone of a legacy task",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    if args.candidates < 1:
        parser.error("--candidates must be positive")
    report: dict[str, Any] = {
        "schema_version": 1,
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.system(),
            "benchmark_scope": "synthetic, offline",
            "memory_metric": "Python tracemalloc and sampled process RSS (working set) reported separately",
        },
        "benchmark": benchmark(args.candidates),
    }
    if args.audit_task:
        with tempfile.TemporaryDirectory(
            prefix=".tmp-sqlite-validation-", dir=Path.cwd()
        ) as name:
            report["legacy_task_audit"] = audit_task(args.audit_task, Path(name))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.audit_task:
        audit = report["legacy_task_audit"]
        compared = audit["legacy_comparison"]
        consistent = all(
            compared[kind]["missing_after"] == 0
            and compared[kind]["added_after"] == 0
            and compared[kind]["field_mismatches"] == 0
            for kind in ("images", "videos")
        )
        failures = compared["failures"]
        consistent = (
            consistent
            and failures["before"] == failures["after"] == failures["matching_records"]
        )
        consistent = consistent and all(
            compared[key]
            for key in (
                "decision_values_match",
                "manual_decisions_match",
                "source_records_match",
            )
        )
        if not (
            consistent
            and audit["integrity"]
            and audit["protected_files"]["unchanged_in_clone_and_source"]
        ):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
