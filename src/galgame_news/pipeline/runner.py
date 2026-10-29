"""Resumable orchestration engine shared by CLI and desktop frontends."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..config import PrescanConfig, load_config
from ..curation import ImageCurator, ImageDownloader
from ..delivery.output import OutputManager
from ..delivery.asset_paths import rebase_asset_paths
from ..delivery.helpers import atomic_json_write, section_label, section_prefix
from ..discovery.adapters import (
    DirectImageAdapter,
    DynamicPageAdapter,
    OfficialHtmlAdapter,
    SteamAdapter,
    VideoAdapter,
)
from ..discovery.resolver import DefaultSourceResolver
from ..discovery.x_api import create_x_adapter, _STATUS_ID
from ..domain import (
    CollectionContext,
    DiscoveryMethod,
    FailureRecord,
    FailureStage,
    ImageCandidate,
    Issue,
    NewsItem,
    NewsResult,
    PipelineResult,
    ReviewReason,
    SourceRef,
    SourceType,
    VideoCandidate,
    VideoStatus,
)
from ..ingestion import DocxDocumentParser, OpenAINewsAnalyzer, RuleBasedNewsAnalyzer
from .contracts import (
    CancellationRequested,
    CancellationToken,
    ProgressEvent,
    TaskRequest,
    TaskResult,
)
from . import checkpoint as checkpoint_io
from . import workspace
from .download_state import DownloadJournal


class _NoNewsItemsError(ValueError):
    """Raised when analysis produces no actionable news entries."""


class _NoSelectedNewsItemsError(_NoNewsItemsError):
    """The document has news, but none in the requested sections."""


class _NewsWatchdogTimeout(TimeoutError):
    """A per-news stage exceeded its configured deadline."""


class PipelineRunner:
    """Run one task with isolated output and strict resumable checkpoints."""

    CHECKPOINT_SCHEMA_VERSION = 1
    _MAX_ISSUE_LABEL_LENGTH = workspace.MAX_ISSUE_LABEL_LENGTH
    _UNSAFE_TASK_LABEL_CHARS = workspace.UNSAFE_TASK_LABEL_CHARS

    def __init__(
        self,
        *,
        parser=None,
        analyzer=None,
        resolver=None,
        history=None,
        config: PrescanConfig | None = None,
        config_path=None,
        source_transport=None,
        image_transport=None,
        adapter_factory: Callable[..., Any] | None = None,
        video_downloader_factory: Callable[..., Any] | None = None,
        video_ffmpeg_detector=None,
        _legacy_output: bool = False,
    ) -> None:
        self.config = config or load_config(config_path)
        self.parser = parser or DocxDocumentParser()
        self.analyzer = analyzer
        self.history = history
        self.source_transport = source_transport
        self.image_transport = image_transport
        self.adapter_factory = adapter_factory
        self.video_downloader_factory = video_downloader_factory
        self.video_ffmpeg_detector = video_ffmpeg_detector
        self._legacy_output = _legacy_output
        self._resolver_injected = resolver is not None
        self.resolver = resolver or self._make_resolver(self.config)
        self._socialdata_response_cache: dict[str, object] = {}
        self._network_runtime = None
        self.network_metrics = {}
        self._x_query_base = 0
        self._download_journal = None
        self._media_cache_version = 1

    def _make_resolver(self, config):
        return DefaultSourceResolver(
            history_lookup=(self.history.sources_for if self.history else None),
            same_domain_depth=config.search.same_domain_depth,
            same_domain_transport=self.source_transport,
            search_max_results=config.search.max_results,
            search_timeout=config.network.search_timeout_seconds,
        )

    def run(self, request, event_sink=None, cancellation_token=None) -> TaskResult:
        """Synchronous desktop/CLI facade, using one task-level event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(request, event_sink, cancellation_token))
        raise RuntimeError("Use await runner.run_async() from an active event loop")

    def supplement_news(self, **kwargs):
        """Collect a reviewer's extra source without rerunning the document."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.supplement_news_async(**kwargs))
        raise RuntimeError("Use await runner.supplement_news_async() from an active event loop")

    async def supplement_news_async(self, **kwargs):
        from .supplement import collect_supplement
        return await collect_supplement(self, kind="supplement", **kwargs)

    async def retry_news_async(
        self, *, task_root, issue_id, news_id, official_url, news_item=None,
        request=None, task_id=None, input_path=None, event_sink=None,
        cancellation_token=None,
    ):
        """Collect an official source through the task's async network runtime."""
        parsed = urlsplit(str(official_url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("official_url must be an absolute HTTP(S) URL")
        root = Path(task_root).expanduser().resolve()
        if request is None:
            request = TaskRequest(
                input_path=input_path or root / "input.docx", issue_id=str(issue_id),
                output_dir=root, no_videos=True,
            )
        if request.issue_id != str(issue_id):
            raise ValueError("retry issue mismatch")
        from .supplement import collect_supplement
        result = await collect_supplement(
            self, task_root=root, issue_id=issue_id, news_id=news_id,
            source_url=official_url, news_item=news_item, request=request,
            task_id=task_id, event_sink=event_sink,
            cancellation_token=cancellation_token, kind="retry",
        )
        return result

    @staticmethod
    async def _await_with_news_watchdog(awaitable, timeout: float, token):
        """Bound one news item while excluding deliberate pause time."""
        task = asyncio.create_task(awaitable)
        remaining = timeout
        try:
            while not task.done():
                interval = 0.05 if token.is_paused else min(0.1, remaining)
                started = time.monotonic()
                await asyncio.wait({task}, timeout=interval)
                elapsed = time.monotonic() - started
                if not token.is_paused:
                    remaining -= elapsed
                if remaining <= 0 and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise _NewsWatchdogTimeout(
                        f"单条新闻处理超过 {timeout:g} 秒"
                    )
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run_persistence(self, operation):
        """Serialize durable state writes on the pool reserved for persistence."""
        runtime = self._network_runtime
        future = runtime.loop.run_in_executor(runtime.persistence, operation)
        timeout = max(5.0, self.config.network.timeout_seconds)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.CancelledError:
            # Cancel queued work if possible; a running atomic write may finish
            # in its daemon worker after this task has unwound.
            future.cancel()
            raise

    def _publication_timeout_seconds(self) -> float:
        """Give publication a bounded budget independent of one HTTP request."""
        return max(180.0, 3.0 * self.config.network.news_timeout_seconds)

    async def run_async(self, request, event_sink=None, cancellation_token=None) -> TaskResult:
        from .network_runtime import TaskNetworkRuntime
        if self._network_runtime is not None:
            raise RuntimeError("A PipelineRunner cannot run overlapping tasks")
        token = cancellation_token or CancellationToken()
        old_supplement_enabled = getattr(self, "_image_supplement_enabled", False)
        self._image_supplement_enabled = bool(getattr(request, "use_image_supplement", False))
        try:
            self._activate_config(self._config_for_request(request))
        except Exception:
            # Preserve the existing setup error result instead of throwing.
            pass
        runtime = TaskNetworkRuntime(self, token)
        self._network_runtime = runtime
        self._localization_service = None
        try:
            result = await self._run_async_body(request, event_sink, token)
        finally:
            self.network_metrics = runtime.stats()
            await runtime.close()
            self._network_runtime = None
            self._image_supplement_enabled = old_supplement_enabled
        # Automatic image supplementation is a separate pass: it must begin only
        # after the ordinary task runtime has fully released its clients.
        if (getattr(request, "use_image_supplement", False)
                and result.status in {"completed", "partial"}
                and result.pipeline_result.issue.news_items):
            try:
                from ..supplement.service import supplement_missing
                await supplement_missing(self, task_root=result.task_dir,
                    issue=result.pipeline_result.issue, request=request,
                    event_sink=event_sink, cancellation_token=token)
            except Exception as exc:
                self._emit(event_sink or (lambda _event: None), result.pipeline_result.failures,
                    self._event("supplement_failed", result.task_id, request.issue_id,
                        message=f"图片补充失败（{type(exc).__name__}）"))
        # Full assets and indexes remain available for review and recovery.
        return result

    async def _resolve_async(self, news, request):
        resolver = self._resolver_for(request)
        if type(resolver) is DefaultSourceResolver:
            return await self._network_runtime.resolve(resolver, news)
        if hasattr(resolver, "resolve_async"):
            return list(await resolver.resolve_async(news))
        return list(await self._network_runtime.call_sync(lambda: list(resolver.resolve(news))))

    async def _run_async_body(
        self,
        request: TaskRequest,
        event_sink: Callable[[ProgressEvent], None] | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> TaskResult:
        token = cancellation_token or CancellationToken()
        sink = event_sink or (lambda _event: None)
        failures: list[FailureRecord] = []
        resumed = bool(request.resume)
        task_id = request.task_id or ""
        task_dir = Path(request.output_dir)
        raw_dir = task_dir
        checkpoint_path = task_dir / "checkpoint.json"
        issue = self._empty_issue(request)
        candidates: list[ImageCandidate] = []
        videos: list[VideoCandidate] = []
        completed: set[str] = set()
        source_map: dict[str, list[SourceRef]] = {}
        input_hash = ""
        config_hash = ""
        journal = None
        media_only_news = set()
        attempted_news = set()
        self._x_query_base = 0
        self._download_journal = None
        self._media_cache_version = 1

        try:
            self._socialdata_response_cache = {}
            config = self._config_for_request(request)
            self._activate_config(config)
            task_dir, raw_dir, checkpoint_path, task_id = self._task_paths(request)
            self._ensure_history(request)
            input_hash = self._sha256(request.input_path)
            config_hash = self._config_hash(config, request)
            if resumed:
                from .compact_output import recover_interrupted_compaction
                recover_interrupted_compaction(task_dir, task_id=task_id)
            self._prepare_task_dir(task_dir, request)
            if not self._legacy_output and not resumed:
                from ..tasks.repository import TaskRepository
                repository = TaskRepository(task_dir)
                repository.activate()
                repository.save_options({
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in vars(request).items()
                } if hasattr(request, "__dict__") else {
                    key: str(getattr(request, key)) if isinstance(getattr(request, key), Path) else getattr(request, key)
                    for key in request.__dataclass_fields__
                })
            self._emit(sink, failures, self._event("task_workspace_ready", task_id, request.issue_id,
                payload={"task_dir": str(task_dir)}, message="任务目录已建立"))
            if resumed:
                checkpoint = self._load_checkpoint(checkpoint_path)
                self._validate_checkpoint(checkpoint, request, input_hash, config_hash, task_id)
                issue = checkpoint.issue
                candidates = list(checkpoint.candidates)
                videos = list(checkpoint.videos)
                failures.extend(checkpoint.failures)
                completed = set(checkpoint.completed_news_ids)
                source_map = {key: list(value) for key, value in checkpoint.source_map.items()}
                attempted_news = set(source_map)
                self._media_cache_version = checkpoint.media_cache_version
                self._x_query_base = checkpoint.x_query_count
                journal = DownloadJournal(task_dir, issue, input_hash, config_hash)
                self._socialdata_response_cache = journal.load_posts()
                self._x_query_base = max(self._x_query_base, len(self._socialdata_response_cache))
                saved, collected_ids = journal.load()
                candidates = journal.merge(candidates, saved)
                for candidate in candidates:
                    journal.confine(candidate)
                    if ImageDownloader.reusable(candidate):
                        candidate.download_status = "downloaded"
                repair_news = {candidate.news_id for candidate in candidates if journal.needs_download(candidate, failures)}
                media_only_news = collected_ids | {candidate.news_id for candidate in candidates}
                repair_video_news = set()
                if not request.offline and not request.no_videos and config.video.enabled:
                    for video in videos:
                        if self._video_needs_recovery(video, failures, task_dir):
                            repair_video_news.add(video.news_id)
                            self._reset_video_for_recovery(video)
                    repair_news.update(repair_video_news)
                    media_only_news.update(repair_video_news)
                completed.difference_update(repair_news)
                missing_media_news = {value.news_id for value in failures if value.code == "download_failed" and value.news_id not in media_only_news}
                for news_id in missing_media_news:
                    self._emit(sink, failures, self._event("download_recovery_unavailable", task_id, issue.issue_id,
                        news_id=news_id, message="旧记录缺少媒体候选，不能自动恢复；不会重复查询付费帖子"))
                if checkpoint.status == "completed" and not repair_news:
                    if checkpoint.result is None:
                        raise ValueError("completed checkpoint has no result")
                    self._emit(sink, failures, self._event("x_media_stats", task_id, issue.issue_id, payload=self._x_media_payload(candidates)))
                    if not self._legacy_output and raw_dir.is_dir():
                        await self._network_runtime.call_sync(
                            lambda: self._export_review_delivery(raw_dir, task_dir),
                            timeout=self._publication_timeout_seconds(),
                        )
                    return TaskResult(
                        status="completed", task_id=task_id, task_dir=task_dir,
                        output_dir=raw_dir, checkpoint_path=checkpoint_path,
                        pipeline_result=checkpoint.result, resumed=True,
                    )
                if checkpoint.status == "failed" and not repair_news and not issue.news_items:
                    result = checkpoint.result or self._pipeline_result(issue, candidates, videos, failures)
                    return TaskResult(
                        status="failed", task_id=task_id, task_dir=task_dir,
                        output_dir=raw_dir, checkpoint_path=checkpoint_path,
                        pipeline_result=result, resumed=True,
                    )
            elif request.task_dir is not None or request.checkpoint_path is not None:
                raise ValueError("task_dir/checkpoint_path are only valid when resuming")
        except Exception as exc:
            message = (str(exc).strip() or f"执行失败（{type(exc).__name__}）")
            if "input mismatch" in message:
                code = "checkpoint_input_mismatch"
            elif "config mismatch" in message:
                code = "checkpoint_config_mismatch"
            elif "schema mismatch" in message:
                code = "checkpoint_schema_mismatch"
            elif "issue mismatch" in message:
                code = "checkpoint_issue_mismatch"
            elif resumed and ("checkpoint" in message or "candidate news id" in message):
                code = "checkpoint_invalid"
            elif isinstance(exc, FileNotFoundError) and "configuration" not in message:
                code = "input_missing"
            else:
                code = "setup_error"
            failure = self._failure(code, message)
            if isinstance(exc, FileNotFoundError) and "configuration" not in message:
                failure = self._failure("input_missing", (str(exc).strip() or f"执行失败（{type(exc).__name__}）"))
            failures.append(failure)
            result = self._pipeline_result(issue, candidates, videos, failures)
            return await self._failed_result(
                request, task_id or request.task_id or request.issue_id,
                task_dir, raw_dir, checkpoint_path, result, failures, sink, resumed,
            )

        self._emit(sink, failures, self._event(
            "task_started", task_id, request.issue_id, message="pipeline started",
        ))
        try:
            await token.wait_if_paused_async()
        except (CancellationRequested, asyncio.CancelledError):
            return await self._cancel(
                request, task_id, task_dir, raw_dir, checkpoint_path, issue,
                input_hash, config_hash, completed, candidates, videos, failures,
                source_map, sink, resumed,
            )

        if not resumed:
            phase = "parse"
            try:
                await token.wait_if_paused_async()
                self._emit(sink, failures, self._event(
                    "parse_started", task_id, request.issue_id, message="parsing DOCX",
                ))
                await token.wait_if_paused_async()
                draft = await self._network_runtime.call_sync(lambda: self.parser.parse(request.input_path, request.issue_id))
                await token.wait_if_paused_async()
                self._emit(sink, failures, self._event(
                    "parse_completed", task_id, request.issue_id, message="DOCX parsed",
                ))
                await token.wait_if_paused_async()
                phase = "analyze"
                self._emit(sink, failures, self._event(
                    "analyze_started", task_id, request.issue_id, message="analyzing news",
                ))
                await token.wait_if_paused_async()
                analyzer = self.analyzer
                if analyzer is None:
                    analyzer = (
                        OpenAINewsAnalyzer(
                            provider=request.llm_provider,
                            model=request.llm_model,
                            api_key=os.getenv("OPENAI_API_KEY"),
                        )
                        if request.llm_provider and request.llm_model
                        else RuleBasedNewsAnalyzer()
                    )
                issue = await self._network_runtime.call_sync(lambda: analyzer.analyze(draft))
                if not issue.news_items:
                    raise _NoNewsItemsError(
                        "No news items were recognized; check the DOCX section and title formatting"
                    )
                original_news_count = len(issue.news_items)
                issue = issue.model_copy(update={"news_items": [
                    item for item in issue.news_items
                    if section_prefix(section_label(item)) in request.selected_sections
                ]})
                if not issue.news_items:
                    raise _NoSelectedNewsItemsError("所选栏目没有可抓取的新闻，请重新选择抓取栏目")
                await token.wait_if_paused_async()
                self._emit(sink, failures, self._event(
                    "analyze_completed", task_id, issue.issue_id, message="news analyzed",
                    payload={"selected_sections": list(request.selected_sections),
                             "selected_news_count": len(issue.news_items),
                             "document_news_count": original_news_count},
                ))
                await token.wait_if_paused_async()
                await self._run_persistence(lambda: self._write_checkpoint(
                    checkpoint_path, status="running", task_id=task_id, issue=issue,
                    input_hash=input_hash, config_hash=config_hash,
                    completed_news_ids=completed, candidates=candidates, videos=videos,
                    failures=failures, source_map=source_map,
                ))
            except (CancellationRequested, asyncio.CancelledError):
                return await self._cancel(
                    request, task_id, task_dir, raw_dir, checkpoint_path, issue,
                    input_hash, config_hash, completed, candidates, videos, failures,
                    source_map, sink, resumed,
                )
            except _NoNewsItemsError as exc:
                self._emit(sink, failures, self._event(
                    f"{phase}_failed", task_id, request.issue_id, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                ))
                failures.append(FailureRecord(
                    stage=FailureStage.ANALYZE,
                    code="no_selected_news_items" if isinstance(exc, _NoSelectedNewsItemsError) else "no_news_items",
                    message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=False,
                ))
                result = self._pipeline_result(issue, candidates, videos, failures)
                return await self._failed_result(
                    request, task_id, task_dir, raw_dir, checkpoint_path, result,
                    failures, sink, resumed, input_hash, config_hash,
                )
            except Exception as exc:
                self._emit(sink, failures, self._event(
                    f"{phase}_failed", task_id, request.issue_id, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                ))
                failures.append(self._failure(f"{phase}_error", (str(exc).strip() or f"执行失败（{type(exc).__name__}）")))
                result = self._pipeline_result(issue, candidates, videos, failures)
                return await self._failed_result(
                    request, task_id, task_dir, raw_dir, checkpoint_path, result,
                    failures, sink, resumed, input_hash, config_hash,
                )

        total_news = len(issue.news_items)
        journal = journal or DownloadJournal(task_dir, issue, input_hash, config_hash)
        self._download_journal = journal
        last_checkpoint = 0.0

        async def commit_checkpoint():
            await self._run_persistence(lambda: self._write_checkpoint(checkpoint_path, status="running", task_id=task_id, issue=issue,
                input_hash=input_hash, config_hash=config_hash, completed_news_ids=completed,
                candidates=candidates, videos=videos, failures=failures, source_map=source_map))

        async def save_downloads(news_id, values):
            # Persistence is serialized by this coordinator, including while
            # paused; filesystem lock waits must not stall the network loop.
            await self._run_persistence(lambda: journal.save(news_id, values))
            if journal.last_save_recovered:
                self._emit(sink, failures, self._event("media_state_recovered", task_id, issue.issue_id,
                    news_id=news_id, message="下载清单被占用，已保存恢复快照；图片下载继续"))

        def media_stats():
            self._emit(sink, failures, self._event("x_media_stats", task_id, issue.issue_id,
                payload=self._x_media_payload(candidates)))

        async def recorded(candidate, failure):
            nonlocal last_checkpoint, failures
            if candidate.download_status == "downloaded":
                failures[:] = [value for value in failures if not (value.stage is FailureStage.DOWNLOAD and value.candidate_id == candidate.id)]
            elif failure is not None and failure not in failures:
                failures.append(failure)
            await save_downloads(candidate.news_id, candidates)
            if time.monotonic() - last_checkpoint >= 1.0 or token.is_paused or token.is_cancelled:
                await commit_checkpoint()
                last_checkpoint = time.monotonic()
            media_stats()

        media_stats()
        try:
            for news_index, news in enumerate(issue.news_items, start=1):
                await token.wait_if_paused_async()
                if news.id in completed:
                    self._emit(sink, failures, self._event(
                        "news_skipped", task_id, issue.issue_id, news_id=news.id,
                        news_index=news_index, total_news=total_news,
                        message="restored from checkpoint",
                    ))
                    await token.wait_if_paused_async()
                    continue
                self._emit(sink, failures, self._event(
                    "news_started", task_id, issue.issue_id, news_id=news.id,
                    news_index=news_index, total_news=total_news, message=news.title,
                ))
                await token.wait_if_paused_async()
                news_sources: list[SourceRef] = []
                news_candidates: list[ImageCandidate] = []
                news_videos: list[VideoCandidate] = []

                async def process_news():
                    nonlocal candidates, videos, failures
                    nonlocal news_sources, news_candidates, news_videos
                    if news.id in media_only_news:
                        news_sources = source_map.get(news.id, [])
                        news_candidates = [candidate for candidate in candidates if candidate.news_id == news.id]
                        if news.id in repair_video_news:
                            news_videos = [video for video in videos if video.news_id == news.id
                                           and self._video_needs_recovery(video, failures, task_dir)]
                        for candidate in news_candidates:
                            if journal.needs_download(candidate, failures) and candidate.download_status != "permanent_failed":
                                candidate.download_status = "pending"
                    else:
                        self._emit(sink, failures, self._event(
                            "resolve_started", task_id, issue.issue_id, news_id=news.id,
                            news_index=news_index, total_news=total_news,
                            message="resolving sources",
                        ))
                        try:
                            await token.wait_if_paused_async()
                            self._network_runtime.resolve_failures = []
                            self._network_runtime.resolve_progress = lambda message: self._emit(
                                sink, failures, self._event("resolve_progress", task_id, issue.issue_id,
                                    news_id=news.id, news_index=news_index, total_news=total_news,
                                    message=message))
                            resolved_sources = await asyncio.wait_for(
                                self._resolve_async(news, request),
                                timeout=self.config.network.news_timeout_seconds,
                            )
                            failures.extend(self._network_runtime.resolve_failures)
                            news_sources = self._filter_sources(resolved_sources, request)
                            source_map[news.id] = news_sources
                            if request.resume and request.use_socialdata_x and self._media_cache_version == 0 and news.id in attempted_news:
                                withheld = [source for source in news_sources if source.source_type is SourceType.OFFICIAL_X
                                            and (match := _STATUS_ID.search(urlsplit(source.url).path))
                                            and match.group(1) not in self._socialdata_response_cache]
                                news_sources = [source for source in news_sources if source not in withheld]
                                for source in withheld:
                                    failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                                        code="resume_media_missing", message="旧记录缺少 X 媒体信息，未重复查询付费帖子", source_url=source.url, retryable=False))
                            await token.wait_if_paused_async()
                            self._emit(sink, failures, self._event(
                                "resolve_completed", task_id, issue.issue_id, news_id=news.id,
                                news_index=news_index, total_news=total_news,
                                message=f"{len(news_sources)} sources",
                            ))
                            await token.wait_if_paused_async()
                        except (CancellationRequested, asyncio.CancelledError):
                            raise
                        except asyncio.TimeoutError as exc:
                            raise _NewsWatchdogTimeout(
                                f"来源解析超过 {self.config.network.news_timeout_seconds:g} 秒"
                            ) from exc
                        except Exception as exc:
                            failures.append(FailureRecord(
                                stage=FailureStage.RESOLVE, news_id=news.id, code="news_failed",
                                message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=True,
                            ))
                            self._emit(sink, failures, self._event(
                                "resolve_failed", task_id, issue.issue_id, news_id=news.id,
                                news_index=news_index, total_news=total_news, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                            ))

                        for source in news_sources:
                            self._emit(sink, failures, self._event(
                                "collect_started", task_id, issue.issue_id, news_id=news.id,
                                news_index=news_index, total_news=total_news, message=source.url,
                            ))
                        collections = await self._network_runtime.collect_many(news, news_sources, request)
                        for source, collection in zip(news_sources, collections):
                            await token.wait_if_paused_async()
                            try:
                                if isinstance(collection, Exception):
                                    raise collection
                                news_candidates.extend(collection.candidates)
                                news_videos.extend(collection.video_candidates)
                                failures.extend(collection.failures)
                                for reason in collection.manual_review_reasons:
                                    failures.append(FailureRecord(
                                        stage=FailureStage.COLLECT, news_id=news.id,
                                        code="manual_review_required", message=reason.value,
                                        source_url=source.url, retryable=False,
                                    ))
                                await token.wait_if_paused_async()
                                self._emit(sink, failures, self._event(
                                    "collect_completed", task_id, issue.issue_id,
                                    news_id=news.id, news_index=news_index,
                                    total_news=total_news, message=source.url,
                                ))
                                await token.wait_if_paused_async()
                            except (CancellationRequested, asyncio.CancelledError):
                                raise
                            except Exception as exc:
                                code = (
                                    "source_timeout"
                                    if isinstance(collection, TimeoutError)
                                    else "source_failed"
                                )
                                failures.append(FailureRecord(
                                    stage=FailureStage.COLLECT, news_id=news.id,
                                    code=code, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                                    source_url=source.url, retryable=True,
                                ))
                                self._emit(sink, failures, self._event(
                                    "collect_failed", task_id, issue.issue_id, news_id=news.id,
                                    news_index=news_index, total_news=total_news,
                                    message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                                ))

                    # Register every candidate before network dispatch. Workers
                    # mutate these records; the single result consumer commits them.
                    if request.offline:
                        importer = ImageDownloader(self.config)
                        for candidate in news_candidates:
                            if candidate.local_path or candidate.original_path:
                                imported, failure, _ = await self._network_runtime.loop.run_in_executor(
                                    self._network_runtime.processing, importer.import_local,
                                    candidate, self._image_stage(task_dir),
                                )
                                if failure:
                                    ImageDownloader._apply_outcome(candidate, failure)
                                    failures.append(failure)
                    candidates = journal.merge(candidates, news_candidates)
                    await save_downloads(news.id, candidates)
                    await commit_checkpoint()
                    media_stats()
                    await token.wait_if_paused_async()
                    self._emit(sink, failures, self._event(
                        "download_started", task_id, issue.issue_id, news_id=news.id,
                        news_index=news_index, total_news=total_news,
                        message="media download",
                    ))
                    await token.wait_if_paused_async()
                    try:
                        if request.offline:
                            for video in news_videos:
                                self._skip_video(video, "offline_mode", failures)
                            await token.wait_if_paused_async()
                        else:
                            accepted, download_failures = await self._network_runtime.download(
                                news_candidates, self._image_stage(task_dir), on_result=recorded,
                            )
                            candidates = [candidate for candidate in candidates if candidate.news_id != news.id]
                            candidates.extend(accepted)
                            failures.extend(value for value in download_failures if value not in failures)
                            # Goods search depends on decoded native assets, not on
                            # X attachment presence or unverified page dimensions.
                            from .goods_collection import search_goods_after_download
                            extra_sources, extra_results = await search_goods_after_download(
                                self._network_runtime, news, news_sources, request, accepted,
                            )
                            extra_candidates = []
                            for collection in extra_results:
                                extra_candidates.extend(collection.candidates)
                                news_videos.extend(collection.video_candidates)
                                failures.extend(collection.failures)
                            if extra_sources:
                                source_map[news.id] = news_sources
                                self._emit(sink, failures, self._event(
                                    "collect_completed", task_id, issue.issue_id, news_id=news.id,
                                    news_index=news_index, total_news=total_news,
                                    message=f"商品补搜新增 {len(extra_sources)} 个来源、{len(extra_candidates)} 张候选图片",
                                ))
                            if extra_candidates:
                                # Persist every newly discovered URL before
                                # download, with the same recovery coordinator.
                                candidates = journal.merge(candidates, extra_candidates)
                                await save_downloads(news.id, candidates)
                                await commit_checkpoint()
                                downloaded, extra_failures = await self._network_runtime.download(
                                    extra_candidates, self._image_stage(task_dir), on_result=recorded,
                                )
                                accepted = journal.merge(accepted, downloaded)
                                candidates = [c for c in candidates if c.news_id != news.id]
                                candidates.extend(accepted)
                                failures.extend(value for value in extra_failures if value not in failures)
                            # A single network request may finish while pause is
                            # requested; honor it immediately after the call.
                            await token.wait_if_paused_async()
                            if request.no_videos or not self.config.video.enabled:
                                for video in news_videos:
                                    self._skip_video(video, "video_download_disabled", failures)
                                videos.extend(news_videos)
                            elif news_videos:
                                await token.wait_if_paused_async()
                                prior_successes = sum(
                                    video.news_id == news.id and self._video_asset_is_valid(video, task_dir)
                                    for video in videos if video.id not in {value.id for value in news_videos}
                                )
                                remaining_video_slots = max(0, self.config.video.max_per_news - prior_successes)
                                downloaded, video_failures = await self._network_runtime.call_sync(
                                    lambda: self._download_videos(news_videos, self._video_stage(task_dir),
                                                                  max_per_news=remaining_video_slots,
                                                                  news_item=news),
                                )
                                attempted_ids = {video.id for video in news_videos}
                                videos = [video for video in videos if video.id not in attempted_ids]
                                videos.extend(downloaded)
                                failures = [failure for failure in failures if not (
                                    failure.stage is FailureStage.DOWNLOAD and failure.candidate_id in attempted_ids
                                )]
                                failures.extend(video_failures)
                                await token.wait_if_paused_async()
                        videos = self._dedupe_videos([*videos, *news_videos])
                        await token.wait_if_paused_async()
                        self._emit(sink, failures, self._event(
                            "download_completed", task_id, issue.issue_id, news_id=news.id,
                            news_index=news_index, total_news=total_news,
                            message="media downloaded",
                        ))
                        await token.wait_if_paused_async()
                    except (CancellationRequested, asyncio.CancelledError):
                        raise
                    except Exception as exc:
                        failures.append(FailureRecord(
                            stage=FailureStage.DOWNLOAD, news_id=news.id,
                            code="download_failed", message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=True,
                        ))
                        self._emit(sink, failures, self._event(
                            "download_failed", task_id, issue.issue_id, news_id=news.id,
                            news_index=news_index, total_news=total_news, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                        ))

                    completed.add(news.id)
                    await save_downloads(news.id, candidates)
                    media_stats()
                    await commit_checkpoint()
                    self._emit(sink, failures, self._event(
                        "news_completed", task_id, issue.issue_id, news_id=news.id,
                        news_index=news_index, total_news=total_news,
                        message="news completed",
                    ))
                    await token.wait_if_paused_async()
                try:
                    watchdog_seconds = max(
                        1.0, 3.0 * self.config.network.news_timeout_seconds,
                    )
                    await self._await_with_news_watchdog(
                        process_news(), watchdog_seconds, token,
                    )
                except (CancellationRequested, asyncio.CancelledError):
                    raise
                except (asyncio.TimeoutError, _NewsWatchdogTimeout) as exc:
                    detail = str(exc).strip() or f"单条新闻处理超过 {watchdog_seconds:g} 秒"
                    message = f"{detail}，已记录并继续下一条"
                    completed.discard(news.id)
                    candidates = journal.merge(candidates, news_candidates)
                    videos = self._dedupe_videos([*videos, *news_videos])
                    failures.extend([
                        FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                            code="news_timeout", message=message, retryable=True),
                        FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                            code="news_failed", message=message, retryable=True),
                    ])
                    await save_downloads(news.id, candidates)
                    media_stats()
                    await commit_checkpoint()
                    self._emit(sink, failures, self._event(
                        "news_failed", task_id, issue.issue_id, news_id=news.id,
                        news_index=news_index, total_news=total_news, message=message,
                    ))
                    continue

            await token.wait_if_paused_async()
            self._emit(sink, failures, self._event(
                "curate_started", task_id, issue.issue_id, message="curating candidates",
            ))
            await token.wait_if_paused_async()
            try:
                result = self._curate_result(
                    issue, candidates, failures, videos, source_map, request.max_images,
                )
            except (CancellationRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                failures.append(self._failure("curate_error", (str(exc).strip() or f"执行失败（{type(exc).__name__}）")))
                self._emit(sink, failures, self._event(
                    "curate_failed", task_id, issue.issue_id, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                ))
                result = self._pipeline_result(issue, candidates, videos, failures)
                return await self._failed_result(
                    request, task_id, task_dir, raw_dir, checkpoint_path, result,
                    failures, sink, resumed, input_hash, config_hash, completed, source_map,
                )
            self._emit(sink, failures, self._event(
                "curate_completed", task_id, issue.issue_id, message="candidates curated",
            ))
            await token.wait_if_paused_async()
            self._emit(sink, failures, self._event(
                "output_started", task_id, issue.issue_id, message="publishing output",
            ))
            await token.wait_if_paused_async()
            try:
                publication = asyncio.create_task(self._network_runtime.call_sync(
                    lambda: self._publish_output(result, raw_dir, task_dir),
                    timeout=self._publication_timeout_seconds()))
                try:
                    await asyncio.shield(publication)
                except asyncio.CancelledError:
                    await publication  # Finish the atomic swap before cleanup.
                    raise
                await token.wait_if_paused_async()
            except (CancellationRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                failures.append(FailureRecord(
                    stage=FailureStage.OUTPUT, code="output_error",
                    message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=True,
                ))
                failure_index = len(failures) - 1
                self._emit(sink, failures, self._event(
                    "output_failed", task_id, issue.issue_id, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                ))
                result.failures = self._merge_failures(
                    [*result.failures, *failures[failure_index:]]
                )
                return await self._failed_result(
                    request, task_id, task_dir, raw_dir, checkpoint_path, result,
                    failures, sink, resumed, input_hash, config_hash, completed, source_map,
                )
            failure_index = len(failures)
            self._emit(sink, failures, self._event(
                "output_completed", task_id, issue.issue_id, message="output published",
            ))
            result.failures = self._merge_failures(
                [*result.failures, *failures[failure_index:]]
            )
            await token.wait_if_paused_async()
            for news in issue.news_items:
                await save_downloads(news.id, result.all_candidates)
            published_ids = {value.id for value in result.all_candidates}
            if any(value.download_status == "downloaded" and value.id not in published_ids for value in candidates):
                raise RuntimeError("已下载图片未进入发布索引，保留暂存文件")
            for value in result.all_candidates:
                if value.download_status == "downloaded" and (
                    not value.original_path or not Path(value.original_path).resolve().is_relative_to(task_dir.resolve())
                    or not ImageDownloader.reusable(value)
                ):
                    raise RuntimeError("已下载原件未正确发布，保留暂存文件")
            await self._run_persistence(lambda: self._write_checkpoint(
                checkpoint_path, status="completed", task_id=task_id, issue=issue,
                input_hash=input_hash, config_hash=config_hash,
                completed_news_ids=completed, candidates=result.all_candidates,
                videos=result.videos, failures=result.failures,
                source_map=source_map, result=result,
            ))
            shutil.rmtree(task_dir / ".work", ignore_errors=True)
            failure_index = len(failures)
            self._emit(sink, failures, self._event(
                "task_completed", task_id, issue.issue_id, message="pipeline completed",
            ))
            result.failures = self._merge_failures([*result.failures, *failures[failure_index:]])
            if len(failures) > failure_index:
                await self._run_persistence(lambda: self._write_checkpoint(
                    checkpoint_path, status="completed", task_id=task_id, issue=issue,
                    input_hash=input_hash, config_hash=config_hash,
                    completed_news_ids=completed, candidates=result.all_candidates,
                    videos=result.videos, failures=result.failures,
                    source_map=source_map, result=result,
                ))
            return TaskResult(
                status="completed", task_id=task_id, task_dir=task_dir,
                output_dir=raw_dir, checkpoint_path=checkpoint_path,
                pipeline_result=result, resumed=resumed,
            )
        except (CancellationRequested, asyncio.CancelledError):
            return await self._cancel(
                request, task_id, task_dir, raw_dir, checkpoint_path, issue,
                input_hash, config_hash, completed, candidates, videos, failures,
                source_map, sink, resumed,
            )
        except Exception as exc:
            failures.append(FailureRecord(
                stage=FailureStage.OUTPUT, code="output_error", message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
                retryable=True,
            ))
            self._emit(sink, failures, self._event(
                "output_failed", task_id, issue.issue_id, message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"),
            ))
            result = self._pipeline_result(issue, candidates, videos, failures)
            return await self._failed_result(
                request, task_id, task_dir, raw_dir, checkpoint_path, result,
                failures, sink, resumed, input_hash, config_hash, completed, source_map,
            )

    def retry_news(
        self,
        *,
        task_root: Path | str,
        issue_id: str,
        news_id: str,
        official_url: str,
        task_id: str | None = None,
        input_path: Path | str | None = None,
        news_item=None,
        request: TaskRequest | None = None,
        event_sink=None,
        cancellation_token=None,
    ) -> str:
        """Synchronous compatibility facade; returns the generated attempt ID."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(self.retry_news_async(
                task_root=task_root, issue_id=issue_id, news_id=news_id,
                official_url=official_url, task_id=task_id, input_path=input_path,
                news_item=news_item, request=request, event_sink=event_sink,
                cancellation_token=cancellation_token,
            ))
            return result.attempt_id
        raise RuntimeError("Use await runner.retry_news_async() from an active event loop")

    def _config_for_request(self, request):
        return load_config(request.config_path) if request.config_path is not None else self.config

    def _activate_config(self, config):
        self.config = config
        if not self._resolver_injected:
            self.resolver = self._make_resolver(config)

    def _collect(self, news, source, *, request=None):
        if getattr(self, "_supplement_mode", False):
            parsed = urlsplit(source.url)
            host = (parsed.hostname or "").casefold()
            if host in {"ymgal.games", "www.ymgal.games"} and parsed.path.startswith("/co/picset/"):
                from ..supplement.providers import YmgalPicsetAdapter
                return YmgalPicsetAdapter(self._page_client()).collect(news, source, CollectionContext(
                    timeout_seconds=self.config.network.timeout_seconds,
                    max_candidates=self.config.search.max_candidates_per_source,
                    now=datetime.now(timezone.utc)))
        if self.adapter_factory is None:
            from ..discovery.goods_service import GoodsSourceService
            if GoodsSourceService.supports(news, source):
                return GoodsSourceService(self._page_client()).collect(news, source, CollectionContext(
                    timeout_seconds=self.config.network.timeout_seconds,
                    max_candidates=self.config.search.max_candidates_per_source,
                    now=datetime.now(timezone.utc)))
        # Explicit review additions can reuse the work-aware adapters in any
        # section; ordinary x/z collection retains its existing behavior.
        host = (urlsplit(source.url).hostname or "").casefold()
        use_work_adapter = section_prefix(section_label(news)) == "h" or getattr(self, "_supplement_mode", False)
        if self.adapter_factory is None and use_work_adapter and host in {"vndb.org", "www.vndb.org", "store.steampowered.com"}:
            from ..localization.service import LocalizationImageService
            # Initialize on the coordinator before parallel source collection.
            service = getattr(self, "_localization_service", None)
            if service is None:
                service = LocalizationImageService(self._page_client())
                self._localization_service = service
            return service.collect(news, source, CollectionContext(
                timeout_seconds=self.config.network.timeout_seconds,
                max_candidates=self.config.search.max_candidates_per_source,
                now=datetime.now(timezone.utc),
            ))
        if self.adapter_factory is not None:
            try:
                adapter = self.adapter_factory(news, source, self.config)
            except TypeError:
                adapter = self.adapter_factory(news, source)
        else:
            adapter = self._default_adapter(source, request=request)
        return adapter.collect(
            news, source,
            CollectionContext(
                timeout_seconds=self.config.network.timeout_seconds,
                max_candidates=self.config.search.max_candidates_per_source,
                now=datetime.now(timezone.utc),
            ),
        )

    @staticmethod
    def _bind_localization_context(news, candidates):
        if section_prefix(section_label(news)) != "h":
            return
        from ..domain import LocalizationContext
        context = news.localization_context or LocalizationContext(source_urls=list(news.source_urls))
        for candidate in candidates:
            proof = candidate.localization_provenance
            if proof is None or proof.binding != "confirmed":
                continue
            if not ImageCurator._source_linked(news, candidate):
                continue
            target = context.vndb_ids if proof.source == "vndb" else context.steam_app_ids if proof.source == "steam" else None
            # An established identity is never replaced by a discovered page.
            if target and proof.work_id not in target:
                continue
            if target is not None and proof.work_id not in target:
                target.append(proof.work_id)
        news.localization_context = context

    def _default_adapter(self, source, *, request=None):
        if source.source_type is SourceType.VIDEO:
            return VideoAdapter()
        if ReviewReason.DYNAMIC_PAGE in source.review_reasons:
            return DynamicPageAdapter(client=self._page_client())
        path = urlsplit(source.url).path.casefold()
        if source.source_type is SourceType.DIRECT_IMAGE or path.endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
        ):
            return DirectImageAdapter()
        if source.source_type is SourceType.STEAM:
            return SteamAdapter(client=self._page_client())
        if source.source_type is SourceType.OFFICIAL_X:
            runtime = self._network_runtime
            kwargs = {}
            if runtime is not None:
                kwargs["socialdata_transport"] = runtime.socialdata_lookup
            adapter = create_x_adapter(
                public_transport=self.source_transport,
                use_socialdata=bool(request and request.use_socialdata_x),
                response_cache=self._socialdata_response_cache,
                credential_store=getattr(self, "_supplement_credential_store", None),
                **kwargs,
            )
            if runtime is not None:
                adapter.public_client = runtime.page_bridge
                adapter.entity_client = runtime.page_bridge
            return adapter
        return OfficialHtmlAdapter(client=self._page_client())

    def _page_client(self):
        if self._network_runtime is not None:
            return self._network_runtime.page_bridge
        from ..discovery.http import SafeHttpClient
        return SafeHttpClient(transport=self.source_transport, timeout=20.0, max_retries=2)

    def _download_videos(self, candidates, output_dir, *, max_per_news=None, news_item=None):
        video_config = self.config.video
        if max_per_news is not None:
            video_config = video_config.model_copy(update={"max_per_news": max_per_news})
        if self.video_downloader_factory is None:
            from ..video import VideoDownloader
            downloader = VideoDownloader(video_config, ffmpeg_detector=self.video_ffmpeg_detector)
            return downloader.download(candidates, output_dir, news_item=news_item)
        else:
            try:
                downloader = self.video_downloader_factory(
                    video_config, ffmpeg_detector=self.video_ffmpeg_detector,
                )
            except TypeError:
                downloader = self.video_downloader_factory(video_config)
        return downloader.download(candidates, output_dir)

    def _curate_result(self, issue, candidates, failures, videos, source_map, max_images):
        curated = ImageCurator(self.config).curate(issue, candidates, self.history)
        failures.extend(curated.failures)
        if max_images is not None:
            for news_id in {candidate.news_id for candidate in curated.candidates}:
                selected = [
                    candidate for candidate in curated.candidates
                    if candidate.news_id == news_id and candidate.selected
                ]
                for candidate in selected[max_images:]:
                    candidate.selected = False
        result = PipelineResult(
            issue=issue, candidates=curated.candidates,
            filtered_candidates=curated.filtered_candidates,
            failures=self._merge_failures(failures),
            videos=self._dedupe_videos(videos),
        )
        result.review_required = [c for c in result.all_candidates if c.review_reasons]
        if self.history is not None:
            for news in issue.news_items:
                try:
                    self.history.record_news_result(NewsResult(
                        news_item=news, sources=source_map.get(news.id, []),
                        candidates=[c for c in result.all_candidates if c.news_id == news.id],
                        failures=[f for f in result.failures if f.news_id == news.id],
                    ))
                except Exception as exc:
                    result.failures.append(FailureRecord(
                        stage=FailureStage.HISTORY, news_id=news.id,
                        code="history_error", message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=True,
                    ))
        result.failures = self._merge_failures(result.failures)
        return result

    def _publish_output(self, result, raw_dir, task_dir):
        if self._legacy_output:
            return OutputManager().write(result, raw_dir)
        work = task_dir / ".work"
        work.mkdir(parents=True, exist_ok=True)
        stage = work / f"publish-{uuid.uuid4().hex}"
        backup = task_dir / f".raw-backup-{uuid.uuid4().hex}"
        # Delivery mutates candidates while copying/converting. Retain their
        # identities, but restore their metadata if staging or publication fails.
        media = {id(item): item for item in [*result.all_candidates, *result.review_required, *result.videos]}
        previous = [(item, copy.deepcopy(item.__dict__)) for item in media.values()]
        previous_failures = list(result.failures)
        try:
            manifest = OutputManager().write(result, stage)
            self._rewrite_paths(result, stage, raw_dir)
            rebase_asset_paths(manifest, stage, raw_dir)
            # Rewrite persisted payloads before the atomic directory swap. Any
            # write failure therefore leaves the previous raw output intact.
            for index in stage.glob("*.json"):
                payload = json.loads(index.read_text(encoding="utf-8"))
                atomic_json_write(index, rebase_asset_paths(payload, stage, raw_dir))
            if raw_dir.exists():
                os.replace(raw_dir, backup)
            os.replace(stage, raw_dir)
        except Exception:
            if not raw_dir.exists() and backup.exists():
                os.replace(backup, raw_dir)
            for item, metadata in previous:
                item.__dict__.clear()
                item.__dict__.update(metadata)
            result.failures[:] = previous_failures
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        from ..tasks.repository import TaskRepository
        if TaskRepository.is_authoritative(task_dir):
            # JSON is a publication snapshot only. Commit converted assets
            # before review/export reads the authoritative database.
            def snapshot(name, default):
                path = raw_dir / name
                return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
            TaskRepository(task_dir, create=False).save_index(
                snapshot("image_index.json", {}), snapshot("video_index.json", {}),
                snapshot("review_required.json", []), snapshot("failed_items.json", []))
        self._export_review_delivery(raw_dir, task_dir)
        return manifest

    @staticmethod
    def _export_review_delivery(raw_dir, task_dir):
        from ..review.session import ReviewSession
        return ReviewSession.from_output(raw_dir, task_root=task_dir,
            state_path=task_dir / "review_state.json").export_final()

    @staticmethod
    def _rewrite_paths(result, old_root, new_root):
        rebase_asset_paths(result, Path(old_root), Path(new_root))

    async def _cancel(
        self, request, task_id, task_dir, raw_dir, checkpoint_path, issue,
        input_hash, config_hash, completed, candidates, videos, failures,
        source_map, sink, resumed,
    ):
        try:
            await self._run_persistence(lambda: self._write_checkpoint(
                checkpoint_path, status="cancelled", task_id=task_id, issue=issue,
                input_hash=input_hash, config_hash=config_hash,
                completed_news_ids=completed, candidates=candidates, videos=videos,
                failures=failures, source_map=source_map,
            ))
        except Exception as exc:
            failures.append(self._failure("checkpoint_error", (str(exc).strip() or f"执行失败（{type(exc).__name__}）")))
        self._emit(sink, failures, self._event(
            "task_cancelled", task_id, issue.issue_id, message="cancelled",
        ))
        result = self._pipeline_result(issue, candidates, videos, failures)
        return TaskResult(
            status="cancelled", task_id=task_id, task_dir=task_dir,
            output_dir=raw_dir, checkpoint_path=checkpoint_path,
            pipeline_result=result, resumed=resumed,
        )

    async def _failed_result(
        self, request, task_id, task_dir, raw_dir, checkpoint_path, result,
        failures, sink, resumed, input_hash="", config_hash="",
        completed=None, source_map=None,
    ):
        task_id = task_id or request.task_id or request.issue_id
        failure_index = len(failures)
        self._emit(sink, failures, self._event(
            "task_failed", task_id, request.issue_id, message="pipeline failed",
        ))
        if self._legacy_output:
            image_root = Path(raw_dir) / "images"
            preserve_existing_images = image_root.is_dir() and any(image_root.iterdir())
            if not preserve_existing_images:
                try:
                    OutputManager().write(result, raw_dir)
                except Exception as exc:
                    failures.append(self._failure("output_error", (str(exc).strip() or f"执行失败（{type(exc).__name__}）")))
        result.failures = self._merge_failures(
            [*result.failures, *failures[failure_index:]]
        )
        try:
            if input_hash and config_hash and checkpoint_path.parent.exists():
                await self._run_persistence(lambda: self._write_checkpoint(
                    checkpoint_path, status="failed", task_id=task_id, issue=result.issue,
                    input_hash=input_hash, config_hash=config_hash,
                    completed_news_ids=completed or set(), candidates=result.all_candidates,
                    videos=result.videos, failures=result.failures,
                    source_map=source_map or {}, result=result,
                ))
        except Exception:
            pass
        return TaskResult(
            status="failed", task_id=task_id, task_dir=task_dir,
            output_dir=raw_dir, checkpoint_path=checkpoint_path,
            pipeline_result=result, resumed=resumed,
        )

    def _pipeline_result(self, issue, candidates, videos, failures):
        result = PipelineResult(
            issue=issue, candidates=list(candidates),
            videos=self._dedupe_videos(videos),
            failures=self._merge_failures(failures),
        )
        result.review_required = [c for c in result.all_candidates if c.review_reasons]
        return result

    def _task_paths(self, request):
        paths = workspace.resolve_task_paths(request, legacy_output=self._legacy_output)
        return paths.task_dir, paths.raw_dir, paths.checkpoint_path, paths.task_id

    @classmethod
    def _safe_task_label(cls, issue_id: str) -> str:
        return workspace.safe_task_label(issue_id)

    @staticmethod
    def _within(path, root):
        return workspace.within(path, root)

    def _prepare_task_dir(self, task_dir, request):
        workspace.prepare_task_dir(task_dir, request, legacy_output=self._legacy_output)

    def _image_stage(self, task_dir):
        return workspace.image_stage(task_dir)

    def _video_stage(self, task_dir):
        return workspace.video_stage(task_dir)

    def _resolver_for(self, request):
        if request.offline and not self._resolver_injected:
            return DefaultSourceResolver(
                history_lookup=(self.history.sources_for if self.history else None),
                same_domain_depth=0, same_domain_transport=self.source_transport,
                search_provider=lambda _news: [],
                search_max_results=self.config.search.max_results,
                search_timeout=self.config.network.search_timeout_seconds,
            )
        return self.resolver

    @staticmethod
    def _filter_sources(sources, request):
        if not request.offline:
            return list(sources)
        result = []
        for source in sources:
            path = urlsplit(source.url).path.casefold()
            if source.source_type in {SourceType.VIDEO, SourceType.DIRECT_IMAGE} or path.endswith(
                (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
            ):
                result.append(source)
        return result

    def _ensure_history(self, request):
        if self.history is not None or request.history_db is None:
            return
        from ..delivery.history import SQLiteHistoryStore
        self.history = SQLiteHistoryStore(request.history_db)
        if not self._resolver_injected:
            self.resolver = self._make_resolver(self.config)

    @staticmethod
    def _empty_issue(request):
        return Issue(issue_id=request.issue_id, input_path=str(request.input_path), news_items=[])

    @staticmethod
    def _sha256(path):
        return checkpoint_io.sha256_path(path)

    def _config_hash(self, config=None, request=None):
        return checkpoint_io.config_hash(config or self.config, request)

    def _write_checkpoint(
        self, path, *, status, task_id, issue, input_hash, config_hash,
        completed_news_ids, candidates, videos, failures, source_map, result=None,
    ):
        checkpoint_io.write_checkpoint(
            path,
            status=status,
            task_id=task_id,
            issue=issue,
            input_hash=input_hash,
            config_hash_value=config_hash,
            completed_news_ids=completed_news_ids,
            candidates=candidates,
            videos=videos,
            failures=failures,
            source_map=source_map,
            result=result,
            x_query_count=self._x_query_base + (self._network_runtime.socialdata.request_count if self._network_runtime else 0),
            media_cache_version=self._media_cache_version,
            schema_version=self.CHECKPOINT_SCHEMA_VERSION,
            dedupe_videos=self._dedupe_videos,
            merge_failures=self._merge_failures,
        )

    def _load_checkpoint(self, path):
        return checkpoint_io.load_checkpoint(path)

    def _validate_checkpoint(self, checkpoint, request, input_hash, config_hash, task_id):
        checkpoint_io.validate_checkpoint(
            checkpoint,
            request,
            input_hash,
            config_hash,
            task_id,
            schema_version=self.CHECKPOINT_SCHEMA_VERSION,
        )

    @staticmethod
    def _validate_checkpoint_associations(checkpoint):
        checkpoint_io.validate_checkpoint_associations(checkpoint)

    @staticmethod
    def _dedupe_videos(videos):
        unique = {}
        for video in videos or []:
            unique.setdefault(video.id or video.video_url, video)
        return list(unique.values())

    @classmethod
    def _video_asset_is_valid(cls, video, task_dir):
        if video.status is not VideoStatus.DOWNLOADED or not video.local_path or not video.sha256:
            return False
        path = Path(video.local_path)
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(Path(task_dir).resolve()) or not resolved.is_file():
                return False
            digest = checkpoint_io.sha256_path(resolved)
        except OSError:
            return False
        return digest.casefold() == str(video.sha256).casefold()

    @classmethod
    def _video_needs_recovery(cls, video, failures, task_dir):
        if cls._video_asset_is_valid(video, task_dir):
            return False
        if video.status in {VideoStatus.DISCOVERED, VideoStatus.DOWNLOADING}:
            return True
        if video.status is VideoStatus.FAILED:
            return any(failure.stage is FailureStage.DOWNLOAD
                       and failure.candidate_id == video.id and failure.retryable
                       for failure in failures)
        if video.status is VideoStatus.SKIPPED:
            return video.failure_reason in {"height_exceeded", "file_size_exceeded"}
        return video.status is VideoStatus.DOWNLOADED

    @staticmethod
    def _reset_video_for_recovery(video):
        if video.status is VideoStatus.DOWNLOADED:
            video.local_path = None
            video.sha256 = None
        video.status = VideoStatus.DISCOVERED
        video.failure_reason = None

    @staticmethod
    def _merge_failures(failures):
        unique = {}
        for failure in failures or []:
            key = (
                failure.stage.value, failure.news_id, failure.candidate_id,
                failure.code, failure.message, failure.source_url,
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

    @staticmethod
    def _skip_video(video, reason, failures):
        video.status = VideoStatus.SKIPPED
        video.failure_reason = reason
        if reason not in video.review_reasons:
            video.review_reasons.append(reason)
        failures.append(FailureRecord(
            stage=FailureStage.DOWNLOAD, news_id=video.news_id,
            candidate_id=video.id,
            code="offline_video_skip" if reason == "offline_mode" else reason,
            message=reason.replace("_", " "), source_url=video.source_url,
            retryable=False,
        ))

    @staticmethod
    def _failure(code, message):
        return FailureRecord(stage=FailureStage.PARSE, code=code, message=str(message).strip() or "执行失败，未提供异常说明", retryable=False)

    def _x_media_payload(self, candidates):
        photos = [value for value in candidates if value.signals.get("x_api_photo") or value.signals.get("socialdata_photo")]
        return {"x_queries": self._x_query_base + (self._network_runtime.socialdata.request_count if self._network_runtime else 0),
            "x_attachments": len(photos), "x_downloaded": sum(value.download_status == "downloaded" for value in photos),
            "x_pending": sum(value.download_status in {"pending", "retryable_failed"} for value in photos),
            "x_failed": sum(value.download_status in {"retryable_failed", "permanent_failed"} for value in photos)}

    @staticmethod
    def _event(kind, task_id, issue_id, *, news_id=None, news_index=None, total_news=None, message="", payload=None):
        return ProgressEvent(
            kind=kind, task_id=task_id, issue_id=issue_id, news_id=news_id,
            news_index=news_index, total_news=total_news, message=message, payload=payload or {},
        )

    @staticmethod
    def _emit(sink, failures, event):
        try:
            sink(event)
        except Exception as exc:
            failures.append(FailureRecord(
                stage=FailureStage.OUTPUT, code="event_sink_failed",
                message=(str(exc).strip() or f"执行失败（{type(exc).__name__}）"), retryable=False,
            ))
