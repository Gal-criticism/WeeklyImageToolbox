"""Atomic JSON/Markdown delivery for pipeline results."""

from __future__ import annotations

import hashlib
import shutil
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain import FailureRecord, FailureStage, ImageCandidate, ImageCurationStatus, OutputManifest, PipelineResult, ReviewReason, VideoStatus
from ..curation.review_filter import is_low_resolution, reviewable_pending_images
from .image_conversion import ImageConversionError, ImageConverter
from .helpers import atomic_json_write, item_names as canonical_item_names, safe_video_name, section_label, section_prefix
from ..tasks.assets import TaskAssets


class OutputManager:
    _SENSITIVE_QUERY_NAMES = {
        "credential", "signature", "token", "api_key", "apikey",
        "access_key", "key", "policy", "expires", "key-pair-id",
    }
    # ``未候选`` is the user-facing canonical name.  Keep the old label as a
    # read-compatibility marker for imported outputs, but never write both
    # directories for one candidate.
    _UNSELECTED_CANDIDATE_DIR = "未候选"
    _LEGACY_FAILED_CANDIDATE_DIR = "失败候选图"

    @staticmethod
    def _image_source(candidate: ImageCandidate) -> Path | None:
        for value in (candidate.original_path, candidate.local_path):
            if value:
                path = Path(value)
                if path.is_file():
                    return path
        return None

    @classmethod
    def _save_review_image(cls, candidate: ImageCandidate, source: Path, root: Path, destination: Path) -> bool:
        source_bytes = source.read_bytes()
        task_assets = TaskAssets.for_path(root)
        converter = ImageConverter()
        try:
            converted = converter.convert(source_bytes, candidate)
        except ImageConversionError as exc:
            if task_assets:
                original = task_assets.put_original(source, source.suffix.casefold() or ".img")
            else:
                original_dir = root / "originals" / candidate.news_id
                original_dir.mkdir(parents=True, exist_ok=True)
                original = original_dir / f"{candidate.id}.source{source.suffix.casefold()}"
                if source.resolve() != original.resolve():
                    shutil.copyfile(source, original)
            candidate.original_path = str(original)
            candidate.original_mime_type = candidate.original_mime_type or candidate.mime_type
            candidate.original_byte_size = len(source_bytes)
            candidate.original_sha256 = hashlib.sha256(source_bytes).hexdigest()
            candidate.original_width = candidate.original_width or candidate.width
            candidate.original_height = candidate.original_height or candidate.height
            candidate.signals["conversion_error"] = str(exc)
            candidate.selection_reasons.append("review_image_conversion_failed")
            candidate.selected = False
            candidate.curation_status = ImageCurationStatus.UNSELECTED
            candidate.local_path = None
            return False

        source_suffix = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/avif": ".avif",
        }[converted.original_mime_type]
        if task_assets:
            original = task_assets.put_original(source_bytes, source_suffix)
        else:
            original_dir = root / "originals" / candidate.news_id
            original_dir.mkdir(parents=True, exist_ok=True)
            original = original_dir / f"{candidate.id}.source{source_suffix}"
            if source.resolve() != original.resolve():
                shutil.copyfile(source, original)
        candidate.original_path = str(original)
        candidate.original_mime_type = converted.original_mime_type
        candidate.original_byte_size = len(source_bytes)
        candidate.original_sha256 = hashlib.sha256(source_bytes).hexdigest()
        candidate.original_width = candidate.width
        candidate.original_height = candidate.height
        if converted.animated:
            candidate.animated_source = True
            candidate.animation_frame_index = converted.frame_index
            if "animated_source_requires_review" not in candidate.selection_reasons:
                candidate.selection_reasons.append("animated_source_requires_review")
            if ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
            candidate.signals["animated_source"] = True
        destination = destination.with_suffix(".jpg" if converted.mime_type == "image/jpeg" else ".png")
        destination = task_assets.put_review(converted.data, destination.suffix, profile="v1") if task_assets else destination
        if not task_assets:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(converted.data)
        candidate.local_path = str(destination)
        candidate.output_mime_type = converted.mime_type
        candidate.output_byte_size = len(converted.data)
        candidate.output_sha256 = hashlib.sha256(converted.data).hexdigest()
        candidate.output_width, candidate.output_height = converted.width, converted.height
        return True

    @classmethod
    def _safe_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.query:
            return value
        safe_query = []
        for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
            normalized = key.casefold()
            if normalized.startswith("x-amz-") or normalized in cls._SENSITIVE_QUERY_NAMES:
                continue
            safe_query.append((key, item_value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query, doseq=True), parts.fragment))

    @classmethod
    def _sanitize_payload(cls, value):
        """Remove credentials from serialized provenance without mutating models."""

        if isinstance(value, dict):
            sanitized = {}
            for key, item_value in value.items():
                if isinstance(item_value, str) and str(key).casefold().endswith("url"):
                    sanitized[key] = cls._safe_url(item_value)
                else:
                    sanitized[key] = cls._sanitize_payload(item_value)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_payload(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_payload(item) for item in value]
        return value

    @staticmethod
    def _section_label(item) -> str:
        return section_label(item)

    @classmethod
    def _section_prefix(cls, section: str) -> str:
        return section_prefix(section)

    @classmethod
    def item_names(cls, items) -> dict[str, str]:
        return canonical_item_names(items)

    # Private compatibility alias used by older delivery callers.
    _item_names = item_names

    def _atomic_json(self, path: Path, payload) -> None:
        atomic_json_write(path, payload, default=str)

    @staticmethod
    def _record_output_file(files: list[str], path: str | Path, root: Path) -> None:
        try:
            files.append(str(Path(path).relative_to(root)))
        except ValueError:
            # Task assets intentionally live outside the publication snapshot.
            pass

    @classmethod
    def _is_reviewable_failed_candidate(cls, candidate: ImageCandidate) -> bool:
        if candidate.selected or candidate.curation_status is ImageCurationStatus.INVALID or not candidate.downloadable or not candidate.local_path:
            return False
        source = Path(candidate.local_path)
        return source.is_file()

    @classmethod
    def _failed_candidate_sort_key(cls, candidate: ImageCandidate):
        total = candidate.score.total if candidate.score else 0.0
        source_trust = candidate.score.source_trust if candidate.score else 0.0
        pixels = (candidate.width or 0) * (candidate.height or 0)
        return (
            not (candidate.signals.get("x_api_photo") is True or candidate.signals.get("socialdata_photo") is True),
            -total,
            -source_trust,
            -pixels,
            candidate.id or "",
        )

    @staticmethod
    def _safe_video_name(candidate, fallback: str) -> str:
        return safe_video_name(candidate, fallback)

    @classmethod
    def _video_index_payload(cls, result: PipelineResult, videos) -> dict:
        related_news = []
        for item in result.issue.news_items:
            related = [video for video in videos if video.news_id == item.id]
            related_news.append(
                {
                    "news_id": item.id,
                    "sequence": item.sequence,
                    "section": cls._section_label(item),
                    "title": item.title,
                    "videos": [video.id for video in related],
                }
            )
        failures = [
            failure.model_dump(mode="json")
            for failure in result.failures
            if failure.stage.value == "download" and failure.news_id in {video.news_id for video in videos}
        ]
        return cls._sanitize_payload(
            {
                "schema_version": 1,
                "issue_id": result.issue.issue_id,
                "news_items": related_news,
                "videos": [video.model_dump(mode="json") for video in videos],
                "failures": failures,
            }
        )

    def write(self, result: PipelineResult, output_dir: Path | str) -> OutputManifest:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        image_root = root / "images"
        image_root.mkdir(exist_ok=True)
        # Only remove files managed beneath this run's images directory.
        for child in list(image_root.iterdir()):
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            elif child.is_file():
                child.unlink()
        files: list[str] = []
        item_names = self._item_names(result.issue.news_items)
        per_news_rank: dict[str, int] = {}
        for candidate in [c for c in result.candidates if c.selected and c.curation_status is not ImageCurationStatus.INVALID]:
            source_path = self._image_source(candidate)
            if source_path is None:
                continue
            per_news_rank[candidate.news_id] = per_news_rank.get(candidate.news_id, 0) + 1
            rank = per_news_rank[candidate.news_id]
            readable = re.sub(r"[\\/:*?\"<>|]", "_", item_names.get(candidate.news_id, candidate.news_id))
            target_dir = image_root / readable
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{readable}.{rank:02d}.png"
            if self._save_review_image(candidate, source_path, root, target):
                self._record_output_file(files, candidate.local_path, root)
        all_candidates = result.all_candidates
        exported_failed: set[int] = set()
        internally_reviewable: set[int] = set()
        accepted = [
            candidate for candidate in all_candidates
            if candidate.selected and candidate.curation_status is not ImageCurationStatus.INVALID
        ]
        for news_id in sorted({candidate.news_id for candidate in all_candidates}):
            pending = sorted(
                (
                    candidate
                    for candidate in all_candidates
                    if candidate.news_id == news_id and self._is_reviewable_failed_candidate(candidate)
                ),
                key=self._failed_candidate_sort_key,
            )
            failed = reviewable_pending_images(pending, accepted=accepted)
            failed_ids = {id(candidate) for candidate in failed}
            for candidate in failed:
                candidate.signals.pop("pending_hidden_reason", None)
            # Keep filtered images available to downstream review tooling, while
            # keeping them out of the user-facing 未候选 directory.
            for candidate in pending:
                if id(candidate) in failed_ids:
                    continue
                candidate.signals["pending_hidden_reason"] = (
                    "low_resolution" if is_low_resolution(candidate) else "confirmed_duplicate"
                )
                source_path = self._image_source(candidate)
                if source_path is None:
                    continue
                target = root / "review_assets" / candidate.news_id / f"{candidate.id}.png"
                if self._save_review_image(candidate, source_path, root, target):
                    internally_reviewable.add(id(candidate))
                    self._record_output_file(files, candidate.local_path, root)
            for rank, candidate in enumerate(failed, 1):
                readable = re.sub(r"[\\/:*?\"<>|]", "_", item_names.get(news_id, news_id))
                target_dir = image_root / readable / self._UNSELECTED_CANDIDATE_DIR
                target = target_dir / f"{readable}.u{rank:02d}.png"
                source_path = self._image_source(candidate)
                if source_path is None or not self._save_review_image(candidate, source_path, root, target):
                    continue
                exported_failed.add(id(candidate))
                self._record_output_file(files, candidate.local_path, root)
        videos = list(getattr(result, "videos", []) or [])
        video_reserved: dict[str, set[str]] = {}
        for video in videos:
            if video.status is not VideoStatus.DOWNLOADED or not video.local_path:
                continue
            source_path = Path(video.local_path)
            if not source_path.is_file():
                continue
            readable = re.sub(r'[\\/:*?"<>|]', "_", item_names.get(video.news_id, video.news_id))
            target_dir = image_root / readable
            target_dir.mkdir(parents=True, exist_ok=True)
            target_name = self._safe_video_name(video, readable)
            reserved = video_reserved.setdefault(video.news_id, set())
            stem, suffix = Path(target_name).stem, Path(target_name).suffix
            index = 2
            while target_name.casefold() in reserved or (target_dir / target_name).exists():
                target_name = f"{stem} ({index}){suffix}"
                index += 1
            reserved.add(target_name.casefold())
            target = target_dir / target_name
            if source_path.resolve() != target.resolve():
                shutil.copyfile(source_path, target)
            video.local_path = str(target)
            files.append(str(target.relative_to(root)))

        for candidate in all_candidates:
            # Preserve landed originals even when later policy marks a
            # candidate invalid and therefore does not export a review image.
            source_path = self._image_source(candidate)
            if source_path is not None and not TaskAssets.for_path(root) and not source_path.resolve().is_relative_to(root.resolve()):
                original_dir = root / "originals" / candidate.news_id
                original_dir.mkdir(parents=True, exist_ok=True)
                original = original_dir / f"{candidate.id}.source{source_path.suffix.casefold()}"
                shutil.copyfile(source_path, original)
                candidate.original_path = str(original)
                candidate.original_sha256 = hashlib.sha256(original.read_bytes()).hexdigest()
            if not candidate.selected and id(candidate) not in exported_failed and id(candidate) not in internally_reviewable:
                candidate.local_path = None
            conversion_error = candidate.signals.get("conversion_error")
            already_recorded = any(
                failure.code == "image_conversion_failed"
                and failure.candidate_id == candidate.id
                for failure in result.failures
            )
            if isinstance(conversion_error, str) and conversion_error and not already_recorded:
                result.failures.append(FailureRecord(
                    stage=FailureStage.OUTPUT,
                    news_id=candidate.news_id,
                    candidate_id=candidate.id,
                    code="image_conversion_failed",
                    message=conversion_error,
                    source_url=candidate.image_url,
                    retryable=False,
                ))
        candidate_payload = self._sanitize_payload([candidate.model_dump(mode="json") for candidate in all_candidates])
        news_payload = []
        for item in result.issue.news_items:
            related = [candidate for candidate in all_candidates if candidate.news_id == item.id]
            status = "selected" if any(c.selected for c in related) else ("failed" if any(f.news_id == item.id for f in result.failures) else ("no_candidate" if not related else "not_selected"))
            news_payload.append({"news_id": item.id, "sequence": item.sequence, "section": self._section_label(item), "title": item.title, "status": status, "candidates": [c.id for c in related]})
        failures_payload = self._sanitize_payload([failure.model_dump(mode="json") for failure in result.failures])
        index = {"schema_version": 1, "issue_id": result.issue.issue_id, "news_items": news_payload, "candidates": candidate_payload, "failures": failures_payload}
        self._atomic_json(root / "image_index.json", index)
        self._atomic_json(root / "video_index.json", self._video_index_payload(result, videos))
        self._atomic_json(root / "failed_items.json", failures_payload)
        reviews = result.review_required or [c for c in all_candidates if c.review_reasons]
        reviews_payload = self._sanitize_payload([candidate.model_dump(mode="json") for candidate in reviews])
        self._atomic_json(root / "review_required.json", reviews_payload)
        lines = [f"# Issue {result.issue.issue_id}", "", f"候选图片：{len(all_candidates)} 张", ""]
        for item in news_payload:
            lines.append(f"- {item['sequence']}. {item['title']} — {item['status']} ({len(item['candidates'])} candidates)")
            selected_images = [
                candidate for candidate in all_candidates
                if candidate.news_id == item["news_id"] and candidate.selected
            ]
            for rank, candidate in enumerate(selected_images, 1):
                image_url = self._safe_url(candidate.image_url)
                source_url = self._safe_url(candidate.source_url)
                lines.append(f"  - {rank:02d} {candidate.image_type.value}：[查看原图]({image_url}) · [查看来源页]({source_url})")
            related_x_photos = [
                candidate for candidate in all_candidates
                if candidate.news_id == item["news_id"]
                and (candidate.signals.get("x_api_photo") is True or candidate.signals.get("socialdata_photo") is True)
            ]
            for candidate in related_x_photos:
                post_url = self._safe_url(candidate.source_url)
                image_url = self._safe_url(candidate.image_url)
                lines.append(f"  - X API： [查看 X 原帖]({post_url}) · [查看原图]({image_url})")
        (root / "image_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return OutputManifest(issue_id=result.issue.issue_id, output_dir=str(root), image_count=sum(c.selected and bool(c.local_path) for c in result.candidates), files=files)
