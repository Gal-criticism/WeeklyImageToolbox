"""On-demand, content-verified materialization of indexed archive media."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import PrescanConfig, load_config
from ..curation.download import ImageDownloader
from ..delivery.output import OutputManager
from ..domain import (
    DiscoveryMethod,
    ImageCandidate,
    ImageType,
    SourceType,
    VideoCandidate,
    VideoStatus,
)
from ..video.downloader import VideoDownloader
from .models import ArchiveAsset


Cancelled = Callable[[], bool] | threading.Event | None


def _is_cancelled(cancelled: Cancelled) -> bool:
    if cancelled is None:
        return False
    if isinstance(cancelled, threading.Event):
        return cancelled.is_set()
    return bool(cancelled())


def _check_cancel(cancelled: Cancelled) -> None:
    if _is_cancelled(cancelled):
        raise InterruptedError("archive operation cancelled")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class ArchiveMaterializer:
    """Fetch only an adopted archive asset, with independent verified caches."""

    def __init__(
        self,
        cache_root: str | Path,
        provider_lookup: Callable[[str], Any],
        config: PrescanConfig | None = None,
        media_probe: Callable[[Path], dict[str, Any] | None] | None = None,
    ):
        self.cache_root = Path(cache_root)
        self.provider_lookup = provider_lookup
        self.config = config or load_config()
        self.video_downloader = VideoDownloader(self.config.video, media_probe=media_probe)

    def _key(self, asset: ArchiveAsset) -> str:
        raw = "\0".join((asset.source_id, asset.asset_id, asset.revision or ""))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _limit(self, asset: ArchiveAsset, *, preview: bool = False) -> int:
        if preview:
            return 20 * 1024 * 1024
        if asset.kind == "image":
            return self.config.filters.max_image_bytes
        return self.config.video.max_file_bytes

    def _cached_file(self, asset: ArchiveAsset, category: str) -> Path:
        suffix = Path(asset.name).suffix.casefold()
        if not suffix or len(suffix) > 10:
            suffix = ".bin"
        return self.cache_root / category / self._key(asset)[:2] / f"{self._key(asset)}{suffix}"

    @staticmethod
    def _cached_valid(path: Path, manifest: Path) -> bool:
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            digest, size = _hash_file(path)
            return size > 0 and size == metadata["size"] and digest == metadata["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _materialize(self, asset: ArchiveAsset, category: str, *, preview: bool, cancelled: Cancelled) -> Path | None:
        _check_cancel(cancelled)
        if preview and not (asset.preview_url or (asset.metadata and asset.metadata.get("local_preview_path"))):
            return None
        limit = self._limit(asset, preview=preview)
        if not preview and asset.size is not None and asset.size > limit:
            raise ValueError("asset exceeds byte limit")
        destination = self._cached_file(asset, category)
        manifest = destination.with_suffix(destination.suffix + ".json")
        if destination.is_file() and self._cached_valid(destination, manifest):
            _check_cancel(cancelled)
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        provider = self.provider_lookup(asset.source_id)
        try:
            fetch = provider.preview if preview else provider.fetch
            fetch(asset, partial, max_bytes=limit, cancelled=cancelled)
            _check_cancel(cancelled)
            digest, size = _hash_file(partial)
            if size <= 0 or size > limit:
                raise ValueError("asset exceeds byte limit" if size > limit else "asset is empty")
            if not preview and asset.size is not None and size != asset.size:
                raise ValueError("asset size does not match indexed metadata")
            os.replace(partial, destination)
            manifest.write_text(json.dumps({"sha256": digest, "size": size}, separators=(",", ":")), encoding="utf-8")
            return destination
        except BaseException:
            partial.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            raise

    def get_original(self, asset: ArchiveAsset, cancelled: Cancelled = None) -> Path:
        path = self._materialize(asset, "originals", preview=False, cancelled=cancelled)
        assert path is not None
        return path

    def get_preview(self, asset: ArchiveAsset, cancelled: Cancelled = None) -> Path | None:
        return self._materialize(asset, "previews", preview=True, cancelled=cancelled)

    def prepare(
        self,
        asset: ArchiveAsset,
        news_id: str,
        task_root: str | Path,
        cancelled: Cancelled = None,
    ) -> ImageCandidate | VideoCandidate:
        if not news_id or Path(news_id).name != news_id:
            raise ValueError("news_id must be a single path component")
        if asset.kind not in {"image", "video"}:
            raise ValueError("unsupported archive asset kind")
        _check_cancel(cancelled)
        task_root = Path(task_root)
        attempt_id = "archive_" + hashlib.sha256(
            "\0".join((news_id, asset.source_id, asset.asset_id, asset.revision or "")).encode("utf-8")
        ).hexdigest()[:20]
        attempt_root = task_root / "retries" / news_id / attempt_id
        attempt_root.mkdir(parents=True, exist_ok=True)
        provenance = {
            "attempt_id": attempt_id,
            "source_id": asset.source_id,
            "asset_id": asset.asset_id,
            "revision": asset.revision,
            "issue_number": asset.issue_number,
            "issue_date": asset.issue_date,
            "path": asset.path,
            "source_kind": asset.source_kind,
            "original_source_url": asset.original_source_url,
        }
        source_path = self.get_original(asset, cancelled=cancelled)
        _check_cancel(cancelled)
        source_url = asset.original_source_url or asset.original_url or f"archive://{asset.source_id}/{asset.asset_id}"
        if asset.kind == "image":
            candidate = ImageCandidate(
                news_id=news_id,
                image_url=asset.original_url or source_url,
                source_url=source_url,
                source_type=SourceType.UNKNOWN,
                image_type=ImageType.UNKNOWN,
                fetched_at=datetime.now(timezone.utc),
                downloadable=True,
                selected=True,
                archive_provenance=provenance,
            )
            candidate.original_path = str(source_path)
            candidate.original_sha256, candidate.original_byte_size = _hash_file(source_path)
            imported, failure, _ = ImageDownloader(self.config).import_local(candidate, attempt_root / "images")
            if failure is not None or not imported.local_path:
                raise ValueError(failure.message if failure else "archive image import failed")
            review_path = attempt_root / "review" / f"{imported.id}.jpg"
            if not OutputManager._save_review_image(imported, Path(imported.local_path), task_root, review_path):
                raise ValueError("archive image could not be converted for review")
            imported.selected = True
            imported.downloadable = True
            record = imported.model_dump(mode="json")
        else:
            limit = self.config.video.max_file_bytes
            if source_path.stat().st_size > limit:
                raise ValueError("video exceeds byte limit")
            probe = self.video_downloader.media_probe
            if probe is None:
                raise ValueError("video validation requires a media probe")
            try:
                measured = probe(source_path)
            except Exception as exc:
                raise ValueError("video is not playable") from exc
            if not measured or not measured.get("width") or not measured.get("height"):
                raise ValueError("video is not playable")
            width = int(measured["width"])
            height = int(measured["height"])
            duration = measured.get("duration_seconds")
            duration = float(duration) if duration is not None else None
            if width < 1 or height < 1:
                raise ValueError("video is not playable")
            if height > self.config.video.max_height:
                raise ValueError("video height exceeds configured limit")
            if duration is not None and duration > self.config.video.max_duration_seconds:
                raise ValueError("video duration exceeds configured limit")
            suffix = Path(asset.name).suffix.casefold()
            supported_formats = {
                ".mp4": {"mp4", "m4a", "3gp", "3g2", "mj2"},
                ".mov": {"mov"},
                ".mkv": {"matroska"},
                ".webm": {"webm"},
            }
            if suffix not in supported_formats:
                raise ValueError(f"unsupported video container: {suffix or '(no extension)'}")
            format_name = str(measured.get("format_name") or "").casefold()
            if format_name:
                detected = {part.strip() for part in format_name.split(",") if part.strip()}
                if not detected.intersection(supported_formats[suffix]):
                    raise ValueError("video container does not match filename suffix")
            video_path = attempt_root / "videos" / f"{candidate_id_for_video(news_id, asset)}{suffix}"
            video_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, video_path)
            digest, size = _hash_file(video_path)
            candidate = VideoCandidate(
                news_id=news_id,
                source_url=source_url,
                video_url=asset.original_url or source_url,
                title=asset.name,
                duration_seconds=duration,
                width=width,
                height=height,
                mime_type=mimetypes.guess_type(asset.name)[0] or "video/mp4",
                byte_size=size,
                sha256=digest,
                local_path=str(video_path),
                downloadable=True,
                status=VideoStatus.DOWNLOADED,
                discovered_via=DiscoveryMethod.HISTORY,
                archive_provenance=provenance,
            )
            record = candidate.model_dump(mode="json")
        base = {"schema_version": 1, "kind": "retry", "attempt_id": attempt_id}
        image_payload = {**base, "candidates": [record] if asset.kind == "image" else []}
        video_payload = {**base, "videos": [record] if asset.kind == "video" else []}
        (attempt_root / "image_index.json").write_text(
            json.dumps(image_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (attempt_root / "video_index.json").write_text(
            json.dumps(video_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return candidate


def candidate_id_for_video(news_id: str, asset: ArchiveAsset) -> str:
    """Return a filesystem-safe name scoped to the news and archive revision."""
    raw = "\0".join((news_id, asset.source_id, asset.asset_id, asset.revision or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
