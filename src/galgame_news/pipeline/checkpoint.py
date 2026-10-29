"""Versioned checkpoint serialization and integrity helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import Checkpoint
from ..config import CONCURRENCY_FIELDS, BrowserConfig, DEFAULT_REJECTED_IMAGE_TYPES
from ..delivery.helpers import atomic_json_write


CHECKPOINT_SCHEMA_VERSION = 1


def sha256_path(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def config_hash(config, request=None) -> str:
    behavior = {}
    if request is not None:
        behavior = {
            "offline": request.offline,
            "no_videos": request.no_videos,
            "max_images": request.max_images,
            "llm_provider": request.llm_provider,
            "llm_model": request.llm_model,
        }
        if request.use_socialdata_x:
            behavior["use_socialdata_x"] = True
        # Keep old feature-off checkpoints byte-for-byte compatible.
        if getattr(request, "use_archive_materials", False):
            behavior["use_archive_materials"] = True
            if getattr(request, "archive_range", "year") != "year":
                behavior["archive_range"] = request.archive_range
        if getattr(request, "use_image_supplement", False):
            behavior["use_image_supplement"] = True
            behavior["supplement_target_images"] = request.supplement_target_images
            behavior["supplement_sources"] = list(request.supplement_sources)
        sections = getattr(request, "selected_sections", ["x", "h", "z"])
        normalized_sections = [value for value in ("x", "h", "z") if value in sections]
        if normalized_sections != ["x", "h", "z"]:
            behavior["selected_sections"] = normalized_sections
    semantic_config = config.model_dump(mode="json")
    # Added default-only fields must not strand historical download recovery.
    # Explicit browser changes and existing/custom type rules remain hashed.
    if semantic_config.get("browser") == BrowserConfig().model_dump(mode="json"):
        semantic_config.pop("browser", None)
    limits = semantic_config.get("selection", {}).get("type_limits", {})
    if limits.get("decorative") == 0:
        limits.pop("decorative")
    rejected = semantic_config.get("image_types", {}).get("rejected_image_types_by_requirement", {})
    for key, values in rejected.items():
        if values == DEFAULT_REJECTED_IMAGE_TYPES.get(key):
            rejected[key] = [value for value in values if value != "decorative"]
    for field in CONCURRENCY_FIELDS:
        semantic_config["network"].pop(field, None)
    payload = {"config": semantic_config, "request": behavior}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dedupe_videos(videos):
    unique = {}
    for video in videos or []:
        unique.setdefault(video.id or video.video_url, video)
    return list(unique.values())


def _merge_failures(failures):
    unique = {}
    for failure in failures or []:
        key = (
            failure.stage.value,
            failure.news_id,
            failure.candidate_id,
            failure.code,
            failure.message,
            failure.source_url,
        )
        if key not in unique:
            unique[key] = failure
        else:
            current = unique[key]
            occurrences = int(getattr(current, "occurrences", 1)) + int(
                getattr(failure, "occurrences", 1)
            )
            unique[key] = current.model_copy(update={"occurrences": occurrences})
    return list(unique.values())


def write_checkpoint(
    path: Path | str,
    *,
    status,
    task_id,
    issue,
    input_hash,
    config_hash_value,
    completed_news_ids,
    candidates,
    videos,
    failures,
    source_map,
    result=None,
    x_query_count=0,
    media_cache_version=1,
    schema_version: int = CHECKPOINT_SCHEMA_VERSION,
    dedupe_videos=_dedupe_videos,
    merge_failures=_merge_failures,
) -> None:
    checkpoint = Checkpoint(
        schema_version=schema_version,
        status=status,
        task_id=task_id,
        issue_id=issue.issue_id,
        input_sha256=input_hash,
        config_sha256=config_hash_value,
        issue=issue,
        completed_news_ids=sorted(completed_news_ids),
        retryable_candidate_ids=[candidate.id for candidate in candidates if candidate.download_status in {"pending", "retryable_failed"}],
        x_query_count=x_query_count,
        media_cache_version=media_cache_version,
        candidates=list(candidates),
        videos=dedupe_videos(videos),
        failures=merge_failures(failures),
        source_map=source_map,
        result=result,
    )
    validate_checkpoint_associations(checkpoint)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from ..tasks.repository import TaskRepository
    if destination.suffix == ".sqlite3" or TaskRepository.is_authoritative(destination.parent):
        TaskRepository(destination.parent).save_checkpoint(checkpoint.model_dump(mode="json"))
    else:
        atomic_json_write(destination, checkpoint.model_dump(mode="json"))


def load_checkpoint(path: Path | str) -> Checkpoint:
    from ..tasks.repository import TaskRepository
    source = Path(path)
    if source.suffix == ".sqlite3" or TaskRepository.is_authoritative(source.parent):
        payload = TaskRepository(source.parent, create=False).load_checkpoint()
        if payload is None:
            raise ValueError("SQLite 检查点缺失，已保留数据库，请检查或恢复任务备份")
    else:
        payload = json.loads(source.read_text(encoding="utf-8"))
    return Checkpoint.model_validate(payload)


def validate_checkpoint(checkpoint, request, input_hash, config_hash_value, task_id, *, schema_version: int = CHECKPOINT_SCHEMA_VERSION) -> None:
    if checkpoint.schema_version != schema_version:
        raise ValueError("checkpoint schema mismatch")
    if checkpoint.issue_id != request.issue_id or checkpoint.issue.issue_id != request.issue_id:
        raise ValueError("checkpoint issue mismatch")
    if checkpoint.task_id != task_id:
        raise ValueError("checkpoint task mismatch")
    if checkpoint.input_sha256 != input_hash:
        raise ValueError("checkpoint input mismatch")
    if checkpoint.config_sha256 != config_hash_value:
        raise ValueError("checkpoint config mismatch")
    validate_checkpoint_associations(checkpoint)
    if checkpoint.status == "completed" and checkpoint.result is None:
        raise ValueError("completed checkpoint has no result")


def validate_checkpoint_associations(checkpoint) -> None:
    ids = {item.id for item in checkpoint.issue.news_items}
    if len(ids) != len(checkpoint.issue.news_items) or None in ids:
        raise ValueError("checkpoint issue has invalid news ids")
    if not set(checkpoint.completed_news_ids).issubset(ids):
        raise ValueError("checkpoint completed news id is unknown")
    if not set(checkpoint.source_map).issubset(ids):
        raise ValueError("checkpoint source map news id is unknown")
    if not set(checkpoint.retryable_candidate_ids).issubset({candidate.id for candidate in checkpoint.candidates}):
        raise ValueError("checkpoint retry candidate id is unknown")
    for candidate in checkpoint.candidates:
        if candidate.news_id not in ids:
            raise ValueError("checkpoint candidate news id is unknown")
    for video in checkpoint.videos:
        if video.news_id not in ids:
            raise ValueError("checkpoint video news id is unknown")
    if checkpoint.result is not None and checkpoint.result.issue.issue_id != checkpoint.issue_id:
        raise ValueError("checkpoint result issue mismatch")
