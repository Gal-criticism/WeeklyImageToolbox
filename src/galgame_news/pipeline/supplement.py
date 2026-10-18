"""Image-only, recoverable source additions made during manual review."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from ..curation import ImageCurator, ImageDownloader
from ..delivery.helpers import atomic_json_write
from ..delivery.output import OutputManager
from ..discovery.resolver import DefaultSourceResolver, _normalize
from ..domain import DiscoveryMethod, FailureRecord, FailureStage, ImageCandidate, ImageCurationStatus, Issue, NewsItem, SourceRef, SourceType
from .contracts import CancellationRequested, CancellationToken, ProgressEvent, TaskRequest
from .download_state import DownloadJournal
from .network_runtime import TaskNetworkRuntime


@dataclass
class SupplementResult:
    news_id: str
    attempt_id: str
    path: Path
    status: str
    candidates: list[ImageCandidate]
    failures: list[FailureRecord]

    @property
    def success_count(self):
        return sum(bool(c.local_path) and c.download_status == "downloaded" for c in self.candidates)

    @property
    def failed_count(self):
        return sum(c.download_status in {"pending", "retryable_failed", "permanent_failed"} for c in self.candidates)

    @property
    def added_images(self):
        return [str(c.id) for c in self.candidates]

    @property
    def status_counts(self):
        return {
            "selected": sum(bool(c.selected) for c in self.candidates),
            "downloaded": sum(c.download_status == "downloaded" for c in self.candidates),
            "pending": sum(c.download_status == "pending" for c in self.candidates),
            "retryable_failed": sum(c.download_status == "retryable_failed" for c in self.candidates),
            "permanent_failed": sum(c.download_status == "permanent_failed" for c in self.candidates),
        }


def _read(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _news(root, issue_id, news_id, news_item, *, strict_checkpoint=False):
    checkpoint_path = root / "checkpoint.json"
    from ..tasks.repository import TaskRepository
    if TaskRepository.is_authoritative(root):
        checkpoint_path = root / "task_state.sqlite3"
    if strict_checkpoint:
        has_checkpoint = checkpoint_path.is_file()
        if checkpoint_path.suffix == ".sqlite3" and has_checkpoint:
            has_checkpoint = TaskRepository(root, create=False).load_checkpoint() is not None
        if has_checkpoint:
            # Present checkpoints are critical task state and must validate.
            from .checkpoint import load_checkpoint
            checked = load_checkpoint(checkpoint_path)
            checkpoint = checked.model_dump(mode="json")
        else:
            # Imported task folders may have no checkpoint. Use only a real
            # NewsItem supplied by the caller; never synthesize display data.
            checkpoint = {}
    else:
        if checkpoint_path.suffix == ".sqlite3":
            from .checkpoint import load_checkpoint
            payload = TaskRepository(root, create=False).load_checkpoint()
            checkpoint = load_checkpoint(checkpoint_path).model_dump(mode="json") if payload is not None else {}
        else:
            checkpoint = _read(checkpoint_path, {})
    if checkpoint and checkpoint.get("issue_id") != issue_id:
        raise ValueError("补抓任务期号与检查点不一致")
    data = next((n for n in checkpoint.get("issue", {}).get("news_items", []) if n.get("id") == news_id), None)
    if data is None and isinstance(news_item, NewsItem):
        if strict_checkpoint and (news_item.id != news_id or news_item.issue_id != issue_id):
            raise ValueError("retry news item identity mismatch")
        data = news_item.model_dump(mode="json")
    if data is None and isinstance(news_item, dict):
        if strict_checkpoint and (str(news_item.get("news_id") or news_item.get("id") or "") != news_id
                                  or not news_item.get("title") or not news_item.get("section")):
            raise ValueError("重试缺少当前新闻的真实标题、栏目或标识")
        data = dict(news_item)
        data.pop("news_id", None)
        data.pop("candidates", None)
        # Review index rows contain only display metadata. Reconstruct a news
        # record from it rather than inventing a title or section.
        data = {key: value for key, value in data.items() if key in NewsItem.model_fields}
        data.setdefault("issue_id", issue_id)
        data.setdefault("body", "")
    if data is None:
        raise ValueError("未找到当前新闻的标题与栏目，无法补抓")
    news = NewsItem.model_validate(data)
    if news.issue_id != issue_id:
        raise ValueError("retry news item issue mismatch")
    if news.id != news_id:
        object.__setattr__(news, "id", news_id)
    return news.model_copy(deep=True), checkpoint


def _prior_candidates(root, news_id, source_url, kind):
    """Reuse only indexed assets belonging to this task and this news."""
    records = []
    diagnostics = []
    from ..tasks.repository import TaskRepository
    if TaskRepository.is_authoritative(root):
        repository = TaskRepository(root, create=False)
        rows = list(repository.iter_candidates(news_id=news_id))
        prior_sources = None
        for batch in repository.list_batches(news_id=news_id):
            if batch.get("kind") == kind and batch.get("source_url") == source_url:
                prior_sources = [SourceRef.model_validate(value) for value in batch.get("sources", [])]
        return {row["id"]: ImageCandidate.model_validate(row) for row in rows}, prior_sources, diagnostics
    raw_index = root / "raw" / "image_index.json"
    payload = _read(raw_index, {})
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates", []), list):
        raise ValueError(f"invalid critical raw image index: {raw_index}")
    records.extend(("raw", value) for value in payload.get("candidates", []))
    attempts_root = (root / "retries" / news_id).resolve()
    attempts = sorted(attempts_root.glob("*/image_index.json"), key=lambda p: p.stat().st_mtime_ns) if attempts_root.is_dir() else []
    prior_sources = None
    for path in attempts:
        if not path.resolve().is_relative_to(root):
            continue
        try:
            payload = _read(path, {})
            if not isinstance(payload, dict) or payload.get("kind") != kind:
                continue
            batch_records = payload.get("candidates", [])
            if not isinstance(batch_records, list):
                raise ValueError("candidate list is not an array")
            records.extend((str(path), value) for value in batch_records)
            if payload.get("source_url") == source_url:
                candidate_sources = payload.get("sources", [])
                if not isinstance(candidate_sources, list):
                    raise ValueError("source list is not an array")
                try:
                    prior_sources = [SourceRef.model_validate(value) for value in candidate_sources]
                except Exception as exc:
                    prior_sources = None
                    diagnostics.append(FailureRecord(stage=FailureStage.COLLECT,
                        news_id=news_id, code="prior_retry_batch_invalid",
                        message=f"历史来源记录已跳过（{type(exc).__name__}）",
                        source_url=source_url, retryable=False))
        except Exception as exc:
            diagnostics.append(FailureRecord(stage=FailureStage.COLLECT,
                news_id=news_id, code="prior_retry_batch_invalid",
                message=f"历史重试批次已跳过（{type(exc).__name__}）",
                source_url=source_url, retryable=False))
    by_id = {}
    for batch, raw in records:
        try:
            if not isinstance(raw, dict):
                raise ValueError("candidate entry is not an object")
            if raw.get("news_id") != news_id:
                continue
            candidate = ImageCandidate.model_validate(raw)
            if any(value and not Path(value).resolve().is_relative_to(root) for value in (candidate.local_path, candidate.original_path)):
                continue
            old = by_id.get(candidate.id)
            if old is None or ImageDownloader.reusable(candidate) or not ImageDownloader.reusable(old):
                by_id[candidate.id] = candidate
        except Exception as exc:
            diagnostics.append(FailureRecord(stage=FailureStage.COLLECT,
                news_id=news_id, code="prior_candidate_invalid",
                message=f"历史候选记录已跳过（{type(exc).__name__}）",
                source_url=source_url, retryable=False))
    return by_id, prior_sources, diagnostics


async def collect_supplement(runner, *, task_root, issue_id, news_id, source_url,
                             news_item=None, request=None, task_id=None,
                             event_sink=None, cancellation_token=None, kind="supplement"):
    if kind not in {"supplement", "retry"}:
        raise ValueError("unknown review collection kind")
    if runner._network_runtime is not None:
        raise RuntimeError("本任务已有抓取操作正在运行")
    root = Path(task_root).resolve()
    news_id, issue_id = str(news_id), str(issue_id)
    if not news_id or Path(news_id).name != news_id or news_id in {".", ".."} or ":" in news_id:
        raise ValueError("新闻标识不合法")
    source_url = _normalize(str(source_url).strip())
    news, checkpoint = _news(root, issue_id, news_id, news_item, strict_checkpoint=kind == "retry")
    request = request or TaskRequest(input_path=root / "input.docx", issue_id=issue_id, output_dir=root, no_videos=True)
    if request.issue_id != issue_id:
        raise ValueError("补抓请求与任务期号不一致")
    host = (urlsplit(source_url).hostname or "").casefold()
    is_x = host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
    if is_x and not request.use_socialdata_x:
        raise ValueError("本任务未启用 SocialData，不能付费补抓 X；请在启用 X API 的任务中操作")
    if is_x:
        from ..discovery.x_api import _STATUS_ID
        if not _STATUS_ID.search(urlsplit(source_url).path):
            raise ValueError("请填写具体 X 帖子的链接")
    if request.offline:
        raise ValueError("本任务为离线模式，不能抓取补充网络链接")
    config = runner._config_for_request(request)
    # Review additions never invoke optional inference or modify videos.
    config = config.model_copy(deep=True)
    config.visual_analysis.enabled = False
    runner._activate_config(config)
    token = cancellation_token or CancellationToken()
    runtime = TaskNetworkRuntime(runner, token)
    runner._network_runtime = runtime
    runner._localization_service = None
    old_supplement_mode = getattr(runner, "_supplement_mode", False)
    runner._supplement_mode = kind == "supplement"
    old_journal = runner._download_journal
    failures, candidates, sources = [], [], []
    attempt_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    attempt_root = root / "retries" / news_id / attempt_id
    status = "running"
    sink = event_sink or (lambda event: None)

    def emit(message):
        sink(ProgressEvent("supplement_progress", str(task_id or request.task_id or ""), issue_id,
            news_id=news_id, message=message, payload={"success_count": sum(bool(c.local_path) for c in candidates),
                                                     "candidate_count": len(candidates)}))

    def save():
        from ..tasks.repository import TaskRepository
        if TaskRepository.is_authoritative(root):
            TaskRepository(root, create=False).save_batch(news_id, attempt_id, str(attempt_root), kind, status,
                [c.model_dump(mode="json") for c in candidates], [], [f.model_dump(mode="json") for f in failures],
                source_url, [s.model_dump(mode="json") for s in sources])
            return
        atomic_json_write(attempt_root / "image_index.json", {
            "schema_version": 1, "kind": kind, "source_url": source_url,
            "status": status, "issue_id": issue_id,
            "news_items": [{"news_id": news_id, "title": news.title, "sequence": news.sequence, "section": news.section}],
            "sources": [s.model_dump(mode="json") for s in sources],
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "failures": [f.model_dump(mode="json") for f in failures],
        }, ensure_parent=True)
        atomic_json_write(attempt_root / "video_index.json", {"schema_version": 1, "videos": []})
        atomic_json_write(attempt_root / "failed_items.json", [f.model_dump(mode="json") for f in failures])

    try:
        # The public client validates even a source supplied to an injected adapter.
        await runtime.call_sync(lambda: runtime.page_client.validate_url(source_url))
        prior, prior_sources, prior_failures = await runtime.call_sync(lambda: _prior_candidates(root, news_id, source_url, kind))
        failures.extend(prior_failures)
        cache_identity = (checkpoint.get("input_sha256") or hashlib.sha256(str(root).encode()).hexdigest(),
                          checkpoint.get("config_sha256") or runner._config_hash(config, request))
        runner._download_journal = DownloadJournal(root, Issue(issue_id=issue_id, input_path=str(request.input_path), news_items=[news]), *cache_identity)
        runner._socialdata_response_cache = runner._download_journal.load_posts()
        original_urls = list(news.source_urls)
        resolver_news = news.model_copy(deep=True)
        resolver_news.source_urls = [source_url]
        object.__setattr__(resolver_news, "id", news_id)
        # No search or history expansion: navigation is anchored to the extra URL.
        resolver = DefaultSourceResolver(same_domain_depth=config.search.same_domain_depth,
            search_provider=lambda item: [], history_lookup=lambda item: [])
        if prior_sources is not None:
            sources = prior_sources
        elif runner.adapter_factory is not None:
            sources = [resolver._document_source(source_url)]
        else:
            emit("正在发现补充页面及相关图库…")
            sources = await runtime.resolve(resolver, resolver_news)
        sources = [s for s in sources if s.source_type is not SourceType.VIDEO][:16]
        if not sources:
            raise ValueError("该链接不是支持的图片来源")
        for source in sources:
            source.discovered_via = DiscoveryMethod.MANUAL if _normalize(source.url) == source_url else DiscoveryMethod.SAME_DOMAIN
            # Being submitted by a reviewer does not establish official status.
            if host not in {(urlsplit(url).hostname or "").casefold() for url in original_urls}:
                source.officiality = min(source.officiality, 0.4)
                source.requires_review = True
        news.source_urls = list(dict.fromkeys([*original_urls, source_url]))
        object.__setattr__(news, "id", news_id)
        attempt_root.mkdir(parents=True, exist_ok=False)
        provenance_key = "supplement_source_url" if kind == "supplement" else "retry_source_url"
        candidates = [c.model_copy(deep=True) for c in prior.values() if c.signals.get(provenance_key) == source_url] if prior_sources is not None else []
        if not candidates:
            emit("正在采集补充图片…")
            collections = await runtime.collect_many(news, sources, request)
            by_id = {}
            for source, result in zip(sources, collections):
                token.raise_if_cancelled()
                if isinstance(result, Exception):
                    failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news_id,
                        code="supplement_source_failed", message=f"补充来源采集失败（{type(result).__name__}）", source_url=source.url, retryable=True))
                    continue
                failures.extend(result.failures)
                for candidate in result.candidates:
                    if candidate.news_id != news_id:
                        continue
                    existing = prior.get(candidate.id)
                    by_id[candidate.id] = existing.model_copy(deep=True) if existing and ImageDownloader.reusable(existing) else candidate
            candidates = list(by_id.values())
        for candidate in candidates:
            candidate.signals[provenance_key] = source_url
            candidate.signals[f"{kind}_attempt_id"] = attempt_id
            if kind == "supplement":
                candidate.selected = False
        save()  # All candidates exist on disk before the first media request.
        emit("正在下载补充图片…" if kind == "supplement" else "正在重试图片采集…")
        pending = [c for c in candidates if not ImageDownloader.reusable(c) and c.download_status != "permanent_failed"]
        async def on_result(candidate, failure):
            if failure:
                failures.append(failure)
            save()
            emit("补充图片下载处理中…")
        if pending:
            await runtime.download(pending, attempt_root / "images", on_result=on_result)
        if kind == "retry":
            from .goods_collection import search_goods_after_download
            emit("正在检查高清商品图并补搜来源…")
            _, extra_results = await search_goods_after_download(runtime, news, sources, request, candidates)
            by_id = {c.id: c for c in candidates}
            additions = []
            for collection in extra_results:
                failures.extend(collection.failures)
                for candidate in collection.candidates:
                    if candidate.id in by_id or candidate.news_id != news_id:
                        continue
                    existing = prior.get(candidate.id)
                    if existing and ImageDownloader.reusable(existing):
                        candidate = existing.model_copy(deep=True)
                    candidate.signals[provenance_key] = source_url
                    candidate.signals[f"{kind}_attempt_id"] = attempt_id
                    by_id[candidate.id] = candidate
                    additions.append(candidate)
            candidates = list(by_id.values())
            save()
            if additions:
                emit(f"商品补搜新增 {len(additions)} 张候选，正在下载…")
                await runtime.download(additions, attempt_root / "images", on_result=on_result)
        token.raise_if_cancelled()
        status = "completed"
    except (CancellationRequested, asyncio.CancelledError):
        # A pre-cancelled task can stop before an attempt directory is made.
        # Still produce a valid empty cancelled manifest during finalization.
        if not attempt_root.is_dir():
            attempt_root.mkdir(parents=True, exist_ok=False)
        status = "cancelled"
    except Exception as exc:
        if not attempt_root.is_dir():
            raise
        status = "failed"
        failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news_id,
            code="supplement_failed", message=f"补充图片处理失败（{type(exc).__name__}）",
            source_url=source_url, retryable=True))
    finally:
        try:
            if attempt_root.is_dir():
                # CPU work and format conversion run on the existing bounded pool.
                def prepare():
                    nonlocal candidates
                    issue = Issue(issue_id=issue_id, input_path=str(request.input_path), news_items=[news])
                    curated = ImageCurator(config).curate(issue, candidates, runner.history if kind == "retry" else None)
                    failures.extend(curated.failures)
                    candidates = [*curated.candidates, *curated.filtered_candidates]
                    for candidate in candidates:
                        if kind == "supplement":
                            candidate.selected = False
                            if candidate.curation_status is not ImageCurationStatus.INVALID:
                                candidate.curation_status = ImageCurationStatus.UNSELECTED
                        source = OutputManager._image_source(candidate)
                        if source and ImageDownloader.reusable(candidate) and not candidate.signals.get("invalid_file"):
                            try:
                                converted = OutputManager._save_review_image(candidate, source, attempt_root,
                                    attempt_root / "review_assets" / news_id / f"{candidate.id}.png")
                            except Exception as exc:
                                converted = False
                                candidate.signals["conversion_error"] = f"审阅图生成失败（{type(exc).__name__}）"
                                candidate.local_path = None
                            if not converted:
                                failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=news_id,
                                    candidate_id=candidate.id, code="supplement_prepare_failed",
                                    message="补充图片审阅文件生成失败，原件与来源记录已保留",
                                    source_url=candidate.image_url, retryable=True))
                try:
                    await runtime.loop.run_in_executor(runtime.processing, prepare)
                except Exception as exc:
                    status = "failed"
                    failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=news_id,
                        code="supplement_prepare_failed", message=f"补充图片审阅文件生成失败（{type(exc).__name__}）",
                        source_url=source_url, retryable=True))
                if status == "running":
                    status = "failed"
                elif status == "completed" and (failures or any(c.download_status != "downloaded" for c in candidates)):
                    status = "partial"
                save()
        finally:
            try:
                try:
                    runner.network_metrics = runtime.stats()
                finally:
                    await runtime.close()
            finally:
                runner._network_runtime = None
                runner._download_journal = old_journal
                runner._supplement_mode = old_supplement_mode
    return SupplementResult(news_id, attempt_id, attempt_root, status, candidates, failures)
