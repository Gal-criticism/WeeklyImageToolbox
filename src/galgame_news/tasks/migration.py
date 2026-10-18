"""One-way, restartable import of a task's legacy JSON state into SQLite.

The JSON files remain the compatibility/export format.  In particular this
module never edits, renames, or removes them; SQLite becomes authoritative only
after every critical input has been validated and copied.
"""

from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable
from urllib.parse import quote

from PIL import Image

from ..delivery.asset_paths import recover_asset_path
from ..domain import FailureRecord, ImageCandidate, SourceRef, VideoCandidate
from ..pipeline.contracts import Checkpoint
from ..pipeline.download_state import public_media_payload
from ..pipeline.checkpoint import validate_checkpoint_associations
from ..review.session import ReviewSession
from .assets import TaskAssets
from .repository import TaskRepository


def _read(path: Path, *, required: bool = False) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ValueError(f"missing legacy state: {path.name}")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed legacy state {path.name}: {exc}") from exc


def _rows(
    value: Any,
    keys: tuple[str, ...],
    label: str,
    diagnostics: list[dict[str, Any]] | None = None,
    source: str = "",
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = next((value[k] for k in keys if k in value), [])
    else:
        raise ValueError(f"{label} must be an object or list")
    if not isinstance(values, list):
        raise ValueError(f"{label} records must be a list")
    result = []
    for index, row in enumerate(values):
        if isinstance(row, dict):
            result.append(dict(row))
        elif diagnostics is not None:
            diagnostics.append(
                {
                    "source": source,
                    "record": index,
                    "error": f"{label} record is not an object",
                }
            )
    return result


def _confined_payload(
    value: Any,
    root: Path,
    original_roots: tuple[Path, ...] = (),
    diagnostics: list[dict[str, Any]] | None = None,
) -> Any:
    """Store path fields relative to root and reject symlink/traversal escapes."""
    path_keys = {"local_path", "original_path", "output_path", "path"}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in path_keys and isinstance(item, str) and item:
                raw = Path(item).expanduser()
                if raw.is_absolute():
                    # Absolute legacy references are eligible only when they
                    # point inside this task. Copied-root recovery is handled
                    # separately, and only with an expected content hash.
                    try:
                        item = str(raw.resolve().relative_to(root))
                    except (ValueError, OSError):
                        if key == "local_path":
                            expected = value.get("output_sha256") or value.get("sha256")
                        elif key == "original_path":
                            expected = value.get("original_sha256") or value.get(
                                "sha256"
                            )
                        else:
                            expected = value.get("sha256")
                        recovered = None
                        # Rebase only from a root explicitly inferred from a
                        # candidate's recorded absolute path. Preserve its
                        # relative suffix and require the recorded content hash.
                        for old_root in original_roots:
                            try:
                                suffix = raw.resolve().relative_to(old_root)
                                if not suffix.parts or suffix.parts[
                                    0
                                ].casefold() not in {"raw", ".work"}:
                                    continue
                                if not expected:
                                    continue
                                if suffix.parts[0].casefold() == ".work":
                                    target = recover_asset_path(
                                        root.joinpath(*suffix.parts),
                                        root / "raw",
                                        expected,
                                    )
                                else:
                                    target = (root / suffix).resolve()
                                    target.relative_to(root)
                                    if (
                                        target.is_file()
                                        and hashlib.sha256(
                                            target.read_bytes()
                                        ).hexdigest()
                                        != str(expected).casefold()
                                    ):
                                        target = None
                                if target is not None:
                                    recovered = target
                                    break
                            except (ValueError, OSError):
                                continue
                        if recovered is None:
                            recovered = recover_asset_path(raw, root / "raw", expected)
                        if recovered is None:
                            # Preserve unavailable candidates for retry without
                            # retaining a foreign absolute path.
                            if diagnostics is not None:
                                diagnostics.append(
                                    {
                                        "source": "path_recovery",
                                        "path": str(raw),
                                        "error": "foreign or unverified asset path was not imported",
                                    }
                                )
                            item = None
                        else:
                            item = str(recovered.relative_to(root))
                else:
                    if ".." in raw.parts:
                        item = None
                    else:
                        try:
                            resolved = (root / raw).resolve()
                            resolved.relative_to(root)
                            item = str(raw)
                        except (ValueError, OSError):
                            item = None
            else:
                item = _confined_payload(item, root, original_roots, diagnostics)
            result[key] = item
        return result
    if isinstance(value, list):
        return [
            _confined_payload(item, root, original_roots, diagnostics) for item in value
        ]
    return value


def _original_roots(root: Path, *payloads: Any) -> tuple[Path, ...]:
    """Infer same-named copied task roots from documented raw/.work paths."""
    roots: set[Path] = set()
    records: list[Any] = []
    if payloads:
        checkpoint = payloads[0]
        if isinstance(checkpoint, dict):
            for field in ("candidates", "videos"):
                if isinstance(checkpoint.get(field), list):
                    records.extend(checkpoint[field])
            result = checkpoint.get("result")
            if isinstance(result, dict):
                for field in (
                    "candidates",
                    "filtered_candidates",
                    "review_required",
                    "videos",
                ):
                    if isinstance(result.get(field), list):
                        records.extend(result[field])
        for index in payloads[1:]:
            if isinstance(index, dict):
                for field in (
                    "candidates",
                    "images",
                    "image_candidates",
                    "videos",
                    "video_candidates",
                ):
                    if isinstance(index.get(field), list):
                        records.extend(index[field])
            elif isinstance(index, list):
                records.extend(index)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "local_path",
                    "original_path",
                    "output_path",
                    "path",
                } and isinstance(child, str):
                    try:
                        path = Path(child).expanduser()
                        if path.is_absolute():
                            parts = path.parts
                            indexes = [
                                i
                                for i, part in enumerate(parts[:-1])
                                if part.casefold() == "raw"
                            ]
                            if indexes:
                                inferred = Path(*parts[: indexes[-1]])
                                suffix = parts[indexes[-1] :]
                                if (
                                    inferred.name.casefold() == root.name.casefold()
                                    and suffix
                                    and suffix[0].casefold() in {"raw", ".work"}
                                ):
                                    if suffix[0].casefold() != ".work" or (
                                        len(suffix) >= 3
                                        and suffix[1].casefold().startswith("publish-")
                                    ):
                                        roots.add(inferred)
                            work_indexes = [
                                i
                                for i, part in enumerate(parts[:-1])
                                if part.casefold() == ".work"
                            ]
                            if work_indexes:
                                index = work_indexes[-1]
                                inferred = Path(*parts[:index])
                                suffix = parts[index:]
                                if (
                                    inferred.name.casefold() == root.name.casefold()
                                    and len(suffix) >= 3
                                    and suffix[1].casefold().startswith("publish-")
                                ):
                                    roots.add(inferred)
                    except (OSError, ValueError):
                        pass
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for payload in records:
        visit(payload)
    return tuple(roots)


def _cancelled(cancelled: Any) -> bool:
    if cancelled is None:
        return False
    return bool(cancelled() if callable(cancelled) else cancelled.is_set())


def _progress(
    progress: Callable[..., Any] | None, report: dict[str, Any], phase: str
) -> None:
    if progress is not None:
        progress({**report, "phase": phase, "counts": dict(report["counts"])})


def _existing_identity(root: Path) -> dict[str, Any]:
    path = root / "task_state.sqlite3"
    if not path.is_file():
        return {}
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_meta'"
            ).fetchone()
            if not exists:
                return {}
            rows = db.execute(
                "SELECT key,value FROM task_meta WHERE key IN ('task_id','issue_id')"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"existing task database cannot be inspected: {exc}") from exc
    identity = {}
    for key, value in rows:
        try:
            identity[key] = json.loads(value)
        except (ValueError, TypeError):
            identity[key] = value
    return identity


def _sanitize_checkpoint(
    payload: dict[str, Any], diagnostics: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep a checkpoint's identity strict while isolating malformed rows."""
    data = dict(payload)
    for field, model in (
        ("candidates", ImageCandidate),
        ("videos", VideoCandidate),
        ("failures", FailureRecord),
    ):
        rows = data.get(field, [])
        if not isinstance(rows, list):
            raise ValueError(f"checkpoint {field} must be a list")
        valid = []
        for index, row in enumerate(rows):
            try:
                valid.append(model.model_validate(row).model_dump(mode="json"))
            except (TypeError, ValueError) as exc:
                diagnostics.append(
                    {
                        "source": "checkpoint.json",
                        "record": index,
                        "collection": field,
                        "error": str(exc),
                    }
                )
        data[field] = valid
    result = data.get("result")
    if result is not None:
        if not isinstance(result, dict):
            raise ValueError("checkpoint result must be an object")
        result = dict(result)
        for field, model in (
            ("candidates", ImageCandidate),
            ("filtered_candidates", ImageCandidate),
            ("review_required", ImageCandidate),
            ("videos", VideoCandidate),
            ("failures", FailureRecord),
        ):
            rows = result.get(field, [])
            if not isinstance(rows, list):
                raise ValueError(f"checkpoint result {field} must be a list")
            valid = []
            for index, row in enumerate(rows):
                try:
                    valid.append(model.model_validate(row).model_dump(mode="json"))
                except (TypeError, ValueError) as exc:
                    diagnostics.append(
                        {
                            "source": "checkpoint.json",
                            "record": index,
                            "collection": f"result.{field}",
                            "error": str(exc),
                        }
                    )
            result[field] = valid
        data["result"] = result
    return data


def _asset_valid(
    root: Path,
    relative: Any,
    expected_hash: Any = None,
    expected_size: Any = None,
    *,
    image: bool = False,
) -> bool:
    if not isinstance(relative, str) or not relative:
        return False
    try:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return False
        resolved = (root / path).resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return False
        size = resolved.stat().st_size
        if size > 512 * 1024 * 1024:
            return False
        if expected_size is not None and int(expected_size) != size:
            return False
        if expected_hash:
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != str(expected_hash).casefold():
                return False
        if image:
            with Image.open(resolved) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > 50_000_000:
                    return False
                source.verify()
            with Image.open(resolved) as source:
                source.load()
        return True
    except (
        OSError,
        ValueError,
        TypeError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False


def _private_asset(
    value: Any,
    private_root: Path,
    retries_root: Path,
    batch_root: Path,
    expected_hash: Any,
    *,
    image: bool = False,
) -> Path | None:
    """Resolve and verify only files explicitly referenced inside private retries."""
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = Path(value).expanduser()
        candidates = [raw.resolve()] if raw.is_absolute() else [
            (private_root / raw).resolve(), (batch_root / raw).resolve()
        ]
        path = next(
            candidate for candidate in candidates
            if candidate.is_relative_to(retries_root.resolve())
        )
        if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_hash and digest != str(expected_hash).casefold():
            return None
        if image:
            with Image.open(path) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > 50_000_000:
                    return None
                source.verify()
            with Image.open(path) as source:
                source.load()
        return path
    except (OSError, ValueError, TypeError, StopIteration, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _import_private_batches(
    private_root: Path,
    task_root: Path,
    repo: TaskRepository,
    known_news: set[str],
    report: dict[str, Any],
    cancelled: Any,
) -> int:
    """Import a validated private task's retry records without modifying it."""
    private_root = private_root.resolve()
    retry_root = (private_root / "retries").resolve()
    try:
        retry_root.relative_to(private_root)
    except ValueError:
        report["diagnostics"].append(
            {"source": str(private_root / "retries"), "error": "private retries root escapes task root"}
        )
        return 0
    if not retry_root.is_dir():
        return 0
    existing = {
        (str(row.get("news_id")), str(row.get("attempt_id")))
        for row in repo.list_batches()
    }
    assets = TaskAssets(task_root)
    imported = 0

    def diagnostic(path: Path, error: str, record: int | None = None) -> None:
        item = {"source": str(path), "error": error}
        if record is not None:
            item["record"] = record
        report["diagnostics"].append(item)

    def confined_dir(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return path.resolve().parent == parent.resolve()
        except (OSError, ValueError):
            return False

    def confined_file(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return path.resolve().parent == parent.resolve()
        except (OSError, ValueError):
            return False

    def copy_image_asset(row: dict[str, Any], field: str, hash_field: str, size_field: str) -> None:
        value = row.get(field)
        if not value:
            return
        expected_hash = row.get(hash_field) or row.get("sha256")
        source = _private_asset(value, private_root, retry_root, batch_dir, expected_hash, image=True)
        if source is None:
            row[field] = None
            row["download_status"] = "retryable_failed"
            diagnostic(batch_dir, f"invalid or out-of-scope image asset: {field}")
            return
        try:
            if row.get(size_field) is not None and int(row[size_field]) != source.stat().st_size:
                raise ValueError("size mismatch")
        except (TypeError, ValueError):
            row[field] = None
            row["download_status"] = "retryable_failed"
            diagnostic(batch_dir, f"image asset size mismatch: {field}")
            return
        stored = assets.put_original(source) if field == "original_path" else assets.put_review(
            source.read_bytes(), source.suffix or ".img", "legacy"
        )
        row[field] = str(stored.relative_to(task_root))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if field == "original_path":
            row["original_sha256"] = digest
            row["original_byte_size"] = source.stat().st_size
        else:
            row["output_sha256"] = digest
            row["output_byte_size"] = source.stat().st_size

    for news_dir in sorted(retry_root.iterdir()):
        if _cancelled(cancelled):
            break
        if not confined_dir(news_dir, retry_root):
            diagnostic(news_dir, "private news directory escapes retries root")
            continue
        if not news_dir.is_dir() or news_dir.name not in known_news:
            if news_dir.is_dir():
                diagnostic(news_dir, "private retry news_id is not in source task")
            continue
        for batch_dir in sorted(news_dir.iterdir()):
            if _cancelled(cancelled):
                break
            if not confined_dir(batch_dir, news_dir):
                diagnostic(batch_dir, "private batch directory escapes news directory")
                continue
            if not batch_dir.is_dir() or (news_dir.name, batch_dir.name) in existing:
                continue
            index_path = batch_dir / "image_index.json"
            try:
                if not confined_file(index_path, batch_dir):
                    raise ValueError("image index escapes batch directory")
                payload = _read(index_path, required=True)
                if not isinstance(payload, dict):
                    raise ValueError("batch payload must be an object")
                if str(payload.get("news_id") or news_dir.name) != news_dir.name:
                    raise ValueError("batch news_id mismatch")
                if not isinstance(payload.get("candidates", payload.get("images", [])), list):
                    raise ValueError("batch candidates must be a list")
                video_path = batch_dir / "video_index.json"
                if video_path.exists() and not confined_file(video_path, batch_dir):
                    raise ValueError("video index escapes batch directory")
                video_payload = _read(video_path) if video_path.exists() else {}
                if not isinstance(video_payload, dict):
                    raise ValueError("batch video index must be an object")
                video_rows = video_payload.get("videos", video_payload.get("video_candidates", video_payload.get("candidates", [])))
                failures = payload.get("failures")
                failure_path = batch_dir / "failed_items.json"
                if failures is None:
                    if failure_path.exists() and not confined_file(failure_path, batch_dir):
                        raise ValueError("failure index escapes batch directory")
                    failure_payload = _read(failure_path) if failure_path.exists() else []
                    if isinstance(failure_payload, dict):
                        failures = failure_payload.get("failures", failure_payload.get("items", []))
                    else:
                        failures = failure_payload
                kind = payload.get("kind") or payload.get("attempt_kind") or "retry"
                status = payload.get("status", "completed")
                if kind not in {"retry", "supplement"}:
                    raise ValueError("invalid retry batch kind")
                if status not in {"running", "completed", "partial", "failed", "cancelled"}:
                    raise ValueError("invalid retry batch status")
                if not isinstance(video_rows, list) or not isinstance(failures, list):
                    raise ValueError("batch videos/failures must be lists")
            except (OSError, ValueError, TypeError) as exc:
                diagnostic(index_path, f"invalid private batch: {exc}")
                continue

            image_rows = []
            for offset, raw in enumerate(payload.get("candidates", payload.get("images", []))):
                try:
                    if not isinstance(raw, dict) or str(raw.get("news_id") or news_dir.name) != news_dir.name:
                        raise ValueError("invalid candidate or news_id mismatch")
                    candidate, ident = ReviewSession._coerce_image(dict(raw, news_id=news_dir.name))
                    if candidate is None:
                        raise ValueError("candidate could not be coerced")
                    row = candidate.model_dump(mode="json")
                    # Preserve extra provenance fields carried by retry batches.
                    row.update({k: v for k, v in raw.items() if k not in row})
                    row["id"] = ident
                    copy_image_asset(row, "original_path", "original_sha256", "original_byte_size")
                    copy_image_asset(row, "local_path", "output_sha256", "output_byte_size")
                    if row.get("local_path") is None and row.get("download_status") == "downloaded":
                        row["download_status"] = "retryable_failed"
                    image_rows.append(_confined_payload(row, task_root, diagnostics=report["diagnostics"]))
                except (TypeError, ValueError, OSError) as exc:
                    diagnostic(index_path, f"invalid candidate: {exc}", offset)
            video_rows_valid = []
            for offset, raw in enumerate(video_rows):
                try:
                    if not isinstance(raw, dict) or str(raw.get("news_id") or news_dir.name) != news_dir.name:
                        raise ValueError("invalid video candidate or news_id mismatch")
                    candidate, ident = ReviewSession._coerce_video(dict(raw, news_id=news_dir.name))
                    if candidate is None:
                        raise ValueError("video candidate could not be coerced")
                    row = candidate.model_dump(mode="json")
                    path_value = raw.get("local_path")
                    if path_value:
                        source = _private_asset(path_value, private_root, retry_root, batch_dir, row.get("sha256"))
                        if source is None:
                            row["local_path"] = None
                            diagnostic(index_path, "invalid or out-of-scope video asset", offset)
                        else:
                            stored = assets.put_original(source)
                            row["local_path"] = str(stored.relative_to(task_root))
                            row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                            row["byte_size"] = source.stat().st_size
                    video_rows_valid.append(_confined_payload(row, task_root, diagnostics=report["diagnostics"]))
                except (TypeError, ValueError, OSError) as exc:
                    diagnostic(index_path, f"invalid video candidate: {exc}", offset)
            valid_failures = []
            for offset, raw in enumerate(failures):
                try:
                    failure = FailureRecord.model_validate(raw).model_dump(mode="json")
                    if failure.get("news_id") and str(failure["news_id"]) != news_dir.name:
                        raise ValueError("failure news_id mismatch")
                    valid_failures.append(failure)
                except (TypeError, ValueError) as exc:
                    diagnostic(index_path, f"invalid failure record: {exc}", offset)
            source_rows = payload.get("sources", [])
            if not isinstance(source_rows, list):
                diagnostic(index_path, "batch sources must be a list")
                source_rows = []
            valid_sources = []
            for offset, raw in enumerate(source_rows):
                try:
                    valid_sources.append(SourceRef.model_validate(raw).model_dump(mode="json"))
                except (TypeError, ValueError) as exc:
                    diagnostic(index_path, f"invalid source record: {exc}", offset)
            try:
                repo.save_batch(
                    news_dir.name, batch_dir.name,
                    str(Path("retries") / news_dir.name / batch_dir.name),
                    kind, status, image_rows, video_rows_valid,
                    valid_failures, payload.get("source_url"), valid_sources,
                )
                existing.add((news_dir.name, batch_dir.name))
                imported += 1
            except (TypeError, ValueError, OSError) as exc:
                diagnostic(index_path, f"could not save private batch: {exc}")
    return imported


def _validate_image_paths(
    candidate: dict[str, Any],
    root: Path,
    diagnostics: list[dict[str, Any]],
    source: str,
) -> None:
    valid = False
    for key, expected_hash, expected_size, label in (
        (
            "local_path",
            candidate.get("output_sha256") or candidate.get("sha256"),
            candidate.get("output_byte_size") or candidate.get("byte_size"),
            "output",
        ),
        (
            "original_path",
            candidate.get("original_sha256") or candidate.get("sha256"),
            candidate.get("original_byte_size") or candidate.get("byte_size"),
            "original",
        ),
    ):
        value = candidate.get(key)
        if not value:
            continue
        if _asset_valid(root, value, expected_hash, expected_size, image=True):
            valid = True
        else:
            diagnostics.append(
                {
                    "source": source,
                    "candidate_id": candidate.get("id"),
                    "path": str(value),
                    "error": f"{label} image asset missing or failed size/hash/decode validation",
                }
            )
            candidate[key] = None
    if candidate.get("download_status") == "downloaded" and not valid:
        candidate["download_status"] = "retryable_failed"


def migrate_task(
    task_root: Path | str,
    progress: Callable[..., Any] | None = None,
    cancelled: Any = None,
    review_state_override: dict[str, Any] | None = None,
    private_task_root: Path | str | None = None,
) -> dict[str, Any]:
    """Import legacy files, resumably, then activate SQLite as task authority.

    Critical malformed JSON or an invalid checkpoint is rejected before a
    repository is opened, leaving both the original files and any existing DB
    untouched. Per-record journals, caches, and retry batches are isolated.
    """
    root = Path(task_root).expanduser().resolve()
    private_root = (
        Path(private_task_root).expanduser().resolve()
        if private_task_root is not None
        else None
    )
    report: dict[str, Any] = {
        "task_root": str(root),
        "migrated": False,
        "already_authoritative": False,
        "counts": {},
        "warnings": [],
        "diagnostics": [],
        "cancelled": False,
    }
    if TaskRepository.is_authoritative(root):
        report.update(migrated=True, already_authoritative=True)
        _progress(progress, report, "complete")
        return report

    # Preflight critical files before creating or touching the database.
    checkpoint_path = root / "checkpoint.json"
    checkpoint = _read(checkpoint_path) if checkpoint_path.exists() else None
    if checkpoint_path.exists():
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be an object")
        checkpoint = _sanitize_checkpoint(checkpoint, report["diagnostics"])
        parsed = Checkpoint.model_validate(checkpoint)
        validate_checkpoint_associations(parsed)
        checkpoint = parsed.model_dump(mode="json")

    raw = root / "raw"
    image_path, video_path = raw / "image_index.json", raw / "video_index.json"
    image_index = _read(image_path) if image_path.exists() else None
    video_index = _read(video_path) if video_path.exists() else None
    # ReviewSession's public loader performs the established legacy coercion,
    # path restoration, duplicate-ID handling, and .work publish recovery.
    review_required_path = raw / "review_required.json"
    review_required = (
        _read(review_required_path) if review_required_path.exists() else []
    )
    failed_path = raw / "failed_items.json"
    failed_payload = _read(failed_path) if failed_path.exists() else []
    if image_path.exists() and not isinstance(image_index, (dict, list)):
        raise ValueError("image_index.json has an invalid structure")
    if video_path.exists() and not isinstance(video_index, (dict, list)):
        raise ValueError("video_index.json has an invalid structure")
    if not isinstance(review_required, (dict, list)):
        raise ValueError("review_required.json has an invalid structure")
    if not isinstance(failed_payload, (dict, list)):
        raise ValueError("failed_items.json has an invalid structure")
    state_path = root / "review_state.json"
    options_path = root / "request_options.json"
    legacy_options_path = root / "options.json"
    review_state = (
        copy.deepcopy(review_state_override)
        if review_state_override is not None
        else (_read(state_path) if state_path.exists() else None)
    )
    review_state_source = (
        "review_state_override"
        if review_state_override is not None
        else "review_state.json"
    )
    options = (
        _read(options_path)
        if options_path.exists()
        else (_read(legacy_options_path) if legacy_options_path.exists() else None)
    )
    if options is None and private_root is not None:
        private_options_path = private_root / "request_options.json"
        options = _read(private_options_path) if private_options_path.exists() else None
    if review_state is not None and not isinstance(review_state, dict):
        raise ValueError(f"{review_state_source} must be an object")
    if review_state is not None:
        decisions = review_state.get("decisions", {})
        manual = review_state.get("manual_decisions", [])
        revision = review_state.get("revision", 0)
        if (
            not isinstance(decisions, dict)
            or not isinstance(manual, list)
            or any(not isinstance(item, (str, int)) for item in manual)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError(
                f"{review_state_source} has invalid decisions, manual_decisions, or revision"
            )
        for media_id, value in decisions.items():
            decision = value.get("decision") if isinstance(value, dict) else value
            if decision not in {"accepted", "rejected", "pending"}:
                raise ValueError(
                    f"{review_state_source} has invalid decision for {media_id}"
                )
    if (options_path.exists() or legacy_options_path.exists()) and not isinstance(
        options, dict
    ):
        raise ValueError("request_options.json must be an object")
    checkpoint_issue = checkpoint.get("issue_id") if checkpoint else None
    index_issues = [
        value.get("issue_id")
        for value in (image_index, video_index)
        if isinstance(value, dict) and value.get("issue_id")
    ]
    if checkpoint_issue and any(
        str(value) != str(checkpoint_issue) for value in index_issues
    ):
        raise ValueError("legacy index issue_id does not match checkpoint")
    if len({str(value) for value in index_issues}) > 1:
        raise ValueError(
            "legacy image and video indexes have different issue_id values"
        )
    existing_identity = _existing_identity(root)
    desired_issue = checkpoint_issue or (index_issues[0] if index_issues else None)
    if (
        existing_identity.get("task_id")
        and checkpoint
        and str(existing_identity["task_id"]) != str(checkpoint["task_id"])
    ):
        raise ValueError("staged task database task_id does not match checkpoint")
    if (
        existing_identity.get("issue_id")
        and desired_issue
        and str(existing_identity["issue_id"]) != str(desired_issue)
    ):
        raise ValueError("staged task database issue_id does not match legacy state")
    checkpoint_news: set[str] = set()
    if checkpoint:
        checkpoint_news = {
            str(item.get("news_id") or item.get("id"))
            for item in checkpoint["issue"].get("news_items", [])
        }
    known_news = set(checkpoint_news)
    for index in (image_index, video_index):
        if isinstance(index, dict):
            for item in (
                index.get("news_items", index.get("items", []))
                if isinstance(index.get("news_items", index.get("items", [])), list)
                else []
            ):
                if isinstance(item, dict) and (item.get("news_id") or item.get("id")):
                    known_news.add(str(item.get("news_id") or item.get("id")))
            for key in (
                "candidates",
                "images",
                "image_candidates",
                "videos",
                "video_candidates",
            ):
                rows = index.get(key, [])
                if isinstance(rows, list):
                    known_news.update(
                        str(row["news_id"])
                        for row in rows
                        if isinstance(row, dict) and row.get("news_id")
                    )
        if isinstance(index, list):
            for item in index:
                if isinstance(item, dict) and item.get("news_id"):
                    known_news.add(str(item["news_id"]))
    if checkpoint:
        if known_news - checkpoint_news:
            raise ValueError(
                "legacy indexes contain news metadata not present in checkpoint issue"
            )
        for value, keys in (
            (image_index, ("candidates", "images", "image_candidates")),
            (video_index, ("videos", "video_candidates", "candidates")),
        ):
            records = (
                value
                if isinstance(value, list)
                else (
                    next((value.get(k, []) for k in keys if k in value), [])
                    if isinstance(value, dict)
                    else []
                )
            )
            if isinstance(records, list) and any(
                isinstance(row, dict)
                and row.get("news_id")
                and str(row["news_id"]) not in checkpoint_news
                for row in records
            ):
                raise ValueError(
                    "legacy index contains news_id not present in checkpoint issue"
                )
    failures_raw = _rows(
        failed_payload,
        ("failures",),
        "failed items",
        report["diagnostics"],
        "raw/failed_items.json",
    )
    legacy_failures = []
    for index, row in enumerate(failures_raw):
        try:
            failure = FailureRecord.model_validate(row).model_dump(mode="json")
            if (
                known_news
                and failure.get("news_id")
                and failure["news_id"] not in known_news
            ):
                raise ValueError("failure news_id is not present in the migrated task")
            legacy_failures.append(failure)
        except (TypeError, ValueError) as exc:
            report["diagnostics"].append(
                {"source": "raw/failed_items.json", "record": index, "error": str(exc)}
            )
    index_failure_rows = []
    for source, index_payload in (
        ("raw/image_index.json", image_index),
        ("raw/video_index.json", video_index),
    ):
        for row in _rows(
            index_payload,
            ("failures",),
            "index failures",
            report["diagnostics"],
            source,
        ):
            try:
                failure = FailureRecord.model_validate(row).model_dump(mode="json")
                if (
                    known_news
                    and failure.get("news_id")
                    and failure["news_id"] not in known_news
                ):
                    raise ValueError(
                        "failure news_id is not present in the migrated task"
                    )
                index_failure_rows.append(failure)
            except (TypeError, ValueError) as exc:
                report["diagnostics"].append({"source": source, "error": str(exc)})
    all_failures = list(
        {
            json.dumps(row, sort_keys=True): row
            for row in [*index_failure_rows, *legacy_failures]
        }.values()
    )
    if checkpoint is not None and legacy_failures:
        by_key = {
            json.dumps(row, sort_keys=True): row
            for row in checkpoint.get("failures", [])
        }
        for row in legacy_failures:
            by_key.setdefault(json.dumps(row, sort_keys=True), row)
        checkpoint["failures"] = list(by_key.values())
    original_roots = _original_roots(root, checkpoint, image_index, video_index)
    if checkpoint is not None:
        checkpoint = _confined_payload(
            checkpoint, root, original_roots, report["diagnostics"]
        )
        for field in ("candidates", "filtered_candidates", "review_required"):
            for candidate in checkpoint.get(field, []):
                if isinstance(candidate, dict):
                    _validate_image_paths(
                        candidate, root, report["diagnostics"], "checkpoint.json"
                    )
        if isinstance(checkpoint.get("result"), dict):
            for field in ("candidates", "filtered_candidates", "review_required"):
                for candidate in checkpoint["result"].get(field, []):
                    if isinstance(candidate, dict):
                        _validate_image_paths(
                            candidate, root, report["diagnostics"], "checkpoint.json"
                        )
            retryable = {
                str(item.get("id"))
                for field in ("candidates", "filtered_candidates", "review_required")
                for item in checkpoint["result"].get(field, [])
                if isinstance(item, dict)
                and item.get("id")
                and item.get("download_status") == "retryable_failed"
            }
        else:
            retryable = set()
        retryable.update(
            str(item.get("id"))
            for field in ("candidates", "filtered_candidates", "review_required")
            for item in checkpoint.get(field, [])
            if isinstance(item, dict)
            and item.get("id")
            and item.get("download_status") == "retryable_failed"
        )
        checkpoint["retryable_candidate_ids"] = sorted(
            set(map(str, checkpoint.get("retryable_candidate_ids", []))) | retryable
        )

    _progress(progress, report, "validated")
    if _cancelled(cancelled):
        report["cancelled"] = True
        return report

    session = None
    has_index_records = bool(
        _rows(image_index, ("candidates", "images", "image_candidates"), "image index")
        or _rows(
            video_index, ("videos", "video_candidates", "candidates"), "video index"
        )
    )
    if has_index_records:
        try:
            if state_path.exists():
                session = ReviewSession.from_output(
                    raw, task_root=root, state_path=state_path
                )
            else:
                # ReviewSession persists initial defaults when its state path is
                # absent. Give it an external scratch file so migration never
                # writes into the legacy task tree before SQLite activation.
                with tempfile.TemporaryDirectory(
                    prefix="galnews-migration-"
                ) as scratch:
                    scratch_state = Path(scratch) / "review_state.json"
                    scratch_state.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "decisions": {},
                                "manual_decisions": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    session = ReviewSession.from_output(
                        raw, task_root=root, state_path=scratch_state
                    )
        except ValueError as exc:
            # An index file present but unparseable is critical; do not mark a
            # partial migration authoritative.
            raise ValueError(f"legacy review indexes are invalid: {exc}") from exc

    repo = TaskRepository(root, create=True)
    if checkpoint is not None:
        repo.save_checkpoint(checkpoint)
        repo.set_meta("task_id", checkpoint["task_id"])
        repo.set_meta("issue_id", checkpoint["issue_id"])
        report["counts"]["checkpoint_candidates"] = len(
            checkpoint.get("candidates", [])
        )
    images: dict[str, dict[str, Any]] = {}
    videos: dict[str, dict[str, Any]] = {}
    review_ids: set[str] = set()
    if session is not None:
        for candidate in session.images:
            value = candidate.model_dump(mode="json")
            value = _confined_payload(
                value, root, original_roots, report["diagnostics"]
            )
            if value.get("download_status") not in {"downloaded", "not_required"}:
                value["download_status"] = (
                    "retryable_failed"
                    if (candidate.local_path or candidate.original_path)
                    and not any(
                        (root / str(value.get(k))).is_file()
                        for k in ("local_path", "original_path")
                        if value.get(k)
                    )
                    else value.get("download_status", "pending")
                )
            images[candidate.id] = value
        for candidate in session.videos:
            videos[candidate.id] = _confined_payload(
                candidate.model_dump(mode="json"),
                root,
                original_roots,
                report["diagnostics"],
            )
        # Preserve raw alias records (including original metadata) alongside
        # coercion by session, keyed by its resolved stable IDs.
        image_raw = _rows(
            image_index,
            ("candidates", "images", "image_candidates"),
            "image index",
            report["diagnostics"],
            "raw/image_index.json",
        )
        video_raw = _rows(
            video_index,
            ("videos", "video_candidates", "candidates"),
            "video index",
            report["diagnostics"],
            "raw/video_index.json",
        )
        for row in image_raw:
            candidate, ident = ReviewSession._coerce_image(row)
            if candidate is None:
                report["diagnostics"].append(
                    {
                        "source": "raw/image_index.json",
                        "candidate_id": ident,
                        "error": "image candidate could not be coerced",
                    }
                )
            if candidate and ident in images:
                # Raw legacy flags matter for review and failed-download retry.
                images[ident].update(
                    _confined_payload(row, root, original_roots, report["diagnostics"])
                )
                images[ident]["id"] = ident
                if row.get("download_status") in {"pending", "retryable_failed"}:
                    images[ident]["download_status"] = row["download_status"]
        for row in video_raw:
            candidate, ident = ReviewSession._coerce_video(row)
            if candidate is None:
                report["diagnostics"].append(
                    {
                        "source": "raw/video_index.json",
                        "candidate_id": ident,
                        "error": "video candidate could not be coerced",
                    }
                )
            if candidate and ident in videos:
                videos[ident].update(
                    _confined_payload(row, root, original_roots, report["diagnostics"])
                )
                videos[ident]["id"] = ident
        for value in images.values():
            _validate_image_paths(
                value, root, report["diagnostics"], "raw/image_index.json"
            )
        from ..review.session import _review_ids

        review_ids = _review_ids(review_required)
        report["counts"].update(images=len(images), videos=len(videos))
        # Index-only imported tasks still need issue/news metadata and an index
        # snapshot in SQLite for ReviewSession.from_repository/export paths.
        image_payload = dict(image_index) if isinstance(image_index, dict) else {}
        video_payload = dict(video_index) if isinstance(video_index, dict) else {}
        image_payload["issue_id"] = (
            image_payload.get("issue_id") or (checkpoint or {}).get("issue_id") or ""
        )
        video_payload["issue_id"] = (
            video_payload.get("issue_id") or image_payload["issue_id"]
        )
        if not image_payload.get("news_items"):
            image_payload["news_items"] = list(session.news_items)
        if not image_payload.get("news_items"):
            image_payload["news_items"] = image_payload.get(
                "items", video_payload.get("news_items", video_payload.get("items", []))
            )
        normalized_news = []
        for item in image_payload.get("news_items", []):
            if isinstance(item, dict):
                item = dict(item)
                identity = str(item.get("news_id") or item.get("id") or "")
                if identity:
                    item["id"] = identity
                    item["news_id"] = identity
                normalized_news.append(item)
        image_payload["news_items"] = normalized_news
        image_payload["candidates"] = list(images.values())
        video_payload["videos"] = list(videos.values())
        repo.save_index(
            image_payload, video_payload, reviews=review_required, failures=all_failures
        )
    elif image_path.exists() or video_path.exists() or legacy_failures:
        image_payload = dict(image_index) if isinstance(image_index, dict) else {}
        video_payload = dict(video_index) if isinstance(video_index, dict) else {}
        image_payload["issue_id"] = (
            image_payload.get("issue_id") or (checkpoint or {}).get("issue_id") or ""
        )
        video_payload["issue_id"] = (
            video_payload.get("issue_id") or image_payload["issue_id"]
        )
        if not image_payload.get("news_items"):
            image_payload["news_items"] = image_payload.get(
                "items", video_payload.get("news_items", video_payload.get("items", []))
            )
        image_payload["news_items"] = [
            {
                **item,
                "id": str(item.get("news_id") or item.get("id")),
                "news_id": str(item.get("news_id") or item.get("id")),
            }
            for item in image_payload.get("news_items", [])
            if isinstance(item, dict) and (item.get("news_id") or item.get("id"))
        ]
        image_payload["candidates"] = []
        video_payload["videos"] = []
        repo.save_index(
            image_payload, video_payload, reviews=review_required, failures=all_failures
        )
    if review_state is not None:
        repo.save_review_state(review_state)
    if options:
        repo.save_options(options)
    if review_ids:
        report["counts"]["review_required"] = len(review_ids)
    canonical_candidate_ids = {
        str(value.get("id"))
        for value in [*images.values(), *videos.values()]
        if value.get("id")
    }
    if checkpoint is not None:
        canonical_candidate_ids.update(
            str(value.get("id"))
            for key in ("candidates", "videos")
            for value in checkpoint.get(key, [])
            if isinstance(value, dict) and value.get("id")
        )
    _progress(progress, report, "core_state_copied")
    if _cancelled(cancelled):
        report["cancelled"] = True
        return report

    # Journal/cache records are independent snapshots: diagnose a malformed
    # record and continue with its siblings.
    media_dir = root / "media_state"
    journal_rows: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}
    if media_dir.is_dir():
        for path in sorted(media_dir.glob("*.json")):
            try:
                payload = _read(path, required=True)
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("candidates"), list
                ):
                    raise ValueError("expected journal object with candidates list")
                news_id = str(payload.get("news_id") or "")
                if not news_id:
                    raise ValueError("journal has no news_id")
                if known_news and news_id not in known_news:
                    raise ValueError(
                        "journal news_id is not present in the migrated task"
                    )
                identity = {
                    key: payload.get(key) for key in ("input_sha256", "config_sha256")
                }
                rows = []
                for index, rawrow in enumerate(payload["candidates"]):
                    if not isinstance(rawrow, dict):
                        report["diagnostics"].append(
                            {
                                "source": str(path.relative_to(root)),
                                "record": index,
                                "error": "journal candidate is not an object",
                            }
                        )
                        continue
                    candidate, ident = ReviewSession._coerce_image(rawrow)
                    if candidate is None or str(candidate.news_id) != news_id:
                        report["diagnostics"].append(
                            {
                                "source": str(path.relative_to(root)),
                                "record": index,
                                "candidate_id": ident,
                                "error": "journal candidate is invalid or has mismatched news_id",
                            }
                        )
                        continue
                    value = _confined_payload(
                        candidate.model_dump(mode="json"),
                        root,
                        original_roots,
                        report["diagnostics"],
                    )
                    _validate_image_paths(
                        value, root, report["diagnostics"], str(path.relative_to(root))
                    )
                    rows.append(value)
                revision = int(payload.get("journal_revision", 0))
                identity_key = json.dumps(identity, sort_keys=True)
                key = (news_id, identity_key)
                if key not in journal_rows or revision > journal_rows[key][0]:
                    journal_rows[key] = (revision, rows)
            except (OSError, ValueError, TypeError) as exc:
                report["diagnostics"].append(
                    {"source": str(path.relative_to(root)), "error": str(exc)}
                )
            if _cancelled(cancelled):
                report["cancelled"] = True
                return report
    for (news_id, identity_key), (revision, rows) in journal_rows.items():
        # A journal predates the final checkpoint/index snapshot. It can fill
        # missing IDs but must never overwrite a candidate present there.
        novel_rows = [
            row for row in rows if str(row.get("id")) not in canonical_candidate_ids
        ]
        identity = json.loads(identity_key)
        repo.save_journal(news_id, novel_rows, identity)
    report["counts"]["media_journals"] = len(journal_rows)
    _progress(progress, report, "media_journals_copied")
    if _cancelled(cancelled):
        report["cancelled"] = True
        return report

    cache_dir = root / "x_media_cache"
    cache_count = 0
    if cache_dir.is_dir():
        for path in sorted(cache_dir.glob("*.json")):
            try:
                payload = _read(path, required=True)
                if (
                    not isinstance(payload, dict)
                    or not path.stem.isdecimal()
                    or payload.get("state") not in {"pending", "success", "failed"}
                ):
                    raise ValueError("invalid cached post record")
                data = payload.get("payload")
                if data is not None and not isinstance(data, dict):
                    raise ValueError("cached post payload must be an object or null")
                identity = json.dumps(
                    {k: payload.get(k) for k in ("input_sha256", "config_sha256")},
                    sort_keys=True,
                )
                repo.save_post(
                    path.stem,
                    payload["state"],
                    public_media_payload(data) if data is not None else None,
                    json.loads(identity),
                )
                cache_count += 1
            except (OSError, ValueError, TypeError) as exc:
                report["diagnostics"].append(
                    {"source": str(path.relative_to(root)), "error": str(exc)}
                )
            if _cancelled(cancelled):
                report["cancelled"] = True
                return report
    report["counts"]["x_media_cache"] = cache_count
    _progress(progress, report, "post_cache_copied")
    if _cancelled(cancelled):
        report["cancelled"] = True
        return report

    # Retry batches are isolated: malformed batch files never discard valid
    # siblings. Stable IDs derive from the documented directory layout.
    retry_root = root / "retries"
    batch_count = 0
    if retry_root.is_dir():
        for news_dir in sorted(retry_root.iterdir()):
            if not news_dir.is_dir():
                continue
            for batch_dir in sorted(news_dir.iterdir()):
                if not batch_dir.is_dir():
                    continue
                batch_file = batch_dir / "image_index.json"
                try:
                    payload = _read(batch_file, required=True)
                    if not isinstance(payload, dict):
                        raise ValueError("batch payload must be an object")
                    video_file = batch_dir / "video_index.json"
                    video_payload = _read(video_file) if video_file.exists() else {}
                    if not isinstance(video_payload, dict):
                        raise ValueError("video batch payload must be an object")
                    rows = payload.get("candidates", payload.get("images", []))
                    videos_raw = video_payload.get(
                        "videos",
                        video_payload.get(
                            "video_candidates", video_payload.get("candidates", [])
                        ),
                    )
                    failures = payload.get("failures", []) + video_payload.get(
                        "failures", []
                    )
                    if (
                        not isinstance(rows, list)
                        or not isinstance(videos_raw, list)
                        or not isinstance(failures, list)
                    ):
                        raise ValueError("batch records must be lists")
                    recorded_news = [
                        str(value)
                        for value in (
                            payload.get("news_id"),
                            video_payload.get("news_id"),
                        )
                        if value
                    ]
                    if any(value != news_dir.name for value in recorded_news):
                        raise ValueError(
                            "batch news_id does not match its retries directory"
                        )
                    news_id = news_dir.name
                    if news_id not in known_news:
                        raise ValueError(
                            "batch news_id is not present in the migrated task"
                        )
                    images_batch, videos_batch = [], []
                    for offset, row in enumerate(rows):
                        if (
                            not isinstance(row, dict)
                            or str(row.get("news_id") or news_id) != news_id
                        ):
                            report["diagnostics"].append(
                                {
                                    "source": str(batch_file.relative_to(root)),
                                    "record": offset,
                                    "error": "invalid image candidate",
                                }
                            )
                            continue
                        candidate, ident = ReviewSession._coerce_image(
                            dict(row, news_id=news_id)
                        )
                        if candidate is None:
                            report["diagnostics"].append(
                                {
                                    "source": str(batch_file.relative_to(root)),
                                    "record": offset,
                                    "candidate_id": ident,
                                    "error": "image candidate could not be coerced",
                                }
                            )
                            continue
                        value = _confined_payload(
                            candidate.model_dump(mode="json"),
                            root,
                            original_roots,
                            report["diagnostics"],
                        )
                        _validate_image_paths(
                            value,
                            root,
                            report["diagnostics"],
                            str(batch_file.relative_to(root)),
                        )
                        images_batch.append(value)
                    for offset, row in enumerate(videos_raw):
                        if (
                            not isinstance(row, dict)
                            or str(row.get("news_id") or news_id) != news_id
                        ):
                            report["diagnostics"].append(
                                {
                                    "source": str(batch_file.relative_to(root)),
                                    "record": offset,
                                    "error": "invalid video candidate",
                                }
                            )
                            continue
                        candidate, ident = ReviewSession._coerce_video(
                            dict(row, news_id=news_id)
                        )
                        if candidate is None:
                            report["diagnostics"].append(
                                {
                                    "source": str(batch_file.relative_to(root)),
                                    "record": offset,
                                    "candidate_id": ident,
                                    "error": "video candidate could not be coerced",
                                }
                            )
                            continue
                        videos_batch.append(
                            _confined_payload(
                                candidate.model_dump(mode="json"),
                                root,
                                original_roots,
                                report["diagnostics"],
                            )
                        )
                    valid_failures = []
                    for offset, row in enumerate(failures):
                        try:
                            failure = FailureRecord.model_validate(row).model_dump(
                                mode="json"
                            )
                            if (
                                failure.get("news_id")
                                and str(failure["news_id"]) != news_id
                            ):
                                raise ValueError(
                                    "failure news_id does not match retry batch"
                                )
                            valid_failures.append(failure)
                        except (TypeError, ValueError) as exc:
                            report["diagnostics"].append(
                                {
                                    "source": str(batch_file.relative_to(root)),
                                    "record": offset,
                                    "error": "invalid failure record",
                                }
                            )
                    repo.save_batch(
                        news_id,
                        batch_dir.name,
                        str(batch_dir.relative_to(root)),
                        str(
                            payload.get("kind")
                            or payload.get("attempt_kind")
                            or "retry"
                        ),
                        str(payload.get("status", "completed")),
                        images_batch,
                        videos_batch,
                        valid_failures,
                        payload.get("source_url"),
                        payload.get("sources", []),
                    )
                    batch_count += 1
                except (OSError, ValueError, TypeError) as exc:
                    report["diagnostics"].append(
                        {"source": str(batch_file.relative_to(root)), "error": str(exc)}
                    )
                if _cancelled(cancelled):
                    report["cancelled"] = True
                    return report
    report["counts"]["retry_batches"] = batch_count
    if private_root is not None:
        private_count = _import_private_batches(
            private_root, root, repo, known_news, report, cancelled
        )
        report["counts"]["private_retry_batches"] = private_count
        report["counts"]["retry_batches"] += private_count
    if _cancelled(cancelled):
        report["cancelled"] = True
        return report
    check = repo.integrity_check()
    # Missing downloaded assets are an expected retryable legacy state. The
    # repository's strict check reports them; SQLite structure must still pass.
    if check.get("quick_check") != "ok":
        raise ValueError("migrated task database failed integrity check")
    _progress(progress, report, "validated_database")
    repo.activate()
    report["migrated"] = True
    _progress(progress, report, "complete")
    return report
