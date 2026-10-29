"""Safe, injectable video downloading using yt-dlp.

This module deliberately stops at downloading already-discovered video URLs. Source
discovery, application integration, and output indexing belong to later layers.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import mimetypes
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from ..config import VideoConfig
from ..domain import FailureRecord, FailureStage, VideoCandidate, VideoStatus
from ..discovery.relevance import matches_work, news_relevance
from ..settings.ffmpeg import discover_ffmpeg


BackendFactory = Callable[[dict[str, Any]], Any]
_FALLBACK_EXTENSION = "mp4"
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
@dataclass(frozen=True)
class FFmpegResolution:
    available: bool
    location: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    source: str = "none"
    error: str | None = None


def _binary_in_directory(directory: Path, stem: str) -> Path | None:
    for name in (f"{stem}.exe", stem):
        candidate = directory / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolution_from_directory(directory: Path, source: str) -> FFmpegResolution | None:
    if not directory.is_dir():
        return None
    ffmpeg = _binary_in_directory(directory, "ffmpeg")
    if ffmpeg is None:
        return None
    ffprobe = _binary_in_directory(directory, "ffprobe")
    return FFmpegResolution(
        available=True,
        location=str(directory.resolve()),
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe) if ffprobe else None,
        source=source,
    )


def _resolution_from_location(raw_location: str, source: str) -> FFmpegResolution | None:
    expanded = os.path.expanduser(os.path.expandvars(str(raw_location).strip()))
    path = Path(expanded)
    if path.is_file():
        if path.stem.casefold() == "ffprobe":
            return _resolution_from_directory(path.parent, source)
        if path.stem.casefold() == "ffmpeg":
            directory = _resolution_from_directory(path.parent, source)
            if directory is not None:
                return directory
            resolved = path.resolve()
            return FFmpegResolution(
                available=True,
                location=str(resolved.parent),
                ffmpeg_path=str(resolved),
                source=source,
            )
        return None
    return _resolution_from_directory(path, source)


def resolve_ffmpeg_location(
    config_location: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    repo_root: Path | None = None,
) -> FFmpegResolution:
    """Resolve FFmpeg without assuming a user name or a refreshed PATH."""
    # A packaged onedir build places optional binaries in ``bin/``.  Probe
    # that location first, while keeping the existing explicit/PATH/WinGet/
    # portable fallbacks and their source labels unchanged below.
    bundled = discover_ffmpeg(
        bundle_dir=repo_root,
        environ={},
        which=lambda _name: None,
    )
    if bundled.available and bundled.source == "bundled":
        return FFmpegResolution(
            available=True,
            location=bundled.location,
            ffmpeg_path=bundled.ffmpeg_path,
            ffprobe_path=bundled.ffprobe_path,
            source="bundled",
            error=bundled.error,
        )

    values = environ if environ is not None else os.environ
    env_location = str(values.get("FFMPEG_LOCATION") or "").strip()
    configured_location = str(config_location or "").strip()
    explicit_error: str | None = None
    if env_location or configured_location:
        explicit = env_location or configured_location
        explicit_source = "environment" if env_location else "config"
        resolved = _resolution_from_location(explicit, explicit_source)
        if resolved is not None:
            return resolved
        explicit_error = f"invalid explicit FFmpeg location: {explicit}"

    find = which or shutil.which
    path_value = find("ffmpeg")
    if path_value:
        resolved = _resolution_from_location(path_value, "path")
        if resolved is not None:
            return resolved

    local_appdata = str(values.get("LOCALAPPDATA") or "").strip()
    if local_appdata:
        winget_root = Path(local_appdata) / "Microsoft" / "WinGet"
        for link_name in ("ffmpeg.exe", "ffmpeg"):
            resolved = _resolution_from_location(str(winget_root / "Links" / link_name), "winget")
            if resolved is not None:
                return resolved
        packages = winget_root / "Packages"
        if packages.is_dir():
            # WinGet's Gyan package has a package directory, a build directory,
            # then bin/; collect all exact candidates before choosing one so
            # multiple installed versions are deterministic.
            winget_candidates: list[FFmpegResolution] = []
            for package in sorted(packages.glob("Gyan.FFmpeg_*")):
                for build_bin in sorted(package.glob("ffmpeg-*/bin")):
                    resolved = _resolution_from_directory(build_bin, "winget")
                    if resolved is not None:
                        winget_candidates.append(resolved)
                for fallback in (package / "bin", package):
                    resolved = _resolution_from_directory(fallback, "winget")
                    if resolved is not None:
                        winget_candidates.append(resolved)
            if winget_candidates:
                return sorted(
                    winget_candidates,
                    key=lambda item: (item.location or "").casefold(),
                )[-1]

    root = repo_root or Path(__file__).resolve().parents[3]
    for portable in (root / ".tools" / "ffmpeg" / "bin", root / ".tools" / "ffmpeg"):
        resolved = _resolution_from_directory(portable, "portable")
        if resolved is not None:
            return resolved
    return FFmpegResolution(available=False, error=explicit_error or "FFmpeg executable not found")


def normalize_video_url(url: str) -> str:
    """Normalize the URL enough to remove fragments and casing-only duplicates."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path,
            parts.query,
            "",
        )
    )


def sanitize_video_filename(title: str, news_folder: str, extension: str) -> str:
    """Return a Windows-safe filename while preserving the actual container."""
    safe_title = _INVALID_FILENAME.sub("", title or "")
    safe_title = "".join(character for character in safe_title if ord(character) >= 32)
    safe_title = _WHITESPACE.sub(" ", safe_title).strip().rstrip(".")
    safe_title = safe_title[:120].rstrip(" .")
    if not safe_title:
        fallback = _INVALID_FILENAME.sub("", news_folder or "").strip().rstrip(" .")
        safe_title = f"{fallback or 'news'}_video"
    safe_extension = re.sub(r"[^a-zA-Z0-9]+", "", extension or "") or _FALLBACK_EXTENSION
    return f"{safe_title}.{safe_extension.casefold()}"


def _default_backend_factory(options: dict[str, Any]) -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed") from exc
    return yt_dlp.YoutubeDL(options)


def _default_ffmpeg_detector(config_location: str | None = None) -> bool:
    return resolve_ffmpeg_location(config_location).available


def _append_reason(candidate: VideoCandidate, reason: str) -> None:
    if reason not in candidate.review_reasons:
        candidate.review_reasons.append(reason)


def _metadata_number(info: dict[str, Any], *keys: str) -> float | int | None:
    for key in keys:
        value = info.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return value
    return None


def _selected_height(info: dict[str, Any]) -> int | None:
    video = _selected_video_stream(info)
    height = _metadata_number(video, "height")
    return int(height) if height is not None and height > 0 else None


def _selected_video_stream(info: dict[str, Any]) -> dict[str, Any]:
    # `formats` describes every available alternative, including unselected 4K
    # or portrait versions. Only requested streams describe this download.
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        for stream in requested:
            if (isinstance(stream, dict) and stream.get("vcodec") != "none"
                    and (_metadata_number(stream, "height") or _metadata_number(stream, "width"))):
                return stream
    return info


def _selected_size(info: dict[str, Any]) -> int | None:
    exact = _metadata_number(info, "filesize")
    if exact is not None and exact > 0:
        return int(exact)
    requested = info.get("requested_formats")
    if isinstance(requested, list) and requested:
        sizes = [_metadata_number(item, "filesize", "filesize_approx")
                 if isinstance(item, dict) else None for item in requested]
        if all(value is not None and value > 0 for value in sizes):
            return int(sum(sizes))
    approximate = _metadata_number(info, "filesize_approx")
    return int(approximate) if approximate is not None and approximate > 0 else None


def _extension_from_path(path: Path, info: dict[str, Any]) -> str:
    extension = path.suffix.lstrip(".").casefold()
    if extension:
        return extension
    raw = str(info.get("ext") or "")
    return raw.casefold() or _FALLBACK_EXTENSION


def _error_code(message: str) -> str:
    lowered = message.casefold()
    if "ffprobe unavailable" in lowered:
        return "video_tools_missing"
    if "video validation failed" in lowered:
        return "invalid_video"
    if "login" in lowered or "sign in" in lowered or "private" in lowered:
        return "access_restricted"
    if "region" in lowered or "geo" in lowered:
        return "region_restricted"
    if "deleted" in lowered or "unavailable" in lowered or "not found" in lowered:
        return "video_unavailable"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "format" in lowered:
        return "format_unavailable"
    return "download_error"


@contextlib.contextmanager
def _backend_context(factory: BackendFactory, options: dict[str, Any]):
    backend = factory(options)
    enter = getattr(backend, "__enter__", None)
    exit_method = getattr(backend, "__exit__", None)
    if callable(enter):
        backend = enter()
    try:
        yield backend
    finally:
        if callable(exit_method):
            exit_method(None, None, None)


class VideoDownloader:
    """Download a bounded batch of previously discovered video candidates."""

    def __init__(
        self,
        config: VideoConfig | None = None,
        *,
        backend_factory: BackendFactory | None = None,
        ffmpeg_detector: Callable[[], bool] | None = None,
        media_probe: Callable[[Path], dict[str, Any] | None] | None = None,
        retries: int = 2,
        timeout_seconds: int = 30,
    ):
        self.config = config or VideoConfig()
        self.backend_factory = backend_factory or _default_backend_factory
        # Injectable backends may return synthetic media. Real yt-dlp downloads
        # are probed; integrations can inject their own verifier explicitly.
        self.media_probe = media_probe or (self._probe_download if backend_factory is None else None)
        self._uses_default_probe = media_probe is None and backend_factory is None
        self._ffmpeg_detector_override = ffmpeg_detector
        self.ffmpeg_detector = ffmpeg_detector or (lambda: _default_ffmpeg_detector(self.config.ffmpeg_location))
        self.ffmpeg_resolution: FFmpegResolution | None = None
        self.retries = max(0, min(int(retries), 10))
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _ffmpeg_state(self) -> tuple[bool, str | None]:
        resolution = resolve_ffmpeg_location(config_location=self.config.ffmpeg_location)
        if self._ffmpeg_detector_override is not None:
            available = bool(self.ffmpeg_detector())
            self.ffmpeg_resolution = resolution
            return available, resolution.location if available and resolution.available else None
        self.ffmpeg_resolution = resolution
        return resolution.available, resolution.location

    def format_selector(self, *, ffmpeg: bool | None = None) -> str:
        height = self.config.max_height
        if ffmpeg if ffmpeg is not None else self.ffmpeg_detector():
            return (
                f"bv*[height<={height}]+ba / "
                f"b[height<={height}] / best[height<={height}]"
            )
        preferred = self.config.preferred_container
        fallback = "webm" if preferred == "mp4" else "mp4"
        return (
            f"b[height<={height}][ext={preferred}] / "
            f"best[height<={height}][ext={preferred}] / "
            f"b[height<={height}][ext={fallback}] / "
            f"best[height<={height}][ext={fallback}]"
        )

    def _options(self, staging_dir: Path, *, ffmpeg: bool, ffmpeg_location: str | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": self.format_selector(ffmpeg=ffmpeg),
            "outtmpl": str(staging_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "retries": self.retries,
            "extractor_retries": self.retries,
            "fragment_retries": self.retries,
            "socket_timeout": self.timeout_seconds,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": self.config.max_file_bytes,
        }
        if ffmpeg:
            options["merge_output_format"] = self.config.preferred_container
            if ffmpeg_location:
                options["ffmpeg_location"] = ffmpeg_location
        return options

    def download(
        self,
        candidates: Iterable[VideoCandidate],
        output_dir: str | Path,
        *,
        news_item=None,
    ) -> tuple[list[VideoCandidate], list[FailureRecord]]:
        """Download candidates and return all final states plus isolated failures."""
        result: list[VideoCandidate] = []
        failures: list[FailureRecord] = []
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        staging_path = Path(tempfile.mkdtemp(prefix=".video-staging-", dir=output_path))
        seen_by_news: dict[str, set[str]] = defaultdict(set)
        accepted_by_news: dict[str, int] = defaultdict(int)
        reserved_names: set[str] = set()

        try:
            for original in candidates:
                candidate = original.model_copy(deep=True)
                if not self.config.enabled:
                    self._skip(candidate, "video_download_disabled", failures)
                    result.append(candidate)
                    continue

                normalized = normalize_video_url(candidate.video_url)
                if normalized in seen_by_news[candidate.news_id]:
                    self._skip(candidate, "duplicate_video_url", failures)
                    result.append(candidate)
                    continue
                seen_by_news[candidate.news_id].add(normalized)
                if accepted_by_news[candidate.news_id] >= self.config.max_per_news:
                    self._skip(candidate, "per_news_limit", failures)
                    result.append(candidate)
                    continue

                with tempfile.TemporaryDirectory(prefix="item-", dir=staging_path) as item_stage:
                    self._process_one(
                        candidate, Path(item_stage), output_path,
                        reserved_names, accepted_by_news, failures, news_item,
                    )
                result.append(candidate)
        finally:
            shutil.rmtree(staging_path, ignore_errors=True)
        return result, failures

    def _skip(
        self,
        candidate: VideoCandidate,
        reason: str,
        failures: list[FailureRecord],
    ) -> None:
        candidate.status = VideoStatus.SKIPPED
        candidate.downloadable = False
        candidate.failure_reason = reason
        _append_reason(candidate, reason)
        failures.append(
            self._failure(candidate, reason, reason.replace("_", " "), retryable=False)
        )

    def _failure(
        self,
        candidate: VideoCandidate,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> FailureRecord:
        return FailureRecord(
            stage=FailureStage.DOWNLOAD,
            news_id=candidate.news_id,
            candidate_id=candidate.id,
            code=code,
            message=message,
            source_url=candidate.source_url,
            retryable=retryable,
        )

    def _process_one(
        self,
        candidate: VideoCandidate,
        staging_path: Path,
        output_path: Path,
        reserved_names: set[str],
        accepted_by_news: dict[str, int],
        failures: list[FailureRecord],
        news_item=None,
    ) -> None:
        candidate.status = VideoStatus.DOWNLOADING
        try:
            ffmpeg, ffmpeg_location = self._ffmpeg_state()
            if self._uses_default_probe and not self._probe_executable():
                raise RuntimeError("ffprobe unavailable; 请在设置中检查视频工具后继续任务。")
            options = self._options(staging_path, ffmpeg=ffmpeg, ffmpeg_location=ffmpeg_location)
            if (urlsplit(candidate.video_url).hostname or "").casefold() == "video.twimg.com":
                # X's direct MP4/HLS extractor often has no dimensions until
                # the file is read; verify those dimensions after downloading.
                options["format"] = options["format"].replace("height<=", "height<=?")
            with _backend_context(self.backend_factory, options) as backend:
                info = backend.extract_info(candidate.video_url, download=False)
                if not isinstance(info, dict):
                    raise RuntimeError("yt-dlp returned no metadata")
                if info.get("_type") in {"playlist", "multi_video"} or isinstance(info.get("entries"), list):
                    self._skip(candidate, "playlist_not_allowed", failures)
                    return
                self._apply_metadata(candidate, info)

                if news_item is not None:
                    video_title = str(info.get("title") or "")
                    description = str(info.get("description") or "")
                    description = re.split(r"\b(?:recommended|related videos?|also watch|suggested videos?)\b|推荐|推薦|猜你喜欢|相關影片|関連動画", description, maxsplit=1, flags=re.I)[0]
                    # Uploader/channel identity is deliberately not used as
                    # the deciding signal: makers also publish other works.
                    if not matches_work(news_item, video_title) or not news_relevance(news_item, f"{video_title} {description}"):
                        generic_title = re.fullmatch(r"\s*(?:pv|trailer|official video|video|予告|預告|pv公開|動画公開)\s*", video_title, re.I)
                        code = "video_relevance_unverified" if generic_title or not video_title else "video_relevance_rejected"
                        self._skip(candidate, code, failures)
                        return

                duration = candidate.duration_seconds
                if duration is not None and duration > self.config.max_duration_seconds:
                    self._skip(candidate, "duration_exceeded", failures)
                    return
                size = _selected_size(info)
                if size is not None and size > self.config.max_file_bytes:
                    self._skip(candidate, "file_size_exceeded", failures)
                    return
                height = _selected_height(info)
                if height is not None and height > self.config.max_height:
                    self._skip(candidate, "height_exceeded", failures)
                    return

                process_info = getattr(backend, "process_info", None)
                # Download the already selected metadata/streams. Calling
                # download(URL) again would repeat extraction and selection.
                output_result = process_info(info) if callable(process_info) else backend.download([candidate.video_url])
                downloaded_file = self._find_downloaded_file(
                    staging_path, output_result, backend, info
                )
                if downloaded_file is None:
                    raise RuntimeError("yt-dlp produced no output file")
                actual_size = downloaded_file.stat().st_size
                if actual_size <= 0:
                    raise RuntimeError("downloaded video is empty")
                if actual_size > self.config.max_file_bytes:
                    self._skip(candidate, "file_size_exceeded", failures)
                    downloaded_file.unlink(missing_ok=True)
                    return

                if self.media_probe is not None:
                    measured = self.media_probe(downloaded_file)
                    if measured is None:
                        raise ValueError("video validation failed (no probe result)")
                    else:
                        candidate.width = measured.get("width")
                        candidate.height = measured.get("height")
                        candidate.duration_seconds = measured.get("duration_seconds")
                        if candidate.height and candidate.height > self.config.max_height:
                            self._skip(candidate, "height_exceeded", failures)
                            return
                        if candidate.duration_seconds and candidate.duration_seconds > self.config.max_duration_seconds:
                            self._skip(candidate, "duration_exceeded", failures)
                            return

                digest, byte_size = self._hash_file(downloaded_file)
                extension = _extension_from_path(downloaded_file, info)
                filename = self._unique_filename(
                    candidate.title or str(info.get("title") or ""),
                    candidate.news_id,
                    extension,
                    output_path,
                    reserved_names,
                )
                final_path = output_path / filename
                os.replace(downloaded_file, final_path)
                candidate.title = candidate.title or str(info.get("title") or "")
                candidate.byte_size = byte_size
                candidate.sha256 = digest
                candidate.local_path = str(final_path)
                candidate.format = str(info.get("format_id") or info.get("format") or extension)
                candidate.mime_type = (
                    str(info.get("mime_type"))
                    if info.get("mime_type")
                    else mimetypes.guess_type(final_path.name)[0]
                )
                candidate.downloadable = True
                candidate.status = VideoStatus.DOWNLOADED
                candidate.failure_reason = None
                candidate.review_reasons = [reason for reason in candidate.review_reasons if reason not in {
                    "height_exceeded", "file_size_exceeded", "duration_exceeded", "per_news_limit",
                    "download_error", "format_unavailable", "timeout", "invalid_video",
                }]
                accepted_by_news[candidate.news_id] += 1
        except Exception as exc:
            candidate.status = VideoStatus.FAILED
            candidate.downloadable = False
            candidate.failure_reason = str(exc).strip() or f"视频下载失败（{type(exc).__name__}）。"
            failures.append(
                self._failure(
                    candidate,
                    _error_code(candidate.failure_reason),
                    candidate.failure_reason,
                    retryable=_error_code(candidate.failure_reason) != "invalid_video",
                )
            )

    @staticmethod
    def _apply_metadata(candidate: VideoCandidate, info: dict[str, Any]) -> None:
        if info.get("title") is not None:
            candidate.title = str(info["title"])
        if info.get("uploader") is not None:
            candidate.uploader = str(info["uploader"])
        candidate.duration_seconds = _metadata_number(info, "duration")
        candidate.width = _metadata_number(_selected_video_stream(info), "width")
        candidate.height = _selected_height(info)
        candidate.format = str(info.get("format_id") or info.get("format") or info.get("ext") or "") or None
        if info.get("mime_type"):
            candidate.mime_type = str(info["mime_type"])
        candidate.byte_size = _selected_size(info)

    @staticmethod
    def _find_downloaded_file(
        staging_path: Path,
        download_result: Any,
        backend: Any,
        info: dict[str, Any],
    ) -> Path | None:
        if isinstance(download_result, (str, Path)):
            path = Path(download_result)
            if path.is_file() and path.resolve().is_relative_to(staging_path.resolve()):
                return path
        prepare_filename = getattr(backend, "prepare_filename", None)
        if callable(prepare_filename):
            try:
                path = Path(prepare_filename(info))
                if path.is_file() and path.resolve().is_relative_to(staging_path.resolve()):
                    return path
            except Exception:
                pass
        files = [path for path in staging_path.rglob("*") if path.is_file()
                 and path.suffix.casefold() in {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".mpeg", ".mpg"}]
        return max(files, key=lambda path: path.stat().st_mtime_ns) if files else None

    def _probe_executable(self) -> str | None:
        resolution = self.ffmpeg_resolution or resolve_ffmpeg_location(self.config.ffmpeg_location)
        return resolution.ffprobe_path or shutil.which("ffprobe")

    def _probe_download(self, path: Path) -> dict[str, Any]:
        executable = self._probe_executable()
        if not executable:
            raise RuntimeError("ffprobe unavailable; 请在设置中检查视频工具后继续任务。")
        try:
            result = subprocess.run(
                [str(executable), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise ValueError("ffprobe could not read the media")
            payload = json.loads(result.stdout)
            stream = next((entry for entry in payload.get("streams", [])
                           if entry.get("codec_type") == "video"
                           and not entry.get("disposition", {}).get("attached_pic")), None)
            if stream is None or not stream.get("width") or not stream.get("height"):
                raise ValueError("no playable video stream")
            duration = payload.get("format", {}).get("duration") or stream.get("duration")
            return {"width": int(stream["width"]), "height": int(stream["height"]),
                    "duration_seconds": float(duration) if duration is not None else None,
                    "format_name": payload.get("format", {}).get("format_name")}
        except Exception as exc:
            raise ValueError(f"video validation failed ({type(exc).__name__})") from exc

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        byte_size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_size += len(chunk)
        return digest.hexdigest(), byte_size

    @staticmethod
    def _unique_filename(
        title: str,
        news_folder: str,
        extension: str,
        output_path: Path,
        reserved_names: set[str],
    ) -> str:
        original = sanitize_video_filename(title, news_folder, extension)
        stem = Path(original).stem
        suffix = Path(original).suffix
        candidate = original
        index = 2
        while candidate.casefold() in reserved_names or (output_path / candidate).exists():
            candidate = f"{stem} ({index}){suffix}"
            index += 1
        reserved_names.add(candidate.casefold())
        return candidate
