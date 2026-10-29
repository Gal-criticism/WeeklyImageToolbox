"""Bounded source discovery and public YMgal picset collection."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from ..domain import CollectionResult, DiscoveryMethod, FailureRecord, FailureStage, ImageCandidate, ImageEvidence, SourceRef, SourceType
from ..discovery.search import DDGSSearchProvider

_STAGES = {"official", "store", "ymgal", "vndb"}
_EXCLUDED = {"www.ymgal.games", "ymgal.games", "vndb.org", "www.vndb.org", "store.steampowered.com", "x.com", "twitter.com", "www.youtube.com", "youtube.com", "youtu.be", "bilibili.com", "www.bilibili.com"}


def _eligible(url, stage):
    p = urlsplit(str(url))
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        return False
    host = p.hostname.lower()
    if stage == "ymgal":
        return host in {"ymgal.games", "www.ymgal.games"} and bool(re.fullmatch(r"/co/picset/\d+/?", p.path))
    if stage == "vndb":
        return host in {"vndb.org", "www.vndb.org"} and bool(re.fullmatch(r"/[vr]\d+/?", p.path))
    if stage == "store":
        return host == "store.steampowered.com" and bool(re.match(r"/app/\d+", p.path))
    return host not in _EXCLUDED and not host.endswith(".ymgal.games")


def discover_sources(news, *, stage, config, transport=None, skip_url=None):
    if stage not in _STAGES:
        raise ValueError(f"unknown supplement stage: {stage}")
    skip_url = skip_url or (lambda _url: False)
    explicit = [u for u in news.source_urls if _eligible(u, stage) and not skip_url(u)]
    suffix = {"official": "公式 gallery graphic", "store": "site:store.steampowered.com/app/", "ymgal": "site:ymgal.games/co/picset/", "vndb": "site:vndb.org"}[stage]
    names = list(dict.fromkeys(news.game_names)) or [news.title]
    urls = list(explicit)
    for name in names[:2]:
        if len(urls) >= 2:
            break
        query = f'"{name}" {suffix}'
        if transport is not None and hasattr(transport, "search"):
            hits = transport.search(query, stage=stage, limit=5)
        else:
            scoped = news.model_copy(deep=True)
            scoped.game_names = [query]
            hits = DDGSSearchProvider(max_results=5).search(scoped)
        for hit in hits:
            url = hit.get("url", "") if isinstance(hit, dict) else getattr(hit, "url", "")
            if _eligible(url, stage) and url not in urls and not skip_url(url):
                urls.append(url)
    return [SourceRef(url=u, domain=urlsplit(u).hostname, source_type=SourceType.STEAM if stage == "store" else SourceType.UNVERIFIED,
                      discovered_via=DiscoveryMethod.DOCUMENT if u in explicit else DiscoveryMethod.DDGS,
                      officiality=0.82 if stage == "store" else 0.4, requires_review=True) for u in urls[:2]]


class YmgalPicsetAdapter:
    """Collect only main slider assets, excluding avatars and recommendations."""
    def __init__(self, client):
        self.client = client

    def collect(self, news_item, source_ref, context):
        response = self.client.get(source_ref.url)
        page = str(getattr(response, "url", source_ref.url) or source_ref.url)
        if int(getattr(response, "status_code", 200)) != 200 or not _eligible(page, "ymgal"):
            return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT, news_id=news_item.id, code="ymgal_page_unavailable", message="月幕图集页面不可用", source_url=source_ref.url, retryable=True)])
        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
        nodes = soup.select("#main-picset-warp .list-box [data-background], #main-picset-warp .list-box img[data-original], #main-picset-warp .list-box img[src]")
        values, seen = [], set()
        for node in nodes:
            url = urljoin(page, node.get("data-background") or node.get("data-original") or node.get("src") or "")
            p = urlsplit(url)
            if p.hostname != "cdn.ymgal.games" or not p.path.startswith("/topic/content/") or url in seen:
                continue
            seen.add(url)
            values.append(ImageCandidate(news_id=news_item.id, image_url=url, source_url=page, source_type=SourceType.UNVERIFIED,
                fetched_at=context.now, nearby_text=title,
                evidence=[ImageEvidence(page_url=page, container="ymgal:picset-main-slider", role="game_cg", method="dom", relationship=title)],
                signals={"supplement_page_title": title, "supplement_gallery": True}))
            if len(values) >= context.max_candidates:
                break
        return CollectionResult(candidates=values)
