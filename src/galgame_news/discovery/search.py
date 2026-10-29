"""Optional search providers; network clients are injectable for tests."""

from __future__ import annotations

import os
import re
import multiprocessing
import time
from collections.abc import Iterable
from typing import Any, Callable
from urllib.parse import urlsplit

from ..domain import DiscoveryMethod, NewsItem, SearchResult, SourceRef, SourceType
from .goods_context import is_goods_news
from .relevance import _organization_names, news_relevance


def _search_queries(news_item: NewsItem) -> tuple[str, ...]:
    if is_goods_news(news_item):
        # The product/event title is essential. A work-only query tends to
        # return game screenshots instead of the announced merchandise.
        terms = list(dict.fromkeys([*news_item.game_names, *_organization_names(news_item), news_item.title]))
        base = " ".join(terms)[:240]
        return (f"{base} 商品 site:eeo.today OR site:getchu.com OR site:amazon.co.jp OR site:gamers.co.jp",
                f"{base} 商品 site:dmm.co.jp OR site:fantia.jp OR site:dlsite.com")
    work_base = " ".join(news_item.game_names or [news_item.title]).strip()
    qualified_base = " ".join(dict.fromkeys([*news_item.game_names, *_organization_names(news_item)])).strip()
    text = " ".join((news_item.title, news_item.body, *news_item.keywords))
    if re.search(r"cg|ギャラリー|gallery|事件.?cg|画面公開|画像更新", text, re.I):
        return tuple(dict.fromkeys((work_base, f"{qualified_base} CG gallery", f"{qualified_base} CG 画像")))
    if qualified_base != work_base:
        return (qualified_base, work_base)
    return (work_base,)


def _merge_sources(*groups: Iterable[SourceRef], limit: int) -> list[SourceRef]:
    merged: list[SourceRef] = []
    seen: set[str] = set()
    for group in groups:
        for source in group:
            parts = urlsplit(source.url)
            key = f"{parts.scheme.casefold()}://{parts.netloc.casefold()}{parts.path}?{parts.query}"
            if key not in seen:
                seen.add(key)
                merged.append(source)
                if len(merged) >= limit:
                    return merged
    return merged


def _to_sources(results: Iterable[Any], method: DiscoveryMethod, news_item: NewsItem | None = None) -> list[SourceRef]:
    sources = []
    for result in results:
        try:
            if isinstance(result, SearchResult):
                value = result
            elif isinstance(result, dict):
                value = SearchResult(
                    url=str(result.get("url") or result.get("href") or ""),
                    title=str(result.get("title") or ""),
                    snippet=str(result.get("snippet") or result.get("body") or ""),
                )
            else:
                value = SearchResult(url=str(getattr(result, "url", result)), title=str(getattr(result, "title", "")), snippet=str(getattr(result, "snippet", "")))
        except (TypeError, ValueError):
            continue
        if urlsplit(value.url).scheme not in {"http", "https"}:
            continue
        if news_item is not None and not news_relevance(news_item, f"{value.title} {value.snippet}", value.url):
            continue
        host = urlsplit(value.url).hostname or "unknown"
        sources.append(SourceRef(url=value.url, domain=host, source_type=SourceType.UNVERIFIED, discovered_via=method, officiality=0.0, search_title=value.title, search_snippet=value.snippet))
    return sources


class DDGSSearchProvider:
    def __init__(self, *, search_fn: Callable[..., Iterable[Any]] | None = None, max_results: int = 5, timeout: float = 5.0):
        self.search_fn = search_fn
        self.max_results = max_results
        self.timeout = timeout

    def search(self, news_item: NewsItem) -> list[SourceRef]:
        if self.search_fn is None:
            raw = _run_ddgs_subprocess(_search_queries(news_item), self.max_results, self.timeout)
            results = _to_sources(raw, DiscoveryMethod.DDGS, news_item)
            return _merge_sources(results, limit=self.max_results * len(_search_queries(news_item)))
        results: list[SourceRef] = []
        for query in _search_queries(news_item):
            try:
                batch = self.search_fn(query, max_results=self.max_results)
                results.extend(_to_sources(list(batch)[: self.max_results], DiscoveryMethod.DDGS, news_item))
            except Exception:
                continue
        return _merge_sources(results, limit=self.max_results * len(_search_queries(news_item)))


def _run_ddgs_subprocess(queries: tuple[str, ...], max_results: int, timeout: float) -> list[Any]:
    """Run DDGS behind a hard, total deadline, including spawn and shutdown."""
    from .search_worker import ddgs_search_worker

    succeeded, payload = _run_spawned_worker(
        ddgs_search_worker, (queries, max_results, timeout), timeout,
        process_name="galgame-ddgs-search",
    )
    return payload if succeeded else []


def _run_spawned_worker(target, args, timeout: float, *, process_name: str):
    """Run an importable spawn target and terminate it within its wall deadline."""

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=target,
        args=(child, *args), name=process_name, daemon=True)
    deadline = time.monotonic() + timeout
    try:
        process.start()
        child.close()
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError(f"DDGS search exceeded {timeout:g} seconds")
        if not parent.poll(remaining):
            raise TimeoutError(f"DDGS search exceeded {timeout:g} seconds")
        try:
            succeeded, payload = parent.recv()
        except EOFError:
            return False, []
        # A result is not complete until the worker has exited. DDGS may hang
        # during executor cleanup after yielding its last response.
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError(f"DDGS search exceeded {timeout:g} seconds")
        process.join(remaining)
        if process.is_alive():
            raise TimeoutError(f"DDGS search exceeded {timeout:g} seconds")
        return succeeded, payload
    finally:
        parent.close()
        child.close()
        if process.is_alive():
            process.terminate()
            process.join(0.1)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(0.1)


class BraveSearchProvider:
    def __init__(self, *, api_key: str | None = None, request_fn: Callable[..., Any] | None = None, max_results: int = 10):
        self.api_key = api_key or os.getenv("BRAVE_SEARCH_API_KEY")
        self.request_fn = request_fn
        self.max_results = max_results

    def search(self, news_item: NewsItem) -> list[SourceRef]:
        if not self.api_key and self.request_fn is None:
            return []
        batches: list[list[SourceRef]] = []
        for query in _search_queries(news_item):
            try:
                if self.request_fn is not None:
                    payload = self.request_fn(query, api_key=self.api_key, count=self.max_results)
                else:
                    import httpx
                    response = httpx.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": self.max_results},
                        headers={"X-Subscription-Token": self.api_key},
                        timeout=12,
                    )
                    response.raise_for_status()
                    payload = response.json()
                results = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
                batches.append(_to_sources(results[: self.max_results], DiscoveryMethod.BRAVE, news_item))
            except Exception:
                continue
        return _merge_sources(*batches, limit=self.max_results * len(_search_queries(news_item)))


class FallbackSearchProvider:
    def __init__(self, brave: BraveSearchProvider | None = None, ddgs: DDGSSearchProvider | None = None, *, max_results: int = 5, timeout: float = 5.0):
        self.max_results = max_results
        # Brave remains injectable for explicit compatibility, while the
        # default discovery path uses public DDGS only.
        self.brave = brave
        self.ddgs = ddgs or DDGSSearchProvider(max_results=max_results, timeout=timeout)

    def search(self, news_item: NewsItem) -> list[SourceRef]:
        brave_results = self.brave.search(news_item) if self.brave is not None else []
        queries = _search_queries(news_item)
        if len(queries) > 1:
            # CG-specific recall is important enough to query both existing
            # providers, while other news keeps the original fallback cost.
            ddgs_results = self.ddgs.search(news_item)
            return _merge_sources(brave_results, ddgs_results, limit=self.max_results * 3)
        return brave_results or self.ddgs.search(news_item)
