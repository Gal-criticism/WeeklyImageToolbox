"""Automatic, review-only image supplementation for completed task news."""
from __future__ import annotations

import asyncio
from pathlib import Path
import uuid

from ..curation import ImageDownloader
from ..curation import ImageCurator
from ..delivery.output import OutputManager
from ..domain import FailureRecord, FailureStage, ImageCandidate, ImageCurationStatus, Issue
from ..pipeline.contracts import CancellationRequested, CancellationToken, ProgressEvent
from ..pipeline.network_runtime import TaskNetworkRuntime
from .ledger import SupplementLedger, normalize_url


async def supplement_missing(runner, *, task_root, issue, request, event_sink=None,
                             cancellation_token=None, news_ids=None, retry_failed=False):
    """Discover and download additional review candidates after normal collection.

    Automatic results are persisted as an unselected candidate pool. They are never
    promoted into the ordinary export set by this function.
    """
    from .providers import discover_sources
    from .rules import classify_candidate
    from ..delivery.helpers import atomic_json_write

    root = Path(task_root).resolve()
    if getattr(request, "offline", False):
        return []
    token = cancellation_token or CancellationToken()
    sink = event_sink or (lambda _event: None)
    ledger = SupplementLedger(root)
    old_config = runner.config
    config = runner._config_for_request(request)
    config = config.model_copy(deep=True)
    config.visual_analysis.enabled = False
    runner._activate_config(config)
    runtime = TaskNetworkRuntime(runner, token)
    runner._network_runtime = runtime
    old_journal, old_mode = runner._download_journal, getattr(runner, "_supplement_mode", False)
    old_localization = getattr(runner, "_localization_service", None)
    runner._localization_service = None
    runner._supplement_mode = True
    results = []
    try:
        from ..tasks.repository import TaskRepository
        repo = TaskRepository(root, create=False) if TaskRepository.is_authoritative(root) else None
        selected_ids = set(news_ids or [n.id for n in issue.news_items])
        raw = root / "raw"
        for news in issue.news_items:
            if news.id not in selected_ids:
                continue
            token.raise_if_cancelled()
            existing = list(repo.iter_candidates(news_id=news.id)) if repo else []
            review_state = {}
            if repo:
                try:
                    review_state = repo.load_review_state() or {}
                except Exception:
                    review_state = {}
            else:
                import json
                state_path = root / "review_state.json"
                if state_path.is_file():
                    try:
                        review_state = json.loads(state_path.read_text(encoding="utf-8"))
                    except Exception:
                        review_state = {}
                index_paths = [root / "raw" / "image_index.json", root / "image_index.json"]
                retry_root = root / "retries" / news.id
                if retry_root.is_dir():
                    index_paths.extend(retry_root.glob("*/image_index.json"))
                for index_path in index_paths:
                    if not index_path.is_file():
                        continue
                    try:
                        payload = json.loads(index_path.read_text(encoding="utf-8"))
                        rows = payload if isinstance(payload, list) else payload.get("candidates", payload.get("images", []))
                        existing.extend(row for row in rows if isinstance(row, dict) and row.get("news_id") == news.id)
                    except Exception:
                        continue
            decisions = review_state.get("decisions", {}) if isinstance(review_state, dict) else {}
            existing_by_id = {}
            for row in existing:
                try:
                    candidate = row if isinstance(row, ImageCandidate) else ImageCandidate.model_validate(row)
                    old = existing_by_id.get(str(candidate.id))
                    if old is None or (ImageDownloader.reusable(candidate) and not ImageDownloader.reusable(old)):
                        existing_by_id[str(candidate.id)] = candidate
                except Exception:
                    continue
            available = [c for c in existing_by_id.values()
                if decisions.get(str(c.id)) == "accepted" or c.selected
                if decisions.get(str(c.id)) != "rejected"
                if c.curation_status is not ImageCurationStatus.INVALID
                if ImageDownloader.reusable(c)]
            # These URLs were already processed by the normal pipeline. Seed only
            # for news with a completed checkpoint association.
            checkpoint_path = root / "checkpoint.json"
            completed = set()
            completed_sources = []
            checkpoint_failures = []
            if repo:
                try:
                    payload = repo.load_checkpoint()
                    if payload:
                        from ..pipeline.contracts import Checkpoint
                        checked = Checkpoint.model_validate(payload)
                        completed = set(checked.completed_news_ids)
                        completed_sources = checked.source_map.get(news.id, [])
                        checkpoint_failures = checked.failures
                except Exception:
                    completed = set()
            if checkpoint_path.is_file():
                try:
                    import json
                    cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    completed = set(cp.get("completed_news_ids", []))
                    completed_sources = cp.get("source_map", {}).get(news.id, [])
                    checkpoint_failures = cp.get("failures", [])
                except Exception:
                    completed = set()
            if news.id in completed:
                for source in completed_sources:
                    url = source.url if hasattr(source, "url") else source.get("url") if isinstance(source, dict) else str(source)
                    if not url:
                        continue
                    if not ledger.get(news.id, url):
                        associated_failures = []
                        for failure in checkpoint_failures:
                            stage_value = failure.get("stage") if isinstance(failure, dict) else getattr(failure, "stage", None)
                            stage_value = getattr(stage_value, "value", stage_value)
                            failure_url = getattr(failure, "source_url", None) if not isinstance(failure, dict) else failure.get("source_url")
                            if stage_value == "collect" and failure_url and normalize_url(failure_url) == normalize_url(url):
                                associated_failures.append(failure)
                        if associated_failures:
                            ledger.set_state(news.id, url, "failed", detail="normal collection has source failure")
                        else:
                            ledger.set_state(news.id, url, "seeded", detail="normal collection completed without source failure")
            target = max(0, int(getattr(request, "supplement_target_images", 5)))
            def available_count(pool=()):
                content = {str(c.original_sha256 or c.sha256).casefold() for c in available}
                for c in [*existing_by_id.values(), *pool]:
                    if decisions.get(str(c.id)) == "rejected":
                        continue
                    if c.curation_status is ImageCurationStatus.INVALID:
                        continue
                    direct = c.signals.get("supplement_relation")
                    if not direct:
                        direct = classify_candidate(news, c).get("supplement_relation")
                    if direct == "direct" and ImageDownloader.reusable(c):
                        content.add(str(c.original_sha256 or c.sha256).casefold())
                return len(content)
            if available_count() >= target:
                continue
            unfinished = ledger.running(news.id)
            if unfinished:
                from ..discovery.resolver import DefaultSourceResolver
                pending_sources = [DefaultSourceResolver._document_source(row["url"]) for row in unfinished]
            else:
                pending_sources = None
            from ..pipeline.supplement import SupplementResult
            attempt_id = f"automatic-{uuid.uuid4().hex[:10]}"
            candidate_pool = []
            attempt_failures = []
            attempt_path = root / "retries" / news.id / attempt_id

            def persist_pool(status="running", source_url=None):
                if repo:
                    repo.save_batch(news.id, attempt_id, str(attempt_path), "supplement", status,
                        [c.model_dump(mode="json") for c in candidate_pool], [],
                        [f.model_dump(mode="json") for f in attempt_failures],
                        source_url=source_url, sources=[])
                else:
                    atomic_json_write(attempt_path / "image_index.json", {
                        "schema_version": 1, "kind": "supplement", "status": status,
                        "issue_id": issue.issue_id, "news_id": news.id,
                        "candidates": [c.model_dump(mode="json") for c in candidate_pool],
                        "failures": [f.model_dump(mode="json") for f in attempt_failures],
                    }, ensure_parent=True)
            async def source_stream():
                if pending_sources is not None:
                    for item in pending_sources:
                        yield item
                    return
                seen = set()
                for stage in getattr(request, "supplement_sources", ("official", "store", "ymgal", "vndb")):
                    try:
                        found = await runtime.call_sync(lambda stage=stage: discover_sources(
                            news, stage=stage, config=config, transport=runner.source_transport,
                            skip_url=lambda url: ledger.should_skip(news.id, url, retry_failed=retry_failed)))
                    except Exception as exc:
                        attempt_failures.append(FailureRecord(stage=FailureStage.RESOLVE, news_id=news.id,
                            code="supplement_discovery_failed", message=f"source discovery failed ({type(exc).__name__})",
                            retryable=True))
                        continue
                    for item in found or []:
                        url = item.url if hasattr(item, "url") else str(item.get("url", ""))
                        key = normalize_url(url)
                        if not url or key in seen or ledger.should_skip(news.id, url, retry_failed=retry_failed):
                            continue
                        seen.add(key)
                        yield item
                        # The caller processes each yielded source before asking
                        # for the next one; stop before discovering another stage.
                        if available_count(candidate_pool) >= target:
                            return

            async for source in source_stream():
                if available_count(candidate_pool) >= target:
                    break
                token.raise_if_cancelled()
                url = source.url if hasattr(source, "url") else str(source.get("url", ""))
                ledger.set_state(news.id, url, "running")
                try:
                    if isinstance(source, dict):
                        from ..domain import SourceRef
                        source = SourceRef.model_validate(source)
                    cached = [c.model_copy(deep=True) for c in existing_by_id.values()
                              if c.signals.get("supplement_module") and normalize_url(c.source_url) == normalize_url(url)]
                    if cached:
                        fresh = [c for c in cached if not ImageDownloader.reusable(c)
                                 and c.download_status != "permanent_failed"]
                        if not fresh:
                            ledger.set_state(news.id, url, "completed", detail="resumed from downloaded candidates")
                            continue
                    else:
                        page_cache = ledger.get_page(url)
                        if page_cache is not None:
                            raw_candidates = page_cache.get("candidates", [])
                            collection_candidates = []
                            for raw_candidate in raw_candidates:
                                record = dict(raw_candidate)
                                record["news_id"] = news.id
                                record["id"] = None  # stable id is scoped to this news
                                collection_candidates.append(ImageCandidate.model_validate(record))
                        else:
                            collections = await runtime.collect_many(news, [source], request)
                            collection = collections[0]
                            if isinstance(collection, Exception):
                                raise collection
                            if collection.failures:
                                attempt_failures.extend(collection.failures)
                            collection_candidates = [c for c in collection.candidates if c.news_id == news.id]
                            # Cache page parsing across news items in the same task.
                            if collection_candidates or not collection.failures:
                                ledger.set_page(url, source, collection_candidates)
                            elif not collection_candidates:
                                ledger.set_state(news.id, url, "failed", detail="collection failed without candidates")
                                persist_pool("failed", url)
                                continue
                        fresh = []
                        for c in collection_candidates:
                            old = existing_by_id.get(str(c.id))
                            if old is not None:
                                if decisions.get(str(c.id)) in {"accepted", "rejected"}:
                                    continue
                                if old.signals.get("supplement_module") and not ImageDownloader.reusable(old):
                                    fresh.append(old)
                                continue
                            verdict = classify_candidate(news, c)
                            c.signals.update(verdict if isinstance(verdict, dict) else {})
                            c.signals["supplement_module"] = True
                            c.signals["supplement_adopted"] = False
                            c.selected = False
                            fresh.append(c)
                    if fresh:
                        dest = attempt_path / "images"
                        candidate_pool.extend(fresh)
                        persist_pool("running", url)
                        downloadable = [c for c in fresh
                            if c.signals.get("supplement_quality") != "invalid"
                            and c.signals.get("supplement_relation") not in {"unrelated", "version_uncertain"}]
                        if not downloadable:
                            ledger.set_state(news.id, url, "no_assets", detail="all candidates failed metadata triage")
                            persist_pool("no_assets", url)
                            continue
                        async def on_result(_candidate, failure):
                            if failure:
                                attempt_failures.append(failure)
                            persist_pool("running", url)
                            sink(ProgressEvent("supplement_progress", request.task_id or "", issue.issue_id,
                                news_id=news.id, message="补充图片处理中…",
                                payload={"success_count": sum(c.download_status == "downloaded" for c in candidate_pool),
                                         "candidate_count": len(candidate_pool)}))
                        await runtime.download(downloadable, dest, on_result=on_result)
                        # Use the normal curation deduplicator and review-image
                        # conversion while keeping every supplement unselected.
                        copies = [c.model_copy(deep=True) for c in [*existing_by_id.values(), *candidate_pool]]
                        curated = ImageCurator(config).curate(Issue(issue_id=issue.issue_id,
                            input_path=issue.input_path, news_items=[news]), copies)
                        prepared = {c.id: c for c in [*curated.candidates, *curated.filtered_candidates]}
                        for candidate in candidate_pool:
                            reviewed = prepared.get(candidate.id)
                            if reviewed is not None:
                                candidate.image_type = reviewed.image_type
                                candidate.image_type_confidence = reviewed.image_type_confidence
                                candidate.score = reviewed.score
                                candidate.review_reasons = list(reviewed.review_reasons)
                                candidate.selection_reasons = list(reviewed.selection_reasons)
                                candidate.curation_status = reviewed.curation_status
                            candidate.selected = False
                            candidate.signals["supplement_module"] = True
                            candidate.signals["supplement_adopted"] = False
                            if candidate.download_status == "downloaded":
                                source_path = OutputManager._image_source(candidate)
                                if source_path:
                                    OutputManager._save_review_image(candidate, source_path, attempt_path,
                                        attempt_path / "review_assets" / news.id / f"{candidate.id}.png")
                        persist_pool("running", url)
                        ledger.set_page(url, source, fresh)
                        existing_by_id.update({c.id: c for c in fresh})
                        count = available_count(candidate_pool)
                        state = "completed" if count >= target else "partial"
                        ledger.set_state(news.id, url, state, detail=f"downloaded={count}; target={target}")
                        persist_pool(state, url)
                    else:
                        ledger.set_state(news.id, url, "no_assets", detail="no new image candidates")
                except (CancellationRequested, asyncio.CancelledError):
                    ledger.set_state(news.id, url, "cancelled")
                    raise
                except Exception as exc:
                    ledger.set_state(news.id, url, "failed", detail=type(exc).__name__)
                    attempt_failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                        code="supplement_source_failed", message=f"supplement source failed ({type(exc).__name__})",
                        source_url=url, retryable=True))
                    sink(ProgressEvent("supplement_failed", request.task_id or "", issue.issue_id,
                        news_id=news.id, message="补充来源处理失败", payload={"error": type(exc).__name__}))
            status = ("completed" if available_count(candidate_pool) >= target else
                      "failed" if attempt_failures and not candidate_pool else
                      "partial" if candidate_pool else "no_assets")
            results.append(SupplementResult(news_id=news.id, attempt_id=attempt_id,
                path=attempt_path, status=status, candidates=candidate_pool, failures=attempt_failures))
        return results
    finally:
        runner.network_metrics = runtime.stats()
        await runtime.close()
        runner._network_runtime = None
        runner._download_journal = old_journal
        runner._supplement_mode = old_mode
        runner._localization_service = old_localization
        runner._activate_config(old_config)
