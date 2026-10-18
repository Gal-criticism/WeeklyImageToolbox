"""Defensive review state loading and non-destructive final export."""

from __future__ import annotations

import hashlib
import filecmp
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from ..domain import (
    ImageCandidate,
    ImageEvidence,
    ImageType,
    ScoreBreakdown,
    SourceType,
    VideoCandidate,
    VideoStatus,
    candidate_id_for,
    video_candidate_id_for,
)
from ..delivery.helpers import atomic_json_write, atomic_replace, item_names as canonical_item_names, safe_title
from ..delivery.image_conversion import ImageConversionError, ImageConverter
from ..delivery.asset_paths import recover_asset_path, resolve_review_index_root
from ..curation.review_filter import SceneComparisonCache, reviewable_pending_images


class ReviewDecision(str, Enum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    REJECTED = "rejected"


@dataclass
class RetryMergeResult:
    """Description of one retry merge applied to a review session."""

    news_id: str
    attempt_id: str
    added_image_ids: list[str]
    added_video_ids: list[str]
    diagnostics: list[Any] = field(default_factory=list)

    @property
    def added_ids(self) -> list[str]:
        return [*self.added_image_ids, *self.added_video_ids]


@dataclass(frozen=True)
class _PreparedAsset:
    candidate: Any
    raw_type: str
    source: Path | None


@dataclass(frozen=True)
class RetryDelta:
    news_id: str
    attempt_id: str
    images: tuple[_PreparedAsset, ...] = ()
    videos: tuple[_PreparedAsset, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[Any, ...] = ()
    kind: str = "retry"
    status: str = "completed"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return default
    return value


def _as_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _stable_fallback(payload: dict[str, Any], prefix: str) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _items(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _review_ids(payload: Any) -> set[str]:
    result: set[str] = set()
    values = payload if isinstance(payload, list) else payload.get("candidates", []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return result
    for value in values:
        if isinstance(value, str):
            result.add(value)
        elif isinstance(value, dict) and value.get("id") is not None:
            result.add(str(value["id"]))
    return result


def _safe_title(value: Any, fallback: str) -> str:
    return safe_title(value, fallback)


class ReviewSession:
    """Review images/videos from one output directory.

    All source indexes are read-only.  Review decisions are stored in the
    separately managed state path, and final export copies accepted files into
    the task directory.
    """

    _LOW_QUALITY_MIN_WIDTH = 300
    _LOW_QUALITY_MIN_HEIGHT = 300
    _LOW_QUALITY_MIN_PIXELS = 120_000
    _HARD_IMAGE_TYPES = {"logo", "favicon", "icon", "ui", "banner"}

    def __init__(
        self,
        output_dir: Path,
        *,
        task_root: Path | None,
        state_path: Path,
        images: list[ImageCandidate],
        videos: list[VideoCandidate],
        news_items: list[dict[str, Any]],
        source_paths: dict[str, Path],
        raw_types: dict[str, str],
        review_ids: set[str],
        failed_ids: set[str],
        ambiguous_image_ids: set[str] | None = None,
    ):
        self.output_dir = output_dir
        self.task_root = task_root or state_path.parent
        self.retry_root: Path | None = None
        self.legacy_retry_roots: tuple[Path, ...] = ()
        self.state_path = state_path
        self.images = images
        self.videos = videos
        self.news_items = news_items
        self._source_paths = source_paths
        self._raw_types = raw_types
        self._review_ids = review_ids
        self._failed_ids = failed_ids
        self._ambiguous_image_ids = ambiguous_image_ids or set()
        self._decisions: dict[str, ReviewDecision] = {}
        self._manual_decisions: set[str] = set()
        self._image_by_id = {str(item.id): item for item in images}
        self._video_by_id = {str(item.id): item for item in videos}
        self._repository_candidate_ids = set(self._image_by_id) | set(self._video_by_id)
        self.revision = 0
        self._pending_cache: dict[str, tuple[tuple[Any, ...], tuple[ImageCandidate, ...]]] = {}
        self._loaded_state_payload: dict[str, Any] = {"schema_version": 1, "decisions": {}, "manual_decisions": []}
        self._comparison_cache = SceneComparisonCache()

    @classmethod
    def from_output(
        cls,
        output_dir: Path | str,
        *,
        state_path: Path | str | None = None,
        task_root: Path | str | None = None,
    ) -> "ReviewSession":
        root = resolve_review_index_root(output_dir)
        managed_root = Path(task_root).expanduser() if task_root is not None else None
        if state_path is not None:
            state = Path(state_path).expanduser()
        elif managed_root is not None:
            state = managed_root / "review_state.json"
        else:
            # A sibling path is important for imported legacy output: setting a
            # decision must never create a file inside the legacy directory.
            state = root.parent / f".{root.name}.review_state.json"
        from ..tasks.repository import TaskRepository
        db_root = root.parent if root.name.casefold() == "raw" else root
        repository = TaskRepository(db_root, create=False) if TaskRepository.is_authoritative(db_root) else None
        if repository is not None:
            image_payload, video_payload, review_payload, failed_payload = repository.export_review_payloads()
            state = repository.path
            managed_root = db_root
        else:
            image_payload = cls._read_required_json(root / "image_index.json") if (root / "image_index.json").exists() else {}
            video_payload = cls._read_required_json(root / "video_index.json") if (root / "video_index.json").exists() else {}
        if not image_payload and not video_payload:
            raise ValueError("审核索引为空或无法读取，请重新选择有效的任务结果目录。")
        for payload, name, fields in ((image_payload, "图片", ("candidates", "images", "image_candidates")),
                                      (video_payload, "视频", ("videos", "video_candidates", "candidates"))):
            if not isinstance(payload, (dict, list)):
                raise ValueError(f"{name}主索引结构损坏，已保留原文件")
            if isinstance(payload, dict):
                for key in (*fields, "news_items", "items"):
                    if key in payload and not isinstance(payload[key], list):
                        raise ValueError(f"{name}主索引的 {key} 字段损坏，应为列表，已保留原文件")
        if repository is None:
            review_payload = cls._read_required_json(root / "review_required.json") if (root / "review_required.json").exists() else []
            failed_payload = _read_json(root / "failed_items.json", [])
        image_items = _items(image_payload, ("candidates", "images", "image_candidates"))
        video_items = _items(video_payload, ("videos", "video_candidates", "candidates"))
        news_items = cls._news_items(image_payload, video_payload, image_items, video_items)
        review_ids = _review_ids(review_payload)
        failed_ids = cls._failure_ids(image_payload, video_payload, failed_payload)

        images: list[ImageCandidate] = []
        videos: list[VideoCandidate] = []
        source_paths: dict[str, Path] = {}
        raw_types: dict[str, str] = {}
        seen_images: dict[str, str] = {}
        ambiguous_image_ids: set[str] = set()
        seen_videos: set[str] = set()
        for raw in image_items:
            candidate, stable_id = cls._coerce_image(raw)
            if candidate is None:
                continue
            previous_news_id = seen_images.get(stable_id)
            if previous_news_id is not None and previous_news_id == candidate.news_id:
                continue
            if previous_news_id is not None:
                # Legacy indexes used URL-only image IDs. Re-key a cross-news
                # collision locally so review choices stay independent.
                ambiguous_image_ids.add(stable_id)
                stable_id = candidate_id_for(candidate.image_url, candidate.news_id)
                object.__setattr__(candidate, "id", stable_id)
                if stable_id in seen_images:
                    continue
            seen_images[stable_id] = candidate.news_id
            images.append(candidate)
            raw_types[stable_id] = " ".join(
                [str(raw.get("image_type", "")), *[str(value) for value in raw.get("review_reasons", [])]]
            ).casefold()
            source = cls._resolve_image_path(candidate, root)
            if source is not None:
                source_paths[stable_id] = source
        for raw in video_items:
            candidate, stable_id = cls._coerce_video(raw)
            if candidate is None or stable_id in seen_videos:
                continue
            seen_videos.add(stable_id)
            videos.append(candidate)
            raw_types[stable_id] = str(raw.get("media_type", "")).casefold()
            source = cls._resolve_asset_path(raw.get("local_path"), root, candidate.sha256)
            if source is not None:
                object.__setattr__(candidate, "local_path", str(source))
                source_paths[stable_id] = source

        session = cls(
            root,
            task_root=managed_root,
            state_path=state,
            images=images,
            videos=videos,
            news_items=news_items,
            source_paths=source_paths,
            raw_types=raw_types,
            review_ids=review_ids,
            failed_ids=failed_ids,
            ambiguous_image_ids=ambiguous_image_ids,
        )
        session._initialize_decisions()
        return session

    @staticmethod
    def _read_required_json(path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取审核数据 {path}: {exc}") from exc

    @staticmethod
    def _news_items(
        image_payload: Any,
        video_payload: Any,
        image_items: list[dict[str, Any]],
        video_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        source: list[dict[str, Any]] = []
        for payload in (image_payload, video_payload):
            if isinstance(payload, dict):
                values = payload.get("news_items") or payload.get("items") or []
                if isinstance(values, list):
                    source.extend(item for item in values if isinstance(item, dict))
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in sorted(source, key=lambda value: (int(value.get("sequence", 10**9) or 10**9), str(value.get("news_id", "")))):
            news_id = str(item.get("news_id") or item.get("id") or "")
            if news_id and news_id not in seen:
                result.append(dict(item, news_id=news_id))
                seen.add(news_id)
        for raw in [*image_items, *video_items]:
            news_id = str(raw.get("news_id") or "")
            if news_id and news_id not in seen:
                result.append({"news_id": news_id, "sequence": len(result) + 1, "section": "新作", "title": news_id})
                seen.add(news_id)
        return result

    @staticmethod
    def _failure_ids(*payloads: Any) -> set[str]:
        result: set[str] = set()
        for payload in payloads:
            values = _items(payload, ("failures",)) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
            for value in values:
                if isinstance(value, dict) and value.get("candidate_id") is not None:
                    result.add(str(value["candidate_id"]))
        return result

    @classmethod
    def _resolve_asset_path(cls, value: Any, root: Path, expected_hash: str | None) -> Path | None:
        if not value or ".." in Path(str(value)).parts:
            return None
        source = recover_asset_path(value, root, expected_hash)
        if source is not None or any(part.casefold() == ".work" for part in Path(str(value)).parts):
            return source
        # Existing legacy imports can explicitly reference assets outside the
        # index folder. Keep that contract, without using it for stage recovery.
        source = cls._source_path(value, root)
        if source is not None and expected_hash:
            try:
                if hashlib.sha256(source.read_bytes()).hexdigest() != str(expected_hash).casefold():
                    return None
            except OSError:
                return None
        return source

    @classmethod
    def _resolve_image_path(cls, candidate: ImageCandidate, root: Path) -> Path | None:
        resolved: dict[str, Path] = {}
        for name, expected_hash in (
            ("local_path", candidate.output_sha256 or candidate.sha256),
            ("original_path", candidate.original_sha256 or candidate.sha256),
        ):
            recorded = getattr(candidate, name)
            recovered_stage = bool(recorded and any(part.casefold() == ".work" for part in Path(str(recorded)).parts))
            source = cls._resolve_asset_path(recorded, root, expected_hash)
            if source is None:
                object.__setattr__(candidate, name, None)
                continue
            if recovered_stage:
                try:
                    with Image.open(source) as image:
                        image.verify()
                    # verify() checks the container; load() also forces pixel decode.
                    with Image.open(source) as image:
                        image.load()
                except Exception:
                    object.__setattr__(candidate, name, None)
                    continue
            object.__setattr__(candidate, name, str(source))
            resolved[name] = source
        return resolved.get("local_path") or resolved.get("original_path")

    @staticmethod
    def _source_path(value: Any, root: Path) -> Path | None:
        if not value:
            return None
        candidate = Path(str(value))
        candidates = [candidate] if candidate.is_absolute() else [root / candidate, candidate]
        for path in candidates:
            try:
                if path.is_file():
                    return path.resolve()
            except OSError:
                continue
        return None

    @classmethod
    def _coerce_image(cls, raw: dict[str, Any]) -> tuple[ImageCandidate | None, str]:
        data = dict(raw)
        image_url = str(data.get("image_url") or data.get("url") or "")
        news_id = str(data.get("news_id") or data.get("newsId") or "")
        stable_id = str(data.get("id") or (candidate_id_for(image_url, news_id) if image_url else _stable_fallback(data, "image")))
        if not news_id:
            return None, stable_id
        score_data = data.get("score") if isinstance(data.get("score"), dict) else {}
        total = _clamp(score_data.get("total", data.get("total", 0.0)), 0.0, 100.0)
        raw_signals = data.get("signals") if isinstance(data.get("signals"), dict) else {}
        evidence = []
        if isinstance(data.get("evidence"), list):
            for item in data["evidence"]:
                try:
                    evidence.append(ImageEvidence.model_validate(item))
                except (TypeError, ValueError):
                    continue
        score = {
            "relevance": _clamp(score_data.get("relevance", data.get("relevance", total / 100.0)), 0.0, 1.0),
            "freshness": _clamp(score_data.get("freshness", 0.0), 0.0, 1.0),
            "source_trust": _clamp(score_data.get("source_trust", 0.0), 0.0, 1.0),
            "quality": _clamp(score_data.get("quality", data.get("quality", 0.0)), 0.0, 1.0),
            "duplicate_penalty": _clamp(score_data.get("duplicate_penalty", 0.0), 0.0, 1.0),
            "risk_penalty": _clamp(score_data.get("risk_penalty", 0.0), 0.0, 1.0),
            "type_match": _clamp(score_data.get("type_match", 0.0), 0.0, 1.0),
            "total": total,
        }
        payload = {
            "id": stable_id,
            "news_id": news_id,
            "image_url": image_url or f"legacy://{stable_id}",
            "source_url": str(data.get("source_url") or data.get("source") or f"legacy://{stable_id}"),
            "source_type": data.get("source_type", SourceType.UNKNOWN.value),
            "image_type": data.get("image_type", ImageType.UNKNOWN.value),
            "image_type_confidence": _clamp(data.get("image_type_confidence", 0.0), 0.0, 1.0),
            "published_at": _parse_datetime(data["published_at"]) if data.get("published_at") else None,
            "fetched_at": _parse_datetime(data.get("fetched_at")),
            "width": data.get("width"),
            "height": data.get("height"),
            "mime_type": data.get("mime_type"),
            "byte_size": data.get("byte_size"),
            "sha256": data.get("sha256"),
            "perceptual_hash": data.get("perceptual_hash"),
            "downloadable": bool(data.get("downloadable", False)),
            "local_path": str(data["local_path"]) if data.get("local_path") else None,
            "original_path": str(data["original_path"]) if data.get("original_path") else None,
            "original_mime_type": data.get("original_mime_type"),
            "original_byte_size": data.get("original_byte_size"),
            "original_sha256": data.get("original_sha256"),
            "original_width": data.get("original_width"),
            "original_height": data.get("original_height"),
            "output_mime_type": data.get("output_mime_type"),
            "output_byte_size": data.get("output_byte_size"),
            "output_sha256": data.get("output_sha256"),
            "output_width": data.get("output_width"),
            "output_height": data.get("output_height"),
            "news_source_url": data.get("news_source_url"),
            "parent_source_url": data.get("parent_source_url"),
            "image_alt": data.get("image_alt"),
            "nearby_text": data.get("nearby_text"),
            "evidence": evidence,
            "download_status": data.get("download_status", "pending"),
            "download_error_code": data.get("download_error_code"),
            "downloaded_url": data.get("downloaded_url"),
            "media_source_url": data.get("media_source_url"),
            "expected_width": data.get("expected_width"),
            "expected_height": data.get("expected_height"),
            "selection_reasons": data.get("selection_reasons", []),
            "animated_source": bool(data.get("animated_source", False)),
            "animation_frame_index": data.get("animation_frame_index"),
            "review_reasons": [str(item) for item in data.get("review_reasons", []) if item is not None] if isinstance(data.get("review_reasons", []), list) else [],
            "signals": raw_signals,
            "score": score,
            "selected": bool(data.get("selected", False)),
            "curation_status": data.get("curation_status", raw_signals.get("candidate_status", "unselected")),
        }
        try:
            candidate = ImageCandidate.model_validate(payload)
        except (TypeError, ValueError):
            try:
                payload["width"] = int(payload["width"]) if payload["width"] is not None else None
                payload["height"] = int(payload["height"]) if payload["height"] is not None else None
                payload["byte_size"] = int(payload["byte_size"]) if payload["byte_size"] is not None else None
                candidate = ImageCandidate.model_validate(payload)
            except (TypeError, ValueError):
                return None, stable_id
        # Domain models derive IDs from URLs.  Legacy indexes may have an
        # explicit stable ID, which must remain the review identity.
        object.__setattr__(candidate, "id", stable_id)
        return candidate, stable_id

    @classmethod
    def _coerce_video(cls, raw: dict[str, Any]) -> tuple[VideoCandidate | None, str]:
        data = dict(raw)
        video_url = str(data.get("video_url") or data.get("url") or "")
        stable_id = str(data.get("id") or (video_candidate_id_for(video_url) if video_url else _stable_fallback(data, "video")))
        news_id = str(data.get("news_id") or data.get("newsId") or "")
        if not news_id:
            return None, stable_id
        payload = {
            "id": stable_id,
            "news_id": news_id,
            "source_url": str(data.get("source_url") or f"legacy://{stable_id}"),
            "video_url": video_url or f"legacy://{stable_id}",
            "title": str(data.get("title") or ""),
            "uploader": str(data.get("uploader") or ""),
            "duration_seconds": data.get("duration_seconds"),
            "width": data.get("width"),
            "height": data.get("height"),
            "format": data.get("format"),
            "mime_type": data.get("mime_type"),
            "byte_size": data.get("byte_size"),
            "sha256": data.get("sha256"),
            "local_path": str(data["local_path"]) if data.get("local_path") else None,
            "downloadable": bool(data.get("downloadable", False)),
            "review_reasons": [str(item) for item in data.get("review_reasons", []) if item is not None] if isinstance(data.get("review_reasons", []), list) else [],
            "status": data.get("status", VideoStatus.DISCOVERED.value),
            "failure_reason": data.get("failure_reason"),
            "discovered_via": data.get("discovered_via", "unknown"),
        }
        try:
            candidate = VideoCandidate.model_validate(payload)
        except (TypeError, ValueError):
            return None, stable_id
        object.__setattr__(candidate, "id", stable_id)
        return candidate, stable_id

    def _initialize_decisions(self) -> None:
        for candidate in self.images:
            self._decisions[str(candidate.id)] = self._initial_image_decision(candidate)
        for candidate in self.videos:
            self._decisions[str(candidate.id)] = self._initial_video_decision(candidate)
        state_payload = self._read_state_payload()
        self._loaded_state_payload = dict(state_payload)
        self.revision = int(state_payload.get("revision", 0))
        persisted = state_payload.get("decisions", {})
        if isinstance(state_payload, dict) and isinstance(state_payload.get("manual_decisions"), list):
            self._manual_decisions = {str(value) for value in state_payload["manual_decisions"]}
        for media_id, value in persisted.items():
            if media_id in self._decisions and media_id not in self._ambiguous_image_ids:
                try:
                    self._decisions[media_id] = ReviewDecision(value)
                except ValueError:
                    continue
        # Persist initial state so a fresh task has a durable review contract.
        # The SQLite file also contains pipeline state and can exist before
        # the first review decision has been saved.
        if self.state_path.suffix == ".sqlite3":
            from ..tasks.repository import TaskRepository
            missing_state = TaskRepository(self.state_path.parent, create=False).load_review_state() is None
        else:
            missing_state = not self.state_path.exists()
        if missing_state:
            self._persist()

    def _usable(self, candidate: ImageCandidate | VideoCandidate) -> bool:
        source = self._source_paths.get(str(candidate.id))
        return source is not None and source.is_file()

    def _hard_filtered(self, candidate: ImageCandidate) -> bool:
        signals = candidate.signals if isinstance(candidate.signals, dict) else {}
        # Semantic type and editorial quality may reject auto-selection, but
        # must not hide an otherwise usable local image from a human reviewer.
        return signals.get("invalid_file") is True

    def _initial_image_decision(self, candidate: ImageCandidate) -> ReviewDecision:
        if self._hard_filtered(candidate):
            return ReviewDecision.REJECTED
        if bool(candidate.selected) and self._usable(candidate):
            return ReviewDecision.ACCEPTED
        if self._usable(candidate):
            return ReviewDecision.PENDING
        return ReviewDecision.REJECTED

    def _initial_video_decision(self, candidate: VideoCandidate) -> ReviewDecision:
        if self._usable(candidate) and candidate.status is VideoStatus.DOWNLOADED:
            return ReviewDecision.ACCEPTED
        if self._usable(candidate):
            return ReviewDecision.PENDING
        return ReviewDecision.REJECTED

    def _read_state(self) -> dict[str, str]:
        return self._read_state_payload().get("decisions", {})

    def _read_state_payload(self) -> dict[str, Any]:
        if self.state_path.suffix == ".sqlite3":
            from ..tasks.repository import TaskRepository
            return TaskRepository(self.state_path.parent, create=False).load_review_state() or {"schema_version": 1, "decisions": {}, "manual_decisions": []}
        if not self.state_path.exists():
            return {"schema_version": 1, "decisions": {}, "manual_decisions": []}
        payload = self._read_required_json(self.state_path)
        if not isinstance(payload, dict):
            raise ValueError(f"审核状态格式错误：{self.state_path} 应为 JSON 对象")
        values = payload.get("decisions", payload if "manual_decisions" not in payload else {})
        manual = payload.get("manual_decisions", [])
        if (not isinstance(values, dict) or not isinstance(manual, list)
                or any(not isinstance(value, (str, int)) for value in manual)
                or not isinstance(payload.get("revision", 0), int) or payload.get("revision", 0) < 0):
            raise ValueError(f"审核状态格式错误：{self.state_path} 的 decisions/manual_decisions 字段无效")
        for key, value in values.items():
            if str(value) not in {decision.value for decision in ReviewDecision}:
                raise ValueError(f"审核状态包含无效决策：{key}={value!r}")
        if "decisions" not in payload and "manual_decisions" not in payload:
            return {"schema_version": 1, "decisions": dict(values), "manual_decisions": []}
        return dict(payload)

    def _persist(self) -> None:
        existing = self._read_state_payload()
        saved = existing.get("decisions", {})
        decisions = dict(saved) if isinstance(saved, dict) else {}
        # A review session may load retry batches one at a time. Preserve
        # decisions for IDs from batches that have not been merged yet.
        decisions.update({key: value.value for key, value in self._decisions.items()})
        manual = existing.get("manual_decisions", [])
        manual_decisions = set(map(str, manual)) if isinstance(manual, list) else set()
        manual_decisions.update(self._manual_decisions)
        payload = {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "decisions": {str(key): str(value) for key, value in sorted(decisions.items())},
                "manual_decisions": sorted(manual_decisions),
                "revision": self.revision,
            }
        self.persist_state_payload(self.state_path, payload)
        self._loaded_state_payload = payload

    @staticmethod
    def persist_state_payload(path: Path | str, payload: dict[str, Any]) -> None:
        path = Path(path)
        if path.suffix == ".sqlite3":
            from ..tasks.repository import TaskRepository
            TaskRepository(path.parent, create=False).save_review_state(payload)
            return
        # A corrupted state must never be replaced by an empty/default snapshot.
        # This check runs in the writer thread for desktop callers.
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                existing = json.load(handle)
            if not isinstance(existing, dict):
                raise ValueError("审核状态文件结构损坏，已保留原文件")
            values = existing.get("decisions", existing)
            if (not isinstance(values, dict) or not isinstance(existing.get("manual_decisions", []), list)
                    or any(not isinstance(value, (str, int)) for value in existing.get("manual_decisions", []))
                    or not isinstance(existing.get("revision", 0), int) or existing.get("revision", 0) < 0
                    or any(str(value) not in {d.value for d in ReviewDecision} for value in values.values())):
                raise ValueError("审核状态包含损坏的决定记录，已保留原文件")
        atomic_json_write(Path(path), payload, ensure_parent=True)

    def decision_state_payload(self) -> dict[str, Any]:
        payload = dict(self._loaded_state_payload)
        decisions = dict(payload.get("decisions", {}))
        decisions.update({key: value.value for key, value in self._decisions.items()})
        manual = set(map(str, payload.get("manual_decisions", []))) | self._manual_decisions
        payload.update({"schema_version": 1, "decisions": decisions, "manual_decisions": sorted(manual),
                        "revision": self.revision, "updated_at": datetime.now(timezone.utc).isoformat()})
        return payload

    def query_snapshot(self) -> "ReviewSession":
        """Create a lightweight query-only view with independent collections/maps."""
        snap = object.__new__(ReviewSession)
        snap.__dict__ = self.__dict__.copy()
        for name in ("images", "videos", "news_items"):
            setattr(snap, name, list(getattr(self, name)))
        for name in ("_source_paths", "_raw_types", "_decisions", "_image_by_id", "_video_by_id"):
            setattr(snap, name, dict(getattr(self, name)))
        for name in ("_review_ids", "_failed_ids", "_ambiguous_image_ids", "_manual_decisions"):
            setattr(snap, name, set(getattr(self, name)))
        snap._pending_cache = dict(self._pending_cache)
        snap._comparison_cache = self._comparison_cache
        return snap

    def decision(self, media_id: str) -> ReviewDecision:
        return self._decisions.get(str(media_id), ReviewDecision.REJECTED)

    def scope_repository_query(self, news_id: str | None, allowed_news: set[str] | None = None) -> None:
        """Narrow a worker-owned snapshot using bounded database reads.

        Decisions and candidate objects remain those captured by the GUI, so
        a background fetch never replaces an unsaved human decision. New rows
        written by a grab operation become visible only after its delta merge.
        Legacy snapshots keep their existing in-memory query behavior.
        """
        if self.state_path.suffix != ".sqlite3":
            return
        from ..tasks.repository import TaskRepository
        repository = TaskRepository(self.state_path.parent, create=False)
        targets = ([news_id] if news_id is not None
                   else sorted(allowed_news) if allowed_news is not None else [None])
        identifiers: dict[str, set[str]] = {"image": set(), "video": set()}
        for kind in identifiers:
            for target in targets:
                if allowed_news is not None and target is not None and target not in allowed_news:
                    continue
                identifiers[kind].update(str(value) for value in repository.iter_candidate_ids(
                    news_id=target, kind=kind, batch_size=100))
        def included(candidate, kind):
            if news_id is not None and candidate.news_id != news_id:
                return False
            if allowed_news is not None and candidate.news_id not in allowed_news:
                return False
            # Imported legacy batches may still live in the explicitly bound
            # private retry directory. Keep their merged snapshot candidates.
            return (str(candidate.id) in identifiers[kind]
                    or str(candidate.id) not in self._repository_candidate_ids)
        self.images = [candidate for candidate in self.images if included(candidate, "image")]
        self.videos = [candidate for candidate in self.videos if included(candidate, "video")]

    def set_decision(self, media_id: str, decision: ReviewDecision | str) -> None:
        self.set_decisions([media_id], decision)

    def set_decisions(self, media_ids: Iterable[str], decision: ReviewDecision | str, *, persist: bool = True) -> None:
        selected = decision if isinstance(decision, ReviewDecision) else ReviewDecision(decision)
        ids = [str(value) for value in media_ids]
        missing = next((value for value in ids if value not in self._decisions), None)
        if missing is not None:
            raise KeyError(missing)
        changed_news: set[str] = set()
        changed = False
        for media_id in ids:
            if media_id not in self._manual_decisions:
                changed = True
            if self._decisions.get(media_id) is not selected:
                self._decisions[media_id] = selected
                changed = True
            self._manual_decisions.add(media_id)
            candidate = self._image_by_id.get(media_id)
            if candidate is not None:
                changed_news.add(str(candidate.news_id))
        if changed:
            self.revision += 1
            for news_id in changed_news:
                self._pending_cache.pop(news_id, None)
            if persist:
                self._persist()

    def _sorted_pending(self, values: Iterable[ImageCandidate]) -> list[ImageCandidate]:
        def key(candidate: ImageCandidate):
            score = candidate.score
            return (
                not (candidate.signals.get("x_api_photo") is True or candidate.signals.get("socialdata_photo") is True),
                -(score.total if score else 0.0),
                -(score.source_trust if score else 0.0),
                -((candidate.width or 0) * (candidate.height or 0)),
                str(candidate.id),
            )

        return sorted(values, key=key)

    def pending_images(self, news_id: str | None = None) -> list[ImageCandidate]:
        groups: dict[str, list[ImageCandidate]] = {}
        for item in self.images:
            groups.setdefault(str(item.news_id), []).append(item)
        news_ids = [str(news_id)] if news_id is not None else list(groups)
        output: list[ImageCandidate] = []
        for current_news in news_ids:
            candidates = groups.get(current_news, [])
            signature = tuple((str(item.id), self.decision(str(item.id)).value, item.sha256, item.perceptual_hash,
                               item.width, item.height, item.output_sha256, item.local_path, item.original_path,
                               item.image_type.value if hasattr(item.image_type, "value") else str(item.image_type)) for item in candidates)
            cached = self._pending_cache.get(current_news)
            if cached is None or cached[0] != signature:
                values = [item for item in candidates if self.decision(str(item.id)) is ReviewDecision.PENDING]
                accepted = [item for item in candidates if self.decision(str(item.id)) is ReviewDecision.ACCEPTED]
                result = tuple(self._sorted_pending(reviewable_pending_images(values, accepted=accepted, comparison_cache=self._comparison_cache)))
                self._pending_cache[current_news] = (signature, result)
            output.extend(self._pending_cache[current_news][1])
        return self._sorted_pending(output)

    def failed_images(self, news_id: str | None = None) -> list[ImageCandidate]:
        values = [candidate for candidate in self.pending_images(news_id) if str(candidate.id) in self._failed_ids or bool(candidate.review_reasons)]
        return values

    def accepted_images(self, news_id: str | None = None) -> list[ImageCandidate]:
        values = [candidate for candidate in self.images if self.decision(str(candidate.id)) is ReviewDecision.ACCEPTED]
        return [candidate for candidate in values if news_id is None or candidate.news_id == news_id]

    def rejected_images(self, news_id: str | None = None) -> list[ImageCandidate]:
        values = [candidate for candidate in self.images if self.decision(str(candidate.id)) is ReviewDecision.REJECTED]
        return [candidate for candidate in values if news_id is None or candidate.news_id == news_id]

    def pending_videos(self, news_id: str | None = None) -> list[VideoCandidate]:
        values = [candidate for candidate in self.videos if self.decision(str(candidate.id)) is ReviewDecision.PENDING]
        return [candidate for candidate in values if news_id is None or candidate.news_id == news_id]

    def accepted_videos(self, news_id: str | None = None) -> list[VideoCandidate]:
        values = [candidate for candidate in self.videos if self.decision(str(candidate.id)) is ReviewDecision.ACCEPTED]
        return [candidate for candidate in values if news_id is None or candidate.news_id == news_id]

    def _confined_retry_path(self, news_id: str, attempt_id: str) -> Path:
        """Resolve a retry path while rejecting traversal and symlink escapes."""

        if self.task_root is None and self.retry_root is None:
            raise ValueError("retry merge requires a task_root")
        components = (str(news_id), str(attempt_id))
        for component in components:
            value = Path(component)
            if not component or value.is_absolute() or value.name != component or component in {".", ".."}:
                raise ValueError("retry path must be a single confined component")
        retries_root = Path(self.retry_root).resolve() if self.retry_root is not None else (Path(self.task_root) / "retries").resolve()
        lexical = retries_root / components[0] / components[1]
        resolved = lexical.resolve()
        try:
            resolved.relative_to(retries_root)
        except ValueError as exc:
            raise ValueError("retry path escapes task_root/retries") from exc
        return resolved

    def prepare_retry_delta(self, news_id: str, attempt_id: str, *, legacy_attempt: Any = None) -> RetryDelta:
        """Read, validate and decode a retry attempt without changing this session."""
        news_id, attempt_id = str(news_id), str(attempt_id)
        root = self._confined_retry_path(news_id, attempt_id)
        from ..tasks.repository import TaskRepository
        repository = (TaskRepository(self.task_root, create=False)
                      if self.task_root is not None and TaskRepository.is_authoritative(self.task_root) else None)
        batch = repository.read_batch(news_id, attempt_id) if repository is not None else None
        if batch is None and legacy_attempt is not None and getattr(legacy_attempt, "path", None):
            proposed = Path(legacy_attempt.path).resolve()
            for legacy_root in self.legacy_retry_roots:
                confined_root = Path(legacy_root).resolve()
                expected = (confined_root / news_id / attempt_id).resolve()
                try:
                    expected.relative_to(confined_root)
                except ValueError:
                    continue
                if proposed == expected:
                    root = expected
                    break
        if batch is None and not root.is_dir():
            raise FileNotFoundError(root)
        from ..diagnostics import diagnose_error
        diagnostics: list[Any] = []
        try:
            # Older synchronous callers supplied a validated RetryAttempt in
            # memory. Preserve that interface; a present manifest always wins
            # and a corrupt file must never be bypassed by an in-memory copy.
            image_payload = dict(batch, candidates=batch["images"]) if batch else (self._read_required_json(root / "image_index.json")
                             if (root / "image_index.json").exists() or legacy_attempt is None
                             else {"candidates": legacy_attempt.images})
            video_payload = {"videos": batch["videos"]} if batch else (self._read_required_json(root / "video_index.json") if (root / "video_index.json").exists()
                             else {"videos": legacy_attempt.videos if legacy_attempt is not None else []})
        except ValueError as exc:
            raise ValueError(f"补抓批次索引损坏：{exc}") from exc
        for payload, keys in ((image_payload, ("candidates", "images", "image_candidates")),
                              (video_payload, ("videos", "video_candidates", "candidates"))):
            if not isinstance(payload, (dict, list)):
                raise ValueError("批次索引结构错误，应为 JSON 对象或旧版列表")
            if isinstance(payload, dict):
                for key in keys:
                    if key in payload and not isinstance(payload[key], list):
                        raise ValueError(f"批次索引字段 {key} 应为列表")
        def rows(payload, keys):
            values = payload if isinstance(payload, list) else next((payload[k] for k in keys if k in payload), [])
            for raw in values:
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("候选记录不是 JSON 对象")
                    for key in ("local_path", "original_path", "output_path"):
                        value = raw.get(key)
                        if value:
                            path = Path(value)
                            path = path if path.is_absolute() else root / path
                            if self._confine_retry_asset(path) is None:
                                raise ValueError(f"候选资产路径超出本任务范围：{key}")
                    yield raw
                except (OSError, ValueError, TypeError) as exc:
                    diagnostics.append(diagnose_error(exc, stage="校验补抓候选", source=str(root),
                        candidate_id=str(raw.get("id", "")) if isinstance(raw, dict) else None))
        prepared_images: list[_PreparedAsset] = []
        for raw in rows(image_payload, ("candidates", "images", "image_candidates")):
            candidate, stable_id = self._coerce_image(raw)
            if candidate is None or str(candidate.news_id) != news_id:
                diagnostics.append(diagnose_error(ValueError("图片记录无效或新闻归属不符"), stage="校验补抓候选", candidate_id=stable_id))
                continue
            source = self._confine_retry_asset(self._resolve_image_path(candidate, root))
            if source is not None:
                expected = (candidate.original_sha256 or candidate.sha256
                            if candidate.original_path and source == Path(candidate.original_path).resolve()
                            else candidate.output_sha256 or candidate.sha256)
                try:
                    if expected and hashlib.sha256(source.read_bytes()).hexdigest().casefold() != str(expected).casefold():
                        raise ValueError("hash mismatch")
                    with Image.open(source) as image:
                        image.verify()
                    with Image.open(source) as image:
                        image.load()
                except Exception as exc:
                    diagnostics.append(diagnose_error(exc, stage="校验图片", candidate_id=stable_id, source=str(source)))
                    source = None
            if source is None:
                diagnostics.append(diagnose_error(FileNotFoundError("审阅图缺失或校验失败，媒体链接已保留"),
                                   stage="校验图片", candidate_id=stable_id, source=str(candidate.image_url)))
                object.__setattr__(candidate, "local_path", None)
                object.__setattr__(candidate, "original_path", None)
            else:
                object.__setattr__(candidate, "local_path", str(source))
            prepared_images.append(_PreparedAsset(candidate, str(raw.get("image_type", "")).casefold(), source))
        prepared_videos: list[_PreparedAsset] = []
        for raw in rows(video_payload, ("videos", "video_candidates", "candidates")):
            candidate, stable_id = self._coerce_video(raw)
            if candidate is None or str(candidate.news_id) != news_id:
                diagnostics.append(f"skipped invalid retry video {stable_id}")
                continue
            source = self._confine_retry_asset(self._resolve_asset_path(raw.get("local_path") or raw.get("original_path"), root, candidate.sha256))
            if source is not None and not source.is_file():
                source = None
            if source is None:
                object.__setattr__(candidate, "local_path", None)
                diagnostics.append(f"retry video {stable_id} asset unavailable")
            else:
                object.__setattr__(candidate, "local_path", str(source))
            prepared_videos.append(_PreparedAsset(candidate, str(raw.get("media_type", "")).casefold(), source))
        failure_path = root / "failed_items.json"
        failure_payload = []
        if failure_path.exists():
            try:
                failure_payload = self._read_required_json(failure_path)
                if not isinstance(failure_payload, (dict, list)):
                    raise ValueError("批次失败记录应为 JSON 对象或列表")
                if isinstance(failure_payload, dict) and not isinstance(failure_payload.get("failures", []), list):
                    raise ValueError("批次 failures 字段应为列表")
            except (OSError, ValueError, TypeError) as exc:
                diagnostics.append(diagnose_error(exc, stage="读取批次失败记录", source=str(failure_path)))
                failure_payload = []
        failures = tuple(_items(failure_payload, ("failures",)))
        if batch is not None:
            failures = tuple(batch.get("failures", []))
        if legacy_attempt is not None and not (root / "failed_items.json").exists():
            failures = tuple(legacy_attempt.failures)
        metadata = image_payload if isinstance(image_payload, dict) else {}
        kind = metadata.get("kind", "retry")
        status = metadata.get("status", "completed")
        if kind not in {"retry", "supplement"} or status not in {"running", "completed", "partial", "failed", "cancelled"}:
            raise ValueError("批次抓取类型或状态无效")
        return RetryDelta(news_id, attempt_id, tuple(prepared_images), tuple(prepared_videos), failures, tuple(diagnostics), kind, status)

    def apply_retry_delta(self, delta: RetryDelta, *, force_pending: bool = False, persist: bool = True) -> RetryMergeResult:
        added_images: list[str] = []
        added_videos: list[str] = []
        changed_news: set[str] = set()
        recovered: set[str] = set()
        supplement = force_pending or delta.kind == "supplement"
        image_urls = {(str(item.news_id), str(item.image_url)) for item in self.images}
        for entry in delta.images:
            candidate, source = entry.candidate, entry.source
            stable_id = str(candidate.id)
            old = self._image_by_id.get(stable_id)
            if old is not None:
                old_source = self._source_paths.get(stable_id)
                if source is not None and (old_source is None or not old_source.is_file()):
                    self.images[self.images.index(old)] = candidate
                    self._image_by_id[stable_id] = candidate
                    self._source_paths[stable_id] = source
                    self._raw_types[stable_id] = entry.raw_type
                    recovered.add(stable_id); changed_news.add(str(candidate.news_id))
                    if supplement and stable_id not in self._manual_decisions:
                        self._decisions[stable_id] = ReviewDecision.PENDING
                continue
            if (str(candidate.news_id), str(candidate.image_url)) in image_urls:
                continue
            image_urls.add((str(candidate.news_id), str(candidate.image_url)))
            self.images.append(candidate); self._image_by_id[stable_id] = candidate
            self._raw_types[stable_id] = entry.raw_type
            if source is not None: self._source_paths[stable_id] = source
            self._review_ids.add(stable_id)
            saved = self._loaded_state_payload.get("decisions", {}).get(stable_id)
            initial = (ReviewDecision.REJECTED if source is None or self._hard_filtered(candidate)
                       else ReviewDecision.PENDING if supplement
                       else ReviewDecision.ACCEPTED if candidate.selected
                       else ReviewDecision.PENDING)
            self._decisions[stable_id] = ReviewDecision(saved) if saved else initial
            added_images.append(stable_id); changed_news.add(str(candidate.news_id))
        for entry in delta.videos:
            candidate, source = entry.candidate, entry.source
            stable_id = str(candidate.id)
            old = self._video_by_id.get(stable_id)
            if old is not None:
                old_source = self._source_paths.get(stable_id)
                if source is not None and (old_source is None or not old_source.is_file()):
                    self.videos[self.videos.index(old)] = candidate; self._video_by_id[stable_id] = candidate
                    self._source_paths[stable_id] = source; recovered.add(stable_id); changed_news.add(str(candidate.news_id))
                continue
            self.videos.append(candidate); self._video_by_id[stable_id] = candidate
            if source is not None: self._source_paths[stable_id] = source
            saved = self._loaded_state_payload.get("decisions", {}).get(stable_id)
            self._decisions[stable_id] = ReviewDecision(saved) if saved else (ReviewDecision.PENDING if source is not None and supplement else (ReviewDecision.ACCEPTED if source is not None and candidate.status is VideoStatus.DOWNLOADED else ReviewDecision.PENDING if source is not None else ReviewDecision.REJECTED))
            added_videos.append(stable_id); changed_news.add(str(candidate.news_id))
        for failure in delta.failures:
            value = failure.get("candidate_id") or failure.get("id") or failure.get("media_id")
            if value is not None: self._failed_ids.add(str(value))
        self._failed_ids.difference_update(recovered)
        if changed_news:
            self.revision += 1
            self._comparison_cache.invalidate(changed_news)
            for value in changed_news: self._pending_cache.pop(value, None)
            if persist: self._persist()
        return RetryMergeResult(delta.news_id, delta.attempt_id, added_images, added_videos, list(delta.diagnostics))

    def merge_retry_attempt(self, retry_or_news_id: Any, attempt_id: str | None = None, *, force_pending: bool = False, persist: bool = True) -> RetryMergeResult:
        """Merge one retry attempt in memory while retaining raw indexes.

        ``retry_or_news_id`` may be an attempt directory, a ``RetryAttempt``
        returned by ``TaskStore``, or a news ID paired with ``attempt_id``.
        Existing stable IDs always win; only new records are appended.
        """

        # This compatibility entry point may do disk work. Desktop result
        # handling uses apply_retry_delta directly and never rereads state.
        self._loaded_state_payload = self._read_state_payload()
        self._manual_decisions.update(map(str, self._loaded_state_payload.get("manual_decisions", [])))
        if hasattr(retry_or_news_id, "images") and hasattr(retry_or_news_id, "videos"):
            delta = self.prepare_retry_delta(str(retry_or_news_id.news_id), str(retry_or_news_id.attempt_id), legacy_attempt=retry_or_news_id)
            if getattr(retry_or_news_id, "kind", "retry") == "supplement":
                delta = RetryDelta(delta.news_id, delta.attempt_id, delta.images, delta.videos, delta.failures, delta.diagnostics, "supplement")
        elif attempt_id is not None:
            delta = self.prepare_retry_delta(str(retry_or_news_id), str(attempt_id))
        else:
            raw_path = Path(retry_or_news_id).expanduser()
            if raw_path.is_absolute():
                raise ValueError("retry path must be relative to task_root/retries")
            retries_root = Path(self.retry_root).resolve() if self.retry_root is not None else (Path(self.task_root) / "retries").resolve()
            root = raw_path.resolve() if raw_path.is_absolute() else (retries_root / raw_path).resolve()
            try:
                relative = root.relative_to(retries_root)
            except ValueError as exc:
                raise ValueError("retry path escapes task_root/retries") from exc
            if len(relative.parts) != 2:
                raise ValueError("retry path must identify a news and attempt")
            delta = self.prepare_retry_delta(relative.parts[0], relative.parts[1])
        return self.apply_retry_delta(delta, force_pending=force_pending, persist=persist)

    def _confine_retry_asset(self, source: Path | None) -> Path | None:
        if source is None:
            return None
        asset_root = (Path(self.retry_root).resolve().parent if self.retry_root is not None
                      else Path(self.task_root).resolve() if self.task_root is not None else None)
        if asset_root is None:
            return None
        try:
            resolved = source.resolve()
        except OSError:
            return None
        for allowed_root in (asset_root, *self.legacy_retry_roots):
            try:
                resolved.relative_to(Path(allowed_root).resolve())
                return resolved
            except ValueError:
                continue
        return None

    def _folder_names(self) -> dict[str, str]:
        # Use the same canonical naming policy as raw output.  Manifest order
        # is the editorial appearance order; sorting by global sequence would
        # renumber categories when legacy manifests contain non-monotonic IDs.
        names = canonical_item_names(self.news_items)
        counters = {
            prefix: sum(name.startswith(prefix) for name in names.values())
            for prefix in ("x", "h", "z")
        }
        for candidate in [*self.images, *self.videos]:
            if str(candidate.news_id) not in names:
                # A media record without a corresponding news row belongs to
                # the catch-all weekly namespace, never implicitly to 新作.
                counters["z"] += 1
                names[str(candidate.news_id)] = f"z{counters['z']}"
        return names

    def _source_for(self, candidate: ImageCandidate | VideoCandidate) -> Path | None:
        source = self._source_paths.get(str(candidate.id))
        if source is not None and source.is_file():
            return source
        local = getattr(candidate, "local_path", None)
        return self._source_path(local, self.output_dir)

    @staticmethod
    def _unique_target(target: Path, source: Path) -> Path:
        if not target.exists() or target.resolve() == source.resolve():
            return target
        stem, suffix = target.stem, target.suffix
        index = 2
        while True:
            candidate = target.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists() or candidate.resolve() == source.resolve():
                return candidate
            index += 1

    @staticmethod
    def _verified_final_image(candidate: ImageCandidate, source: Path) -> tuple[bytes, str] | None:
        try:
            data = source.read_bytes()
            with Image.open(source) as image:
                actual = (image.format or "").upper()
                image.verify()
            mime = "image/png" if actual == "PNG" else "image/jpeg" if actual == "JPEG" else None
            extension_matches = source.suffix.casefold() in ({".png"} if actual == "PNG" else {".jpg", ".jpeg"} if actual == "JPEG" else set())
            metadata_matches = candidate.output_mime_type == mime and extension_matches
            hash_matches = not candidate.output_sha256 or hashlib.sha256(data).hexdigest() == candidate.output_sha256
            if mime and metadata_matches and hash_matches:
                return data, mime
            converted = ImageConverter().convert(data, candidate)
            return converted.data, converted.mime_type
        except ImageConversionError:
            return None
        except Exception:
            return None

    def export_final(self, *, include_pending: bool = True) -> dict[str, Any]:
        final_root = self.task_root / "final"
        final_root.mkdir(parents=True, exist_ok=True)
        published = final_root / "images"
        image_root = final_root / f".images-stage-{uuid.uuid4().hex}"
        image_root.mkdir()
        backup = final_root / f".images-backup-{uuid.uuid4().hex}"
        if published.is_symlink():
            image_root.rmdir()
            raise ValueError("图片交付目录不能是符号链接")
        previous = _read_json(final_root / "final_manifest.json", {})
        previous_files = previous.get("files", []) if isinstance(previous, dict) else []
        owned = {value for value in previous_files if isinstance(value, str)} if isinstance(previous_files, list) else set()
        committed = False
        # Preserve user-added files. Only the files named by the previous
        # manifest are regenerated; repeated export never adds numbered copies.
        if published.exists():
            for source in published.rglob("*"):
                if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(published.resolve()):
                    continue
                relative = source.relative_to(published)
                if (Path("images") / relative).as_posix() not in owned:
                    target = image_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        try:
            manifest = self._export_images_and_videos(image_root, include_pending=include_pending)
            if published.exists():
                try:
                    atomic_replace(published, backup)
                except PermissionError:
                    # Explorer/preview readers can lock the directory even
                    # when individual files can still be replaced safely.
                    self._publish_files_in_place(image_root, published, backup, owned, manifest)
                    committed = True
                    if not include_pending:
                        # In-place publishing removes old managed files, but
                        # can leave their now-empty pending directories behind.
                        pending_dirs = {Path(value).parent for value in owned if Path(value).parent.name == "未候选"}
                        for relative in pending_dirs:
                            try:
                                (final_root / relative).rmdir()
                            except OSError:
                                pass  # Preserve user-added files or occupied directories.
                    return manifest
            try:
                atomic_replace(image_root, published)
                self._atomic_json(final_root / "final_manifest.json", manifest)
                committed = True
            except Exception:
                if backup.exists():
                    if published.exists():
                        atomic_replace(published, image_root)
                    atomic_replace(backup, published)
                elif published.exists():
                    atomic_replace(published, image_root)
                raise
            return manifest
        finally:
            # If rollback itself failed, preserve both directories for recovery.
            cleanup = (image_root, backup) if committed else (image_root,) if not backup.exists() else ()
            for temporary in cleanup:
                if temporary.exists() and not temporary.is_symlink() and temporary.resolve().is_relative_to(final_root.resolve()):
                    shutil.rmtree(temporary, ignore_errors=True)

    def _publish_files_in_place(
        self, staged: Path, published: Path, backup: Path,
        owned: set[str], manifest: dict[str, Any],
    ) -> None:
        """Replace files transactionally without renaming the open directory."""
        backup.mkdir()
        manifest_path = published.parent / "final_manifest.json"
        had_manifest = manifest_path.is_file()
        if had_manifest:
            shutil.copyfile(manifest_path, backup / "final_manifest.json")
        updates = {path.relative_to(staged): path for path in staged.rglob("*") if path.is_file()}
        obsolete: set[Path] = set()
        for value in owned:
            path = Path(value)
            if path.is_absolute() or path.drive or ".." in path.parts or not path.parts or path.parts[0] != "images":
                raise ValueError("旧导出清单包含不安全的图片路径")
            relative = Path(*path.parts[1:])
            if relative.parts and relative not in updates:
                obsolete.add(relative)
        changed: list[tuple[Path, bool]] = []
        try:
            for relative in sorted(set(updates) | obsolete, key=lambda value: value.as_posix()):
                target = published / relative
                # Do not traverse links/junctions or touch files outside this task.
                if not target.resolve().is_relative_to(published.resolve()):
                    raise ValueError("图片导出路径超出当前任务")
                for part in (target, *target.parents):
                    if part == published.parent:
                        break
                    if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                        raise ValueError("图片导出路径不能包含链接或目录联接")
                source = updates.get(relative)
                existed = target.is_file()
                if source is not None and existed and filecmp.cmp(source, target, shallow=False):
                    continue
                if target.exists() and not existed:
                    raise ValueError(f"图片文件位置被目录占用：{target}")
                if existed:
                    saved = backup / "files" / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, saved)
                if source is None:
                    if existed:
                        target.unlink()
                        changed.append((relative, existed))
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace(source, target)
                    changed.append((relative, existed))
            self._atomic_json(manifest_path, manifest)
        except Exception as error:
            rollback_errors: list[str] = []
            for relative, existed in reversed(changed):
                target = published / relative
                try:
                    if existed:
                        restore = backup / f".restore-{uuid.uuid4().hex}"
                        shutil.copyfile(backup / "files" / relative, restore)
                        atomic_replace(restore, target)
                    elif target.exists():
                        target.unlink()
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            try:
                if had_manifest:
                    saved_manifest = backup / "final_manifest.json"
                    if not manifest_path.is_file() or not filecmp.cmp(saved_manifest, manifest_path, shallow=False):
                        restore = backup / f".manifest-restore-{uuid.uuid4().hex}"
                        shutil.copyfile(saved_manifest, restore)
                        atomic_replace(restore, manifest_path)
                elif manifest_path.exists():
                    manifest_path.unlink()
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise RuntimeError(f"导出失败且部分文件无法回滚，恢复备份已保留：{backup}；请关闭占用图片的程序后重试。") from error
            # Old files are restored; the enclosing cleanup can remove staging.
            shutil.rmtree(backup, ignore_errors=True)
            if isinstance(error, PermissionError):
                raise PermissionError("图片文件被占用或无写入权限。原有图片已恢复，请关闭预览/图片编辑程序后重试导出。") from error
            raise

    def _export_images_and_videos(self, image_root: Path, *, include_pending: bool = True) -> dict[str, Any]:
        folder_names = self._folder_names()
        files: list[str] = []
        image_ranks: dict[str, int] = {}
        pending_ranks: dict[str, int] = {}
        selected_count = pending_count = video_count = 0
        for candidate in [*self.accepted_images(), *(self.pending_images() if include_pending else [])]:
            decision = self.decision(str(candidate.id))
            if self._hard_filtered(candidate):
                continue
            source = self._source_for(candidate)
            if source is None or not source.is_file():
                continue
            folder = folder_names.get(str(candidate.news_id), f"x{candidate.news_id}")
            converted = self._verified_final_image(candidate, source)
            if converted is None:
                continue
            data, mime_type = converted
            extension = ".jpg" if mime_type == "image/jpeg" else ".png"
            if decision is ReviewDecision.ACCEPTED:
                image_ranks[folder] = image_ranks.get(folder, 0) + 1
                target = image_root / folder / f"{folder}.{image_ranks[folder]:02d}{extension}"
                selected_count += 1
            else:
                pending_ranks[folder] = pending_ranks.get(folder, 0) + 1
                target = image_root / folder / "未候选" / f"{folder}.u{pending_ranks[folder]:02d}{extension}"
                pending_count += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self._unique_target(target, source)
            target.write_bytes(data)
            files.append((Path("images") / target.relative_to(image_root)).as_posix())

        reserved: dict[str, set[str]] = {}
        for candidate in self.videos:
            if self.decision(str(candidate.id)) is not ReviewDecision.ACCEPTED:
                continue
            source = self._source_for(candidate)
            if source is None or not source.is_file():
                continue
            folder = folder_names.get(str(candidate.news_id), f"x{candidate.news_id}")
            target_dir = image_root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            extension = source.suffix.casefold() or ".mp4"
            name = _safe_title(candidate.title, str(candidate.id)) + extension
            used = reserved.setdefault(folder, set())
            stem, suffix = Path(name).stem, Path(name).suffix
            index = 2
            while name.casefold() in used or (target_dir / name).exists():
                name = f"{stem} ({index}){suffix}"
                index += 1
            used.add(name.casefold())
            target = target_dir / name
            if target.resolve() != source.resolve():
                shutil.copyfile(source, target)
            files.append((Path("images") / target.relative_to(image_root)).as_posix())
            video_count += 1

        manifest = {
            "schema_version": 1,
            "issue_id": self._issue_id(),
            "files": files,
            "image_count": selected_count,
            "pending_image_count": pending_count,
            "video_count": video_count,
        }
        return manifest

    def _issue_id(self) -> str:
        if self.state_path.suffix == ".sqlite3":
            from ..tasks.repository import TaskRepository
            return str(TaskRepository(self.task_root, create=False).get_meta("issue_id", ""))
        for candidate in [self.output_dir / "image_index.json", self.output_dir / "video_index.json"]:
            payload = _read_json(candidate, {})
            if isinstance(payload, dict) and payload.get("issue_id") is not None:
                return str(payload["issue_id"])
        return ""

    @staticmethod
    def _media_extension(candidate: ImageCandidate | VideoCandidate, source: Path, *, default: str) -> str:
        mime_type = getattr(candidate, "output_mime_type", None) or getattr(candidate, "mime_type", None)
        if isinstance(mime_type, str) and "/" in mime_type:
            suffix = mime_type.split("/", 1)[1].split(";", 1)[0].strip().casefold()
            if suffix == "jpeg":
                suffix = "jpg"
            if suffix and re.fullmatch(r"[a-z0-9]+", suffix):
                return f".{suffix}"
        return source.suffix.casefold() or default

    def export(self) -> dict[str, Any]:
        return self.export_final()

    @classmethod
    def load(cls, output_dir: Path | str, **kwargs: Any) -> "ReviewSession":
        return cls.from_output(output_dir, **kwargs)

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        atomic_json_write(path, payload, ensure_parent=True)
