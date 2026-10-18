"""Download candidates into a temporary staging area before ranking."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
import time
import inspect
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from PIL import Image, ImageFilter
import httpx

from ..config import PrescanConfig
from ..domain import FailureRecord, FailureStage, ImageCandidate, ImageCurationStatus, SourceType
from ..discovery.http import SafeHttpClient, UnsafeUrlError, ResponseTooLarge
from ..discovery.adapters.common import _upgrade_image_url, _is_public_x_media
from .validation import ImageValidator, meaningless_asset_reason, placeholder_asset_reason
from ..tasks.assets import TaskAssets


_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}


def _perceptual_hash(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as image:
            grayscale = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
            values = list(grayscale.get_flattened_data() if hasattr(grayscale, "get_flattened_data") else grayscale.getdata())
        average = sum(values) / len(values)
        bits = "".join("1" if value >= average else "0" for value in values)
        return f"{int(bits, 2):016x}"
    except Exception:
        return None


def _image_quality_signals(data: bytes, width: int, height: int) -> dict[str, float | bool | str]:
    """Measure source detail, blur, and blockiness without using byte size."""
    try:
        with Image.open(BytesIO(data)) as source:
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            frame = source.copy().convert("L")
            frame.thumbnail((384, 384), Image.Resampling.LANCZOS)
            edge_histogram = frame.filter(ImageFilter.FIND_EDGES).histogram()
            count = max(1, sum(edge_histogram))
            edge_energy = sum(index * amount for index, amount in enumerate(edge_histogram)) / count / 255.0
            pixels = frame.load()
            frame_width, frame_height = frame.size
            second_derivatives: list[int] = []
            boundary_diffs: list[int] = []
            non_boundary_diffs: list[int] = []
            for y in range(1, frame_height - 1):
                for x in range(1, frame_width - 1):
                    center = pixels[x, y]
                    laplace = abs(4 * center - pixels[x - 1, y] - pixels[x + 1, y] - pixels[x, y - 1] - pixels[x, y + 1])
                    second_derivatives.append(laplace)
                    horizontal = abs(center - pixels[x - 1, y])
                    vertical = abs(center - pixels[x, y - 1])
                    if x % 8 == 0:
                        boundary_diffs.append(horizontal)
                    elif x % 8 == 1:
                        non_boundary_diffs.append(horizontal)
                    if y % 8 == 0:
                        boundary_diffs.append(vertical)
                    elif y % 8 == 1:
                        non_boundary_diffs.append(vertical)
            laplace_energy = sum(second_derivatives) / max(1, len(second_derivatives)) / 1020.0
            blockiness = max(0.0, min(1.0, (
                sum(boundary_diffs) / max(1, len(boundary_diffs))
                - sum(non_boundary_diffs) / max(1, len(non_boundary_diffs))
            ) / 255.0 * 4.0))
            # These are comparative ranking signals, not calibrated quality
            # labels. Preserve raw pixels and validate thresholds offline.
            sharpness = max(0.0, min(1.0, 0.55 * edge_energy * 2.8 + 0.45 * laplace_energy * 8.0))
            resolution = max(0.0, min(1.0, min(width / 1280.0, height / 720.0)))
            detail = max(0.0, min(1.0, 0.65 * resolution + 0.35 * sharpness - 0.15 * blockiness))
            return {
                "sharpness_score": round(sharpness, 6),
                "visible_detail_score": round(detail, 6),
                "blur_score": round(max(0.0, min(1.0, 1.0 - laplace_energy * 8.0)), 6),
                "blockiness_score": round(blockiness, 6),
                "transparent": has_alpha,
                "animated_source": frame_count > 1,
                "animation_frame_count": frame_count,
            }
    except Exception:
        return {"quality_analysis_failed": True}


class ImageDownloader:
    def __init__(self, config: PrescanConfig, *, transport: Callable[..., Any] | None = None):
        self.config = config
        self.processing_seconds = 0.0
        self._timing_lock = Lock()
        self.client = SafeHttpClient(
            transport=transport,
            timeout=config.network.timeout_seconds,
            max_retries=config.network.max_retries,
            max_response_bytes=config.filters.max_image_bytes,
            user_agent=config.network.user_agent,
            trust_env=config.network.trust_env,
        )
        self.validator = ImageValidator(
            min_width=config.filters.min_width,
            min_height=config.filters.min_height,
            min_pixels=config.filters.min_pixels,
            max_bytes=config.filters.max_image_bytes,
        )

    def import_local(self, candidate, directory):
        """Own an explicitly supplied offline asset before checkpointing it.

        Only fresh adapter results use this path. Resume still rejects foreign
        task paths rather than searching for or importing replacement files.
        """
        source = Path(candidate.original_path or candidate.local_path)
        expected = candidate.original_sha256 if candidate.original_path else candidate.output_sha256 or candidate.sha256
        candidate.local_path = candidate.original_path = None
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        try:
            if source.stat().st_size > self.config.filters.max_image_bytes:
                raise ResponseTooLarge("本地图片超过大小限制")
            data = source.read_bytes()
            if expected and hashlib.sha256(data).hexdigest() != expected:
                return candidate, self._failure(candidate, "local_asset_hash_mismatch", "本地图片哈希校验失败", False), None
            return self._process_response(candidate, SimpleNamespace(content=data, url=candidate.image_url,
                status_code=200, headers={}), root)
        except Exception as exc:
            return candidate, self._exception_failure(candidate, exc), None

    def _prepare(self, candidates, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        seen_urls: set[tuple[str, str]] = set()
        candidate_order: list[str] = []
        duplicate_urls: dict[tuple[str, str], ImageCandidate] = {}
        pending: list[ImageCandidate] = []
        filtered: list[FailureRecord] = []
        invalid: list[ImageCandidate] = []
        for candidate in candidates:
            parts = urlsplit(candidate.image_url.strip())
            normalized = urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, parts.query, ""))
            key = (candidate.news_id, normalized)
            if key in seen_urls:
                original = duplicate_urls[key]
                alternate_roots = [str(original.signals.get("root_source_url", "")), str(candidate.signals.get("root_source_url", ""))]
                roots = list(dict.fromkeys(value for value in alternate_roots if value))
                if roots:
                    original.signals["source_chain_alternates"] = "|".join(roots)
                continue
            seen_urls.add(key)
            duplicate_urls[key] = candidate
            candidate_order.append(str(candidate.id))
            if self.reusable(candidate):
                self._success(candidate)
                invalid.append(candidate)  # records returned without another request
                continue
            if candidate.download_status == "permanent_failed":
                invalid.append(candidate)
                continue
            placeholder_reason = placeholder_asset_reason(candidate)
            if placeholder_reason:
                candidate.downloadable = False
                candidate.signals["invalid_reason"] = placeholder_reason
                invalid.append(candidate)
                candidate.download_status = "permanent_failed"
                candidate.download_error_code = placeholder_reason
                filtered.append(self._failure(candidate, placeholder_reason, "placeholder asset was rejected before download", False))
                continue
            reason = meaningless_asset_reason(candidate)
            if reason or parts.scheme not in {"http", "https"}:
                failure_code = "filtered_invalid_material"
                candidate.downloadable = False
                candidate.signals["invalid_reason"] = reason or "invalid_scheme"
                invalid.append(candidate)
                candidate.download_status = "permanent_failed"
                candidate.download_error_code = failure_code
                filtered.append(self._failure(candidate, failure_code, reason or "invalid_scheme", False))
                continue
            pending.append(candidate)

        return root, candidate_order, pending, filtered, invalid

    def _process_response(self, candidate, response, root, temporary=None):
        started = time.monotonic()
        try:
            status = int(getattr(response, "status_code", 200))
            if status < 200 or status >= 300:
                return candidate, self._failure(candidate, "http_error", f"image request returned HTTP {status}", status >= 500 or status in {408, 429}), None
            final_response_url = str(getattr(response, "url", candidate.image_url) or candidate.image_url)
            redirected = candidate.model_copy(update={"image_url": final_response_url})
            if placeholder_asset_reason(redirected):
                return candidate, self._failure(candidate, "placeholder_image", "redirected URL is a placeholder asset", False), None
            data = temporary.read_bytes() if temporary is not None else (getattr(response, "content", b"") or b"")
            declared_mime = getattr(response, "headers", {}).get("content-type")
            validation = self.validator.validate(data, declared_mime)
            if not validation.valid:
                return candidate, self._failure(candidate, validation.reason or "invalid_image", "downloaded image failed validation", False), None
            suffix = _EXTENSIONS.get(validation.mime_type or "", ".img")
            task_assets = TaskAssets.for_path(root)
            if task_assets is not None:
                from ..tasks.repository import TaskRepository
                digest = hashlib.sha256(data).hexdigest()
                # Durable intent bridges the crash window between landing the
                # hash file and the coordinator committing download success.
                TaskRepository(task_assets.root, create=False).prepare_asset(str(candidate.id), {
                    "original_path": str(task_assets.original_path(data, suffix)),
                    "original_sha256": digest, "original_mime_type": validation.mime_type,
                    "original_width": validation.width, "original_height": validation.height,
                    "original_byte_size": len(data), "downloaded_url": final_response_url,
                })
            target = task_assets.put_original(data, suffix) if task_assets else root / f"{candidate.id}{suffix}"
            if temporary is None:
                if not task_assets:
                    temporary = root / f".{candidate.id}-{uuid.uuid4().hex}.part"
                    temporary.write_bytes(data)
            if not task_assets:
                os.replace(temporary, target)
            candidate.width, candidate.height = validation.width, validation.height
            candidate.mime_type, candidate.byte_size = validation.mime_type, len(data)
            candidate.sha256, candidate.perceptual_hash = hashlib.sha256(data).hexdigest(), _perceptual_hash(data)
            candidate.local_path, candidate.downloadable = str(target), True
            candidate.original_path = str(target)
            candidate.original_mime_type = validation.mime_type
            candidate.original_byte_size = len(data)
            candidate.original_sha256 = candidate.sha256
            candidate.original_width, candidate.original_height = validation.width, validation.height
            candidate.downloaded_url = final_response_url
            self._success(candidate)
            if candidate.expected_width and candidate.expected_height:
                candidate.signals["x_original_dimensions_match"] = (validation.width == candidate.expected_width and validation.height == candidate.expected_height)
            candidate.signals.update(_image_quality_signals(data, validation.width or 0, validation.height or 0))
            return candidate, None, final_response_url
        except Exception as exc:
            return candidate, self._exception_failure(candidate, exc), None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            with self._timing_lock:
                self.processing_seconds += time.monotonic() - started


    def _finish(self, outcomes, candidate_order, filtered, invalid):
        accepted: list[ImageCandidate] = []
        failures: list[FailureRecord] = list(filtered)
        for candidate, failure, final_url in outcomes:
            self._apply_outcome(candidate, failure)
            if failure is not None:
                if candidate is not None:
                    invalid.append(candidate)
                failures.append(failure)
                continue
            if candidate is None:
                continue
            accepted.append(candidate)
        # Keep invalid and downloaded candidate records. Failures are attached
        # separately for diagnostics; callers can still account for every URL.
        by_id = {str(candidate.id): candidate for candidate in [*accepted, *invalid]}
        all_candidates = [by_id[candidate_id] for candidate_id in candidate_order if candidate_id in by_id]
        return all_candidates, failures

    def download(self, candidates, directory):
        root, order, pending, filtered, invalid = self._prepare(candidates, directory)
        def fetch(candidate):
            try:
                response = self.client.get(self._primary_url(candidate))
                outcome = self._process_response(candidate, response, root)
            except Exception as exc:
                outcome = candidate, self._exception_failure(candidate, exc), None
            if self._fallback_allowed(candidate, outcome[1]):
                try:
                    response = self.client.get(candidate.media_source_url, max_retries=1)
                    outcome = self._process_response(candidate, response, root)
                    if outcome[1] is None:
                        candidate.signals["x_original_fallback"] = True
                except Exception as exc:
                    outcome = candidate, self._exception_failure(candidate, exc), None
            return outcome
        with ThreadPoolExecutor(max_workers=min(8, max(4, len(pending)))) as pool:
            outcomes = list(pool.map(fetch, pending))
        return self._finish(outcomes, order, filtered, invalid)

    async def download_async(self, candidates, directory, *, client, executor, concurrency=8, on_result=None):
        """Stream with a bounded window; CPU processing never runs on the loop."""
        loop = asyncio.get_running_loop()
        root, order, pending, filtered, invalid = await loop.run_in_executor(executor, self._prepare, candidates, directory)
        outcomes = [None] * len(pending)
        results = asyncio.Queue()
        queue = asyncio.Queue()
        for index, candidate in enumerate(pending):
            queue.put_nowait((index, candidate))

        async def worker():
            try:
                while not queue.empty():
                    index, candidate = queue.get_nowait()
                    temporary = root / f".{candidate.id}-{uuid.uuid4().hex}.part"
                    try:
                        async def attempt(url, *, fallback=False):
                            kwargs = {"max_retries": 1} if fallback else {}
                            response = await client.download(url, temporary, **kwargs)
                            processing = loop.run_in_executor(executor, self._process_response, candidate, response, root, temporary)
                            try:
                                return await asyncio.shield(processing)
                            except asyncio.CancelledError:
                                outcomes[index] = await processing
                                results.put_nowait((index, outcomes[index]))
                                raise
                        try:
                            outcome = await attempt(self._primary_url(candidate))
                        except Exception as exc:
                            outcome = candidate, self._exception_failure(candidate, exc), None
                        if self._fallback_allowed(candidate, outcome[1]):
                            outcome = await attempt(candidate.media_source_url, fallback=True)
                            if outcome[1] is None:
                                candidate.signals["x_original_fallback"] = True
                        outcomes[index] = outcome
                    except Exception as exc:
                        outcomes[index] = candidate, self._exception_failure(candidate, exc), None
                    finally:
                        if outcomes[index] is not None:
                            results.put_nowait((index, outcomes[index]))
                        temporary.unlink(missing_ok=True)
                        queue.task_done()
            finally:
                results.put_nowait(None)

        async def notify(outcome):
            candidate, failure, _ = outcome
            self._apply_outcome(candidate, failure)
            if on_result is not None:
                result = on_result(candidate, failure)
                if inspect.isawaitable(result):
                    await result

        tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(pending)))]
        delivered = set()
        try:
            for candidate in invalid:
                if on_result is not None:
                    result = on_result(candidate, None)
                    if inspect.isawaitable(result):
                        await result
            finished = 0
            while finished < len(tasks):
                item = await results.get()
                if item is None:
                    finished += 1
                elif item[0] not in delivered:
                    await notify(item[1])
                    delivered.add(item[0])
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for index, outcome in enumerate(outcomes):
                if outcome is not None and index not in delivered:
                    await notify(outcome)
        return self._finish(outcomes, order, filtered, invalid)

    @staticmethod
    def reusable(candidate):
        path = candidate.original_path or candidate.local_path
        digest = candidate.original_sha256 or candidate.sha256
        if not path or not digest:
            return False
        try:
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                return False
            with Image.open(BytesIO(data)) as image:
                image.load()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _success(candidate):
        candidate.download_status = "downloaded"
        candidate.download_error_code = None
        candidate.downloadable = True
        candidate.signals.pop("invalid_reason", None)
        candidate.curation_status = ImageCurationStatus.UNSELECTED
        if not candidate.local_path and candidate.original_path:
            candidate.local_path = candidate.original_path

    @staticmethod
    def _apply_outcome(candidate, failure):
        if failure is not None:
            candidate.downloadable = False
            candidate.local_path = None
            candidate.download_status = "retryable_failed" if failure.retryable else "permanent_failed"
            candidate.download_error_code = failure.code
            # Existing classification remains compatible; download status is
            # independent and cleared after a verified successful recovery.
            candidate.signals["invalid_reason"] = failure.code

    @staticmethod
    def _primary_url(candidate):
        if candidate.source_type is SourceType.OFFICIAL_X or candidate.signals.get("x_api_photo") or candidate.signals.get("socialdata_photo"):
            return _upgrade_image_url(candidate.image_url) or candidate.image_url
        return candidate.image_url

    @staticmethod
    def _fallback_allowed(candidate, failure):
        raw = candidate.media_source_url
        if failure is None or not raw or raw == ImageDownloader._primary_url(candidate):
            return False
        if not _is_public_x_media(raw):
            return False
        parts = urlsplit(raw)
        return ((candidate.signals.get("x_api_photo") or candidate.signals.get("socialdata_photo"))
                and parts.scheme == "https" and parts.hostname == "pbs.twimg.com"
                and parts.path.startswith("/media/") and not parts.username and not parts.password
                and failure.code not in {"unsafe_url", "response_too_large", "placeholder_image"})

    def _exception_failure(self, candidate, exc):
        if isinstance(exc, UnsafeUrlError):
            return self._failure(candidate, "unsafe_url", "媒体地址不允许访问", False)
        if isinstance(exc, ResponseTooLarge):
            return self._failure(candidate, "response_too_large", "媒体响应超过大小限制", False)
        retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, TimeoutError, OSError)) and not isinstance(exc, (PermissionError, FileNotFoundError))
        # Type-only diagnostics cannot expose proxy credentials or headers.
        return self._failure(candidate, "download_error", f"图片下载或处理失败（{type(exc).__name__}）", retryable)

    @staticmethod
    def _failure(candidate: ImageCandidate, code: str, message: str, retryable: bool) -> FailureRecord:
        return FailureRecord(
            stage=FailureStage.DOWNLOAD,
            news_id=candidate.news_id,
            candidate_id=candidate.id,
            code=code,
            message=str(message).strip() or f"图片下载失败（{code}）",
            source_url=candidate.image_url,
            retryable=retryable,
        )
