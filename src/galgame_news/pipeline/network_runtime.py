"""One task loop for HTTP, with bounded bridges for legacy parsers."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
from dataclasses import asdict
import time
from urllib.parse import urlsplit
from ..discovery.x_api import _STATUS_ID

from ..curation.download import ImageDownloader
from ..discovery.async_http import AsyncHttpClient
from ..discovery.async_resolver import resolve_async
from ..discovery.async_x_api import AsyncSocialDataTweetTransport
from ..discovery.adapters.html import OfficialHtmlAdapter
from ..discovery.browser import BrowserRenderer
from ..domain import CollectionContext, FailureRecord, FailureStage, ReviewReason, SourceType
from .contracts import CancellationRequested
from ..concurrency import DaemonTaskPool as _DaemonTaskPool


class SyncHttpBridge:
    """Only called from compatibility workers, never from the loop thread."""

    def __init__(self, runtime, client):
        self.runtime, self.client = runtime, client

    def get(self, url, *, cookies=None):
        response = self.runtime.submit(self.client.get(url, cookies=cookies))
        # Only source responses for the active news are retained here. Besides
        # avoiding a second failed request, this preserves the age-confirmed
        # response for navigation/rendering without another cookie-less fetch.
        if self.client is self.runtime.page_client:
            self.runtime._source_responses[url] = response
        return response

    def post_json(self, url, payload):
        return self.runtime.submit(self.client.post_json(url, payload))

    def validate_url(self, url):
        self.runtime.submit(self.client._validate_url(url))

    def __call__(self, url, **kwargs):
        header = kwargs.get("headers", {}).get("cookie", "")
        cookies = dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)
        return self.get(url, cookies=cookies)


class TaskNetworkRuntime:
    def __init__(self, runner, token):
        self.runner, self.token = runner, token
        self.loop = asyncio.get_running_loop()
        config, network = runner.config, runner.config.network
        self.page_client = AsyncHttpClient(
            transport=runner.source_transport, timeout=max(20.0, network.timeout_seconds),
            max_retries=network.max_retries, max_response_bytes=config.filters.max_response_bytes,
            user_agent=network.user_agent, concurrency=network.page_concurrency,
            per_host_concurrency=network.page_per_host, cancellation=token,
            trust_env=network.trust_env,
        )
        self.image_client = AsyncHttpClient(
            transport=runner.image_transport, timeout=network.timeout_seconds,
            max_retries=network.max_retries, max_response_bytes=config.filters.max_image_bytes,
            user_agent=network.user_agent, concurrency=network.image_concurrency,
            per_host_concurrency=network.image_per_host, cancellation=token,
            trust_env=network.trust_env,
        )
        self.socialdata = AsyncSocialDataTweetTransport(
            concurrency=network.socialdata_concurrency, cancellation=token,
        )
        self.page_bridge = SyncHttpBridge(self, self.page_client)
        self.browser = BrowserRenderer(self.page_client, config.browser, token)
        self.compat = _DaemonTaskPool(
            max_workers=network.page_concurrency,
            thread_name_prefix="galgame-source",
        )
        self.processing = _DaemonTaskPool(max_workers=network.image_processing_threads, thread_name_prefix="galgame-image")
        # Timed-out third-party parsers may retain all compatibility workers.
        # Saving their partial results must still have an available worker.
        self.persistence = _DaemonTaskPool(max_workers=1, thread_name_prefix="galgame-state")
        self._compat_slots = asyncio.Semaphore(network.page_concurrency)
        self.timings = {}
        self._active = set()
        self._source_responses = {}
        self.resolve_failures = []

    async def call_sync(self, call, timeout=None):
        timeout = self.runner.config.network.news_timeout_seconds if timeout is None else timeout
        async def invoke():
            async with self._compat_slots:
                while self.token.is_paused and not self.token.is_cancelled:
                    await asyncio.sleep(0.05)
                if self.token.is_cancelled:
                    raise asyncio.CancelledError()
                # A timed-out callable cannot be killed by Python. Give each
                # admitted call its own daemon worker so it cannot occupy a
                # shared worker needed by the next source or publication.
                worker = _DaemonTaskPool(max_workers=1, thread_name_prefix="galgame-call")
                future = self.loop.run_in_executor(worker, call)
                try:
                    return await future
                except asyncio.CancelledError:
                    future.cancel()
                    raise
                finally:
                    worker.shutdown(wait=False, cancel_futures=True)
        try:
            return await asyncio.wait_for(invoke(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"synchronous compatibility call exceeded {timeout:g} seconds") from None

    def submit(self, awaitable, timeout=None):
        timeout = self.runner.config.network.news_timeout_seconds if timeout is None else timeout
        async def tracked():
            task = asyncio.current_task()
            self._active.add(task)
            try:
                return await awaitable
            finally:
                self._active.discard(task)
        try:
            future = asyncio.run_coroutine_threadsafe(tracked(), self.loop)
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                future.cancel()
                raise
        except CancelledError:
            raise CancellationRequested("pipeline cancellation requested") from None

    async def resolve(self, resolver, news):
        report = getattr(self, "resolve_progress", None)
        self.resolve_failures = []
        return await self._timed("resolve", resolve_async(
            resolver, news, self.page_client, self.call_sync, progress=report,
            on_failure=self.resolve_failures.append,
        ))

    async def collect_many(self, news, sources, request, *, _goods_expand=True):
        if _goods_expand:
            self._source_responses.clear()
        from ..discovery.goods_context import is_goods_news
        if _goods_expand and is_goods_news(news) and self.runner.adapter_factory is None and not request.offline:
            from .goods_collection import collect_goods
            results = await collect_goods(self, news, sources, request)
            self._record_supplement_pages(news, sources, results, request)
            return results
        if not getattr(self.runner, "_localization_service", None):
            from ..localization.service import LocalizationImageService
            self.runner._localization_service = LocalizationImageService(self.page_bridge)
        async def collect(source):
            try:
                return await self.call_sync(lambda: self.runner._collect(news, source, request=request))
            except CancellationRequested:
                raise asyncio.CancelledError()
            except Exception as exc:
                return exc
        async def collect_guarded(source):
            try:
                return await asyncio.wait_for(
                    collect(source),
                    timeout=self.runner.config.network.news_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return TimeoutError(
                    f"来源采集超过 {self.runner.config.network.news_timeout_seconds:g} 秒"
                )

        results = await self._timed(
            "collect",
            asyncio.gather(*(collect_guarded(source) for source in sources)),
        )
        from ..delivery.helpers import section_prefix, section_label
        if section_prefix(section_label(news)) == "h":
            for result in results:
                if isinstance(result, Exception):
                    continue
                self.runner._bind_localization_context(news, result.candidates)
        if self.runner.adapter_factory is not None or request.offline:
            self._record_supplement_pages(news, sources, results, request)
            return results
        # Merge on the coordinator in source order. Rendering never starts
        # inside a synchronous parser/compatibility thread.
        parser = OfficialHtmlAdapter(client=self.page_bridge)
        context = CollectionContext(timeout_seconds=self.runner.config.network.timeout_seconds,
                                    max_candidates=self.runner.config.search.max_candidates_per_source)
        for source, result in zip(sources, results):
            from ..discovery.goods_service import GoodsSourceService, goods_page_text
            goods = GoodsSourceService.supports(news, source)
            active_parser = GoodsSourceService(self.page_bridge) if goods else parser
            supplement_html = (
                getattr(self.runner, "_supplement_mode", False)
                and (urlsplit(source.url).hostname or "").casefold()
                not in {"ymgal.games", "www.ymgal.games", "vndb.org", "www.vndb.org", "store.steampowered.com"}
            )
            if isinstance(result, Exception) or (source.source_type is not SourceType.OFFICIAL_SITE and not goods and not supplement_html):
                continue
            try:
                response = self._source_responses.get(source.url)
                if response is None:
                    # Collection failed before receiving a response; do not
                    # repeat requests just to decide whether to render it.
                    if result.failures:
                        continue
                    response = await self.page_client.get(source.url)
                if not 200 <= response.status_code < 300:
                    continue
                response_text = goods_page_text(response) if goods else response.text
                needs_render = (lambda: active_parser.needs_render(response_text, result, str(response.url))) if goods else (lambda: parser.needs_render(response_text, result))
                if not await self.call_sync(needs_render):
                    continue
                rendered = await self._timed("browser_render", self.browser.render(response.url, news.id, response_text))
                if goods:
                    if not hasattr(self, "_goods_snapshots"):
                        self._goods_snapshots = {}
                    self._goods_snapshots[source.url] = rendered.snapshots
                self.token.raise_if_cancelled()
                by_id = {candidate.id: candidate for candidate in result.candidates}
                resolved = False
                for snapshot in rendered.snapshots:
                    parsed = await self.call_sync(lambda: active_parser.parse(news, source, context, snapshot.html,
                                                                        page_url=snapshot.url, method="browser"))
                    result.failures.extend(parsed.failures)
                    snapshot_needs_render = (lambda: active_parser.needs_render(snapshot.html, parsed, snapshot.url)) if goods else (lambda: parser.needs_render(snapshot.html, parsed))
                    resolved = resolved or not await self.call_sync(snapshot_needs_render)
                    for candidate in parsed.candidates:
                        prior = by_id.get(candidate.id)
                        if prior is None:
                            result.candidates.append(candidate)
                            by_id[candidate.id] = candidate
                        else:
                            existing = {e.model_dump_json() for e in prior.evidence}
                            prior.evidence.extend(e for e in candidate.evidence if e.model_dump_json() not in existing)
                            # Image-local evidence obtained after rendering
                            # replaces fallback-slot context, never asset data.
                            if candidate.evidence:
                                prior.signals.update(candidate.signals)
                                prior.image_alt = candidate.image_alt or prior.image_alt
                                prior.nearby_text = candidate.nearby_text or prior.nearby_text
                result.candidates = result.candidates[:context.max_candidates]
                for diagnostic in rendered.diagnostics:
                    result.failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                        code=diagnostic.code, message=diagnostic.message, source_url=source.url,
                        retryable=diagnostic.code in {"browser_timeout", "browser_render_failed", "browser_resource_failed"}))
                if rendered.snapshots and not rendered.diagnostics and resolved:
                    result.manual_review_reasons = [r for r in result.manual_review_reasons if r is not ReviewReason.DYNAMIC_PAGE]
                    source.review_reasons = [r for r in source.review_reasons if r is not ReviewReason.DYNAMIC_PAGE]
                    for candidate in result.candidates:
                        candidate.review_reasons = [r for r in candidate.review_reasons if r is not ReviewReason.DYNAMIC_PAGE]
                elif rendered.snapshots and not resolved:
                    result.failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                        code="dynamic_gallery_unresolved", message="渲染后仍缺少可确认的图库项，保留静态候选待复核。",
                        source_url=source.url, retryable=False))
            except (CancellationRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                result.failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                    code="browser_collect_failed", message=f"动态采集失败（{type(exc).__name__}）。", source_url=source.url, retryable=True))
        self._record_supplement_pages(news, sources, results, request)
        return results

    def _record_supplement_pages(self, news, sources, results, request):
        """Record exact normal-collection evidence only for opted-in tasks."""
        if (not getattr(request, "use_image_supplement", False)
                or not getattr(self.runner, "_download_journal", None)
                or getattr(self.runner, "_supplement_mode", False)):
            return
        try:
            from ..supplement.ledger import SupplementLedger
            ledger = SupplementLedger(self.runner._download_journal.root)
            for source, result in zip(sources, results):
                if isinstance(result, Exception):
                    ledger.set_state(news.id, source.url, "failed", detail="normal collection raised")
                    continue
                failures = list(getattr(result, "failures", []) or [])
                candidates = [c for c in getattr(result, "candidates", []) if c.news_id == news.id]
                if failures:
                    ledger.set_state(news.id, source.url, "partial" if candidates else "failed",
                        detail="normal collection returned failures")
                    if candidates:
                        ledger.set_page(source.url, source, candidates)
                    continue
                status = "completed" if candidates else "no_assets"
                ledger.set_state(news.id, source.url, status, detail="normal collection completed")
                ledger.set_page(source.url, source, candidates)
        except Exception:
            # Supplement bookkeeping must never fail the primary news collection.
            return

    async def download(self, candidates, directory, *, on_result=None):
        downloader = ImageDownloader(self.runner.config)
        result = await self._timed("download_and_processing", asyncio.wait_for(downloader.download_async(
            candidates, directory, client=self.image_client, executor=self.processing,
            concurrency=self.runner.config.network.image_concurrency,
            on_result=on_result,
        ), timeout=self.runner.config.network.news_timeout_seconds))
        self.timings["image_processing_worker_seconds"] = self.timings.get("image_processing_worker_seconds", 0.0) + downloader.processing_seconds
        if (getattr(self.runner, "_image_supplement_enabled", False)
                and getattr(self.runner, "_download_journal", None)
                and not getattr(self.runner, "_supplement_mode", False)):
            try:
                from ..supplement.ledger import SupplementLedger, normalize_url
                from ..discovery.resolver import DefaultSourceResolver
                ledger = SupplementLedger(self.runner._download_journal.root)
                groups = {}
                for candidate in candidates:
                    groups.setdefault(normalize_url(candidate.source_url), []).append(candidate)
                for key, values in groups.items():
                    ledger.set_page(key, DefaultSourceResolver._document_source(key), values)
            except Exception:
                pass
        return result

    def socialdata_lookup(self, status_url, *, token, timeout):
        async def lookup():
            parts = urlsplit(status_url)
            match = _STATUS_ID.search(parts.path)
            if (parts.hostname or "").casefold() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or not match:
                raise ValueError("SocialData lookup requires an X status URL")
            post_id = match.group(1)
            cache = self.runner._socialdata_response_cache
            if post_id in cache:
                if cache[post_id] is None:
                    raise RuntimeError("SocialData previous lookup failed or was interrupted; media recovery requires a saved response")
                return cache[post_id]
            journal = self.runner._download_journal
            if journal:
                journal.save_post(post_id, "pending")
            try:
                response = await self.socialdata.lookup(status_url, token=token, timeout=timeout)
                # A compromised/error response must never persist credentials.
                import json
                if token and token in json.dumps(response):
                    raise RuntimeError("SocialData returned unexpected credential content")
            except BaseException:
                if not isinstance(cache.get(post_id), dict):
                    cache[post_id] = None
                if journal and cache[post_id] is None:
                    journal.save_post(post_id, "failed")
                raise
            cache[post_id] = response
            if journal:
                journal.save_post(post_id, "success", response)
            return response
        return self.submit(lookup())

    async def _timed(self, name, awaitable):
        start = time.monotonic()
        try:
            return await awaitable
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + time.monotonic() - start

    def stats(self):
        return {
            "pages": asdict(self.page_client.stats),
            "images": asdict(self.image_client.stats),
            "socialdata_requests": self.socialdata.request_count,
            "stage_seconds": dict(self.timings),
        }

    async def cancel_pending(self):
        for task in list(self._active):
            task.cancel()
        await asyncio.gather(*list(self._active), return_exceptions=True)

    async def close(self):
        await self.cancel_pending()
        await self.browser.close()
        await self.page_client.close()
        await self.image_client.close()
        await self.socialdata.close()
        # Compatibility calls already drain before the coordinator returns.
        # Third-party DNS/search code may ignore cancellation. Daemon workers
        # may finish later without keeping the task or desktop process alive.
        self.compat.shutdown(wait=False, cancel_futures=True)
        self.persistence.shutdown(wait=False, cancel_futures=True)
        self.processing.shutdown(wait=False, cancel_futures=True)
