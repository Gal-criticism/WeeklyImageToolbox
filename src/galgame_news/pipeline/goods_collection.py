"""Bounded goods BFS after source evidence is available, on one coordinator."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..discovery.goods_context import cached_product_urls, unwrap_goods_url
from ..discovery.goods_pages import goods_site
from ..discovery.goods_service import GoodsSourceService, page_problem, goods_page_text
from ..discovery.product_navigation import product_links
from ..discovery.resolver import DefaultSourceResolver, _normalize, _is_media_url
from ..domain import CollectionResult, DiscoveryMethod, FailureRecord, FailureStage, SourceType
from .contracts import CancellationRequested


@dataclass
class GoodsCollectionState:
    seen: set[str] = field(default_factory=set)
    pages: int = 0
    searched: bool = False


def has_qualified_goods(news, candidates, config):
    """Only verified native merchant images can satisfy the search stop rule.

    HTML dimensions and the presence of an X attachment are insufficient. The
    downloader has already decoded and hashed successful assets at this stage.
    Reuse the existing entity/type policies rather than inventing weaker ones.
    """
    from ..curation.entity_matching import EntityMatcher
    from ..curation.image_typing import ImageTypeClassifier
    from ..curation.source_policy import SourceTrustPolicy
    from ..domain import ImageType
    classifier = ImageTypeClassifier(config.image_types)
    for candidate in candidates:
        signals = candidate.signals
        width, height = candidate.original_width or 0, candidate.original_height or 0
        if (candidate.news_id != news.id or candidate.download_status != "downloaded"
                or not candidate.original_sha256 or not candidate.original_path
                or signals.get("goods_context") is not True
                or signals.get("goods_association") != "matched"
                or signals.get("goods_original") is not True
                or signals.get("goods_reference") is True or signals.get("goods_role") == "reference"
                or candidate.source_type is SourceType.OFFICIAL_X
                or signals.get("x_api_photo") is True or signals.get("socialdata_photo") is True
                or signals.get("invalid_reason") or signals.get("conversion_error")
                or candidate.animated_source or signals.get("animated_source") is True
                or max(width, height) < 800 or min(width, height) < 600
                or width < config.filters.min_width or height < config.filters.min_height
                or width * height < config.filters.min_pixels):
            continue
        if not Path(candidate.original_path).is_file():
            continue
        typed = classifier.classify(news, candidate)
        if (typed.image_type is ImageType.GOODS and typed.confidence >= 0.8
                and EntityMatcher().match(news, candidate).matched is True
                and SourceTrustPolicy().classify(candidate).tier == "official_store"):
            return True
    return False


async def search_goods_after_download(runtime, news, sources, request, candidates):
    """One bounded public search after download; never re-collect X sources."""
    from ..discovery.goods_context import is_goods_news
    state = getattr(runtime, "_goods_states", {}).get(news.id)
    if (state is None and not request.offline and is_goods_news(news)
            and runtime.runner.adapter_factory is None):
        # Media-only recovery must also finish a search interrupted after the
        # initial download. Reuse recorded sources and assets, never X queries.
        state = GoodsCollectionState(
            seen={_normalize(unwrap_goods_url(s.url)) for s in sources},
            pages=sum(s.source_type not in {SourceType.OFFICIAL_X, SourceType.VIDEO, SourceType.DIRECT_IMAGE} for s in sources),
            searched=any(s.navigation_kind == "goods_search" for s in sources),
        )
        runtime._goods_states = {news.id: state}
    resolver = runtime.runner.resolver
    if (state is None or state.searched or request.offline
            or getattr(runtime.runner, "_supplement_mode", False)
            or not isinstance(resolver, DefaultSourceResolver)):
        return [], []
    state.searched = True
    if await runtime.call_sync(lambda: has_qualified_goods(news, candidates, runtime.runner.config)):
        return [], []
    if state.pages >= 16:
        return [], [CollectionResult(failures=[FailureRecord(stage=FailureStage.RESOLVE,
            news_id=news.id, code="goods_search_budget_exhausted",
            message="尚未取得合格高清商品图，但商品采集已达 16 页上限，未继续补搜。", retryable=False)])]
    try:
        fallback = await runtime.call_sync(lambda: list(resolver.search_provider(news)))
        extra = []
        for source in fallback:
            if goods_site(source.url) and _normalize(unwrap_goods_url(source.url)) not in state.seen:
                source.navigation_kind = "goods_search"
                source.root_url = news.source_urls[0] if news.source_urls else source.url
                source.source_chain = list(dict.fromkeys([source.root_url, source.url]))
                # Search discovery is evidence of discovery, never officiality.
                extra.append(source)
        if not extra:
            return [], [CollectionResult(failures=[FailureRecord(stage=FailureStage.RESOLVE,
                news_id=news.id, code="goods_search_no_results",
                message="未取得合格高清商品图；商品补搜未返回新的受支持商城链接。", retryable=True)])]
        results = await collect_goods(runtime, news, extra, request, _state=state)
        sources.extend(extra)
        return extra, results
    except (CancellationRequested, asyncio.CancelledError):
        raise
    except Exception as exc:
        return [], [CollectionResult(failures=[FailureRecord(stage=FailureStage.RESOLVE,
            news_id=news.id, code="goods_search_failed",
            message=f"商品补充搜索失败（{type(exc).__name__}），已取得的图片仍保留。", retryable=True)])]


async def collect_goods(runtime, news, sources, request, *, _state=None):
    """Expand in input order, sharing one 16-page/3-level budget across stages.

    ``sources`` is the coordinator-owned source-map list, extended alongside
    results before publication/checkpointing. No worker mutates task state.
    """
    runner, token = runtime.runner, runtime.token
    pending = list(sources)
    sources.clear()
    results = []
    state = _state or GoodsCollectionState()
    seen = state.seen
    max_depth = min(3, runner.config.search.same_domain_depth)
    if _state is None:
        runtime._goods_states = {news.id: state}
        runtime._source_responses.clear()
        runtime._goods_snapshots = {}

    def failure(source, code, message, retryable=False):
        return FailureRecord(stage=FailureStage.RESOLVE, news_id=news.id,
            code=code, message=message, source_url=source.url, retryable=retryable)

    def seed(target, parent, root, label="", depth=0, method=DiscoveryMethod.SAME_DOMAIN):
        source = DefaultSourceResolver._document_source(unwrap_goods_url(target))
        source.parent_url, source.root_url = parent, root
        source.navigation_kind, source.navigation_text = "goods", label
        source.navigation_depth, source.discovered_via = depth, method
        source.source_chain = list(dict.fromkeys(u for u in [root, parent, source.url] if u))
        return source

    while pending:
        await token.wait_if_paused_async()
        batch = []
        budget_skipped = []
        for source in pending:
            key = _normalize(unwrap_goods_url(source.url))
            if key in seen:
                continue
            seen.add(key)
            if key != source.url:
                source.parent_url = source.url
                source.url = key
                source.domain = urlsplit(key).hostname or source.domain
            is_page = source.source_type not in {SourceType.OFFICIAL_X, SourceType.VIDEO, SourceType.DIRECT_IMAGE}
            if is_page:
                if state.pages >= 16:
                    budget_skipped.append(source)
                    continue
                state.pages += 1
            batch.append(source)
        pending = []
        if budget_skipped:
            diagnostic = failure(budget_skipped[0], "goods_page_budget",
                "商品导航已达每条新闻 16 页上限；未展开详情：" + " | ".join(s.url for s in budget_skipped))
            if results:
                results[0].failures.append(diagnostic)
            else:
                # Retain the source even in an empty/budget-exhausted run.
                sources.append(budget_skipped[0])
                results.append(CollectionResult(failures=[diagnostic]))
        if batch:
            collected = await runtime.collect_many(news, batch, request, _goods_expand=False)
            for source, result in zip(batch, collected):
                token.raise_if_cancelled()
                if isinstance(result, Exception):
                    result = CollectionResult(failures=[failure(source, "goods_source_failed",
                        f"来源采集失败（{type(result).__name__}），其他来源继续处理。", True)])
                sources.append(source)
                results.append(result)
                if source.source_type in {SourceType.OFFICIAL_X, SourceType.VIDEO, SourceType.DIRECT_IMAGE}:
                    continue
                try:
                    response = runtime._source_responses.get(source.url)
                    if response is None:
                        if result.failures:
                            continue
                        response = await runtime.page_client.get(source.url)
                    response_text = goods_page_text(response) if goods_site(str(response.url)) else response.text
                    if page_problem(response_text, str(response.url), response.status_code):
                        continue
                    snapshots = [(response_text, str(response.url))]
                    snapshots.extend((s.html, s.url) for s in getattr(runtime, "_goods_snapshots", {}).get(source.url, ()))
                    links = []
                    for html, page_url in snapshots:
                        if goods_site(page_url):
                            discovered = await runtime.call_sync(lambda: GoodsSourceService.links(news, source, html, page_url))
                        else:
                            discovered = await runtime.call_sync(lambda: product_links(news, source, page_url, BeautifulSoup(html, "html.parser")))
                        links.extend(discovered)
                    links = list(dict.fromkeys(links))
                    if links and source.navigation_depth >= max_depth:
                        unresolved = [u for u, _, _ in links if _normalize(u) not in seen]
                        if unresolved:
                            result.failures.append(failure(source, "goods_depth_budget",
                                "商品导航已达 3 层或配置深度上限；未展开详情：" + " | ".join(unresolved)))
                        continue
                    for url, label, kind in links:
                        if _is_media_url(url) or _normalize(url) in seen:
                            continue
                        child = seed(url, str(response.url), source.root_url or source.url,
                            label, source.navigation_depth + 1)
                        child.navigation_kind = "goods_search" if source.navigation_kind == "goods_search" else kind
                        child.source_chain = list(dict.fromkeys([
                            *(source.source_chain or [source.root_url or source.url]),
                            str(response.url), child.url]))
                        pending.append(child)
                except (CancellationRequested, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    result.failures.append(failure(source, "goods_navigation_failed",
                        f"商品详情导航失败（{type(exc).__name__}），已取得的图片仍保留。", True))
        # Reuse only saved entities from the current news's posts. This never
        # invokes a post endpoint or obtains another post's recommendations.
        for url, post in cached_product_urls(runner._socialdata_response_cache, news):
            if goods_site(url) and _normalize(url) not in seen:
                pending.append(seed(url, post, post, "本条新闻 X 缓存商品链接"))
    return results
