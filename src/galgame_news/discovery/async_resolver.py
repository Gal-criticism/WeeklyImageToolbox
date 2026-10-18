"""Batched BFS using the resolver's existing work/gallery link policy."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..domain import DiscoveryMethod, SourceType
from .resolver import _normalize, _visit_key
from .adapters.html import _explicit_age_confirmation_cookie
from .goods_context import is_goods_news, unwrap_goods_url


async def resolve_async(resolver, news, client, call_sync):
    ordered, seen = [], set()
    page_budget, source_budget = [16], [16]

    def add(source):
        key = _normalize(source.url)
        if source.source_type is SourceType.OFFICIAL_SITE and key not in seen and sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered) >= 16:
            return
        if key not in seen:
            seen.add(key)
            source.url = key
            ordered.append(source)

    async def crawl(source):
        if resolver.same_domain_lookup is not None:
            return list(await call_sync(lambda: list(resolver.same_domain_lookup(source, news))))
        if resolver.same_domain_depth <= 0 or source.source_type is not SourceType.OFFICIAL_SITE:
            return []
        found, frontier = [], [(source.url, 0, True)]
        visited = {_visit_key(source.url)}

        async def fetch(url):
            try:
                response = await client.get(url)
                soup = await call_sync(lambda: BeautifulSoup(response.text, "html.parser"))
                cookie = _explicit_age_confirmation_cookie(soup, response.text)
                if cookie:
                    response = await client.get(response.url, cookies={"PermitRate": cookie})
                    soup = await call_sync(lambda: BeautifulSoup(response.text, "html.parser"))
                return response.url, soup
            except Exception:
                return None

        while frontier and page_budget[0] > 0 and source_budget[0] > 0:
            level = [entry for entry in frontier if entry[1] < resolver.same_domain_depth][:page_budget[0]]
            if not level:
                break
            # Fetch a layer concurrently, consume responses in original order.
            responses = await asyncio.gather(*(fetch(url) for url, _, _ in level))
            next_level = []
            for (url, depth, is_entry), response in zip(level, responses):
                if page_budget[0] <= 0 or source_budget[0] <= 0:
                    break
                page_budget[0] -= 1
                if response is None:
                    continue
                effective_url, soup = response
                children = await call_sync(lambda: list(resolver._page_children(source, news, url, effective_url, soup, is_entry)))
                for child in children:
                    key = _visit_key(child.url)
                    if key in visited or page_budget[0] <= 0 or source_budget[0] <= 0:
                        continue
                    visited.add(key)
                    found.append(child)
                    source_budget[0] -= 1
                    next_level.append((child.url, depth + 1, False))
            frontier = next_level
        return found

    for url in news.source_urls:
        if urlsplit(url).scheme in {"http", "https"}:
            add(resolver._document_source(url))
    # History stores can own SQLite connections created by the coordinator.
    historical = list(resolver.history_lookup(news))
    for source in historical:
        add(source.model_copy(update={"discovered_via": DiscoveryMethod.HISTORY}))
    if is_goods_news(news):
        for source in ordered:
            target = unwrap_goods_url(source.url)
            if target != source.url:
                source.parent_url = source.url
                source.url = target
                source.domain = urlsplit(target).hostname or source.domain
                source.source_chain = [source.root_url or source.parent_url, target]
        return ordered
    for source in list(ordered):
        source_budget[0] = min(source_budget[0], 16 - sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered))
        for child in await crawl(source):
            add(child.model_copy(update={
                "discovered_via": DiscoveryMethod.SAME_DOMAIN,
                "root_url": child.root_url or source.root_url or source.url,
                "parent_url": child.parent_url or source.url,
            }))
    for source in await call_sync(lambda: list(resolver.search_provider(news))):
        add(source)
        if source.source_type is SourceType.OFFICIAL_SITE and source.officiality >= 0.75 and _normalize(source.url) in seen:
            source_budget[0] = min(source_budget[0], 16 - sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered))
            for child in await crawl(source):
                add(child)
    return ordered
