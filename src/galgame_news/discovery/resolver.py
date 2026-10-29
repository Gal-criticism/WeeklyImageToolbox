"""Deterministic source resolution pipeline."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..domain import DiscoveryMethod, NewsItem, ReviewReason, SourceRef, SourceType
from .search import FallbackSearchProvider
from .http import SafeHttpClient
from .product_navigation import product_links


def _normalize(url: str) -> str:
    p = urlsplit(url.strip())
    # Keep useful section fragments: the HTML collector uses them to scope
    # gallery evidence to images inside the referenced section.
    fragment = p.fragment if p.fragment.casefold() in {"gallery", "graphic"} else ""
    return urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path or "/", p.query, fragment))


def _is_media_url(url: str) -> bool:
    return urlsplit(url).path.casefold().endswith((
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif",
        ".mp4", ".webm", ".m3u8",
    ))


def _visit_key(url: str) -> tuple[str, str, str, str, str]:
    parts = urlsplit(_normalize(url))
    return (parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", parts.query, parts.fragment.casefold())


class DefaultSourceResolver:
    def __init__(self, *, history_lookup: Callable[[NewsItem], Iterable[SourceRef]] | None = None, same_domain_lookup: Callable[[SourceRef, NewsItem], Iterable[SourceRef]] | None = None, same_domain_depth: int = 3, same_domain_transport: Callable[..., Any] | None = None, search_provider: Callable[[NewsItem], Iterable[SourceRef]] | None = None, search_max_results: int = 5, search_timeout: float = 5.0):
        self.history_lookup = history_lookup or (lambda news: [])
        self.same_domain_lookup = same_domain_lookup
        self.same_domain_depth = max(0, min(3, same_domain_depth))
        # Some official sites establish TLS slowly; a five-second discovery
        # timeout prevented their product pages from ever being reached.
        self.search_timeout = max(0.1, float(search_timeout))
        self.same_domain_client = SafeHttpClient(transport=same_domain_transport, timeout=max(20.0, search_timeout), max_retries=2)
        self.search_provider = search_provider or FallbackSearchProvider(max_results=search_max_results, timeout=search_timeout).search

    @staticmethod
    def _document_source(url: str) -> SourceRef:
        parts = urlsplit(url)
        host = (parts.hostname or "unknown").casefold()
        path = parts.path.casefold()
        image_suffixes = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
        video_suffixes = (".mp4", ".webm", ".m3u8")
        if path.endswith(video_suffixes):
            source_type, officiality = SourceType.VIDEO, 0.5
        elif path.endswith(image_suffixes):
            source_type, officiality = SourceType.DIRECT_IMAGE, 0.8
        elif host in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
            source_type, officiality = SourceType.OFFICIAL_X, 0.9
        elif host in {"store.steampowered.com", "steamcommunity.com"}:
            source_type, officiality = SourceType.STEAM, 0.85
        elif host in {"youtube.com", "www.youtube.com", "youtu.be", "bilibili.com", "www.bilibili.com"}:
            source_type, officiality = SourceType.VIDEO, 0.5
        elif host == "vndb.org" or host.endswith(".vndb.org"):
            source_type, officiality = SourceType.UNVERIFIED, 0.25
        else:
            source_type, officiality = SourceType.OFFICIAL_SITE, 0.75
        review = source_type in {SourceType.UNVERIFIED, SourceType.VIDEO}
        return SourceRef(
            url=url,
            domain=parts.hostname or "unknown",
            source_type=source_type,
            discovered_via=DiscoveryMethod.DOCUMENT,
            officiality=officiality,
            requires_review=review,
            review_reasons=[ReviewReason.UNCERTAIN_MATCH] if review else [],
            root_url=url,
        )

    @staticmethod
    def _gallery_link(base_url: str, href: str, label: str) -> str | None:
        target = _normalize(urljoin(base_url, href))
        if _is_media_url(target):
            return None
        base_host = (urlsplit(base_url).hostname or "").casefold()
        if (urlsplit(target).hostname or "").casefold() != base_host:
            return None
        parts = urlsplit(target)
        route = f"{parts.path} {parts.query} {parts.fragment}".casefold()
        tokens = ("gallery", "graphic", "/cg", "cg/", "ギャラリー", "画像一覧")
        if parts.query and not any(token in route for token in tokens):
            return None
        synthetic_label = label in {"gallery data link", "gallery iframe", "gallery modal"}
        evidence = route if synthetic_label else f"{route} {label.casefold()}"
        return target if any(token in evidence for token in tokens) else None

    @staticmethod
    def _portal_aliases(game_names: Iterable[str]) -> set[str]:
        def portal_key(value: str) -> str:
            normalized = unicodedata.normalize("NFKC", value).casefold()
            return "".join(char for char in normalized if not char.isspace() and unicodedata.category(char)[0] not in {"P", "S"})

        aliases: set[str] = set()
        for name in game_names:
            normalized = " ".join(name.casefold().split())
            if not normalized:
                continue
            key = portal_key(normalized)
            if len(key) >= 4:
                aliases.add(key)
            # News titles often identify a chapter while the official site
            # labels its portal with the parent work's base title.
            base = re.sub(r"^chapter\s*[:#：-]?\s*\d+\s*[:：-]?\s*", "", normalized, flags=re.I)
            base = re.sub(r"^(?:第\s*)?\d+\s*(?:章|話)\s*[:：-]?\s*", "", base)
            if base and base != normalized:
                base_key = portal_key(base)
                if len(base_key) >= 4:
                    aliases.add(base_key)
            # Some feeds place the parent title first and append the chapter
            # plus character/route name; the portal still uses the parent.
            parent_first = re.sub(r"\s+chapter\s*[:#：-]?\s*\d+\b.*$", "", normalized, flags=re.I)
            if parent_first and parent_first != normalized:
                parent_key = portal_key(parent_first)
                if len(parent_key) >= 4:
                    aliases.add(parent_key)
        return aliases

    def _page_children(self, source, news, page_url, effective_page_url, soup, is_entry):
        """Shared link policy for synchronous and asynchronous navigation."""
        root_url = source.root_url or source.url
        is_homepage = urlsplit(source.url).path in {"", "/"}
        links = []
        # Follow only an explicit affirmative age entry on this host. This is
        # navigation, so it consumes the same BFS depth and page budget.
        if re.search(r"18\s*歳|18\s*岁|18\s*years", soup.get_text(" ", strip=True), re.I):
            for entry in soup.select("#yes a[href], a#yes[href], a#enter-yes[href]"):
                target = _normalize(urljoin(effective_page_url, str(entry["href"])))
                if urlsplit(target).hostname == urlsplit(effective_page_url).hostname and not _is_media_url(target):
                    yield SourceRef(url=target, domain=urlsplit(target).hostname or source.domain,
                                    source_type=SourceType.OFFICIAL_SITE, discovered_via=DiscoveryMethod.SAME_DOMAIN,
                                    officiality=source.officiality, root_url=root_url,
                                    parent_url=effective_page_url, navigation_kind="age_confirmation")
        for anchor in soup.find_all("a", href=True):
            label = anchor.get_text(" ", strip=True)
            image_alt = " ".join(str(img.get("alt") or "") for img in anchor.find_all("img"))
            links.append((anchor["href"], f"{label} {image_alt}".strip()))
        links.extend(
            (tag["data-izimodal-iframeurl"], "gallery modal")
            for tag in soup.find_all(attrs={"data-izimodal-iframeurl": True})
        )
        links.extend((tag["src"], "gallery iframe") for tag in soup.find_all("iframe", src=True))
        for tag in soup.find_all(True):
            for attr in ("data-iframe", "data-iframe-src", "data-src"):
                value = tag.get(attr)
                if value and not urlsplit(value).path.casefold().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
                    links.append((value, "gallery data link"))
        yielded = set()
        for target, label, kind in product_links(news, source, effective_page_url, soup):
            child = _normalize(target)
            yielded.add(child)
            yield SourceRef(url=child, domain=urlsplit(child).hostname or source.domain,
                            source_type=SourceType.OFFICIAL_SITE, discovered_via=DiscoveryMethod.SAME_DOMAIN,
                            officiality=source.officiality, root_url=root_url,
                            parent_url=effective_page_url, navigation_kind=kind, navigation_text=label)
        for href, label in links:
            target = _normalize(urljoin(effective_page_url, href))
            if _is_media_url(target):
                continue
            same_host = (urlsplit(target).hostname or "").casefold() == (urlsplit(effective_page_url).hostname or "").casefold()
            child = self._gallery_link(effective_page_url, href, label)
            names = self._portal_aliases(news.game_names)
            exact_game_portal = same_host and self._portal_aliases([label]).intersection(names) != set()
            if child is None and exact_game_portal:
                child = target
            # The work portal links to numbered chapters before the CG
            # section. Keep this step inside the portal's own product tree.
            page_path = urlsplit(effective_page_url).path
            portal_dir = page_path.rsplit("/", 1)[0].rstrip("/") + "/"
            chapter_link = bool(re.search(r"(?:chapter\s*[:：#-]?\s*\d+|第\s*\d+\s*章)", label, re.I))
            same_work_tree = "/product/" in portal_dir and urlsplit(target).path.startswith(portal_dir)
            if child is None and same_host and chapter_link and same_work_tree and not urlsplit(target).query:
                child = target
            if not child:
                continue
            if child in yielded:
                continue
            # Entry pages may lead to the exact game portal; deeper
            # traversal follows only explicit gallery/graphic chapters.
            if is_entry:
                child_label = label.casefold()
                is_game_portal = bool(self._portal_aliases([child_label]).intersection(names))
                is_gallery = any(token in child_label for token in ("gallery", "graphic", "ギャラリー", "画像一覧", "cg"))
                if names and not (is_game_portal or (is_gallery and not is_homepage)):
                    continue
            yield SourceRef(url=child, domain=urlsplit(child).hostname or source.domain, source_type=SourceType.OFFICIAL_SITE, discovered_via=DiscoveryMethod.SAME_DOMAIN, officiality=source.officiality, root_url=root_url, parent_url=effective_page_url)

    def _crawl_same_domain(self, source: SourceRef, news: NewsItem, page_budget: list[int] | None = None, source_budget: list[int] | None = None) -> list[SourceRef]:
        if self.same_domain_depth <= 0 or source.source_type is not SourceType.OFFICIAL_SITE:
            return []
        found: list[SourceRef] = []
        root_url = source.root_url or source.url
        is_homepage = urlsplit(source.url).path in {"", "/"}
        queue = deque([(source.url, 0, True)])
        visited = {_visit_key(source.url)}
        page_budget = page_budget if page_budget is not None else [16]
        source_budget = source_budget if source_budget is not None else [16]
        while queue:
            page_url, depth, is_entry = queue.popleft()
            if depth >= self.same_domain_depth:
                continue
            if page_budget[0] <= 0 or source_budget[0] <= 0:
                break
            page_budget[0] -= 1
            try:
                response = self.same_domain_client.get(page_url)
                effective_page_url = str(getattr(response, "url", page_url) or page_url)
                soup = BeautifulSoup(getattr(response, "text", "") or "", "html.parser")
            except Exception:
                continue
            for child_ref in self._page_children(source, news, page_url, effective_page_url, soup, is_entry):
                child = child_ref.url
                if _visit_key(child) in visited or page_budget[0] <= 0 or source_budget[0] <= 0:
                    continue
                visited.add(_visit_key(child))
                found.append(child_ref)
                source_budget[0] -= 1
                queue.append((child, depth + 1, False))
        return found

    def resolve(self, news_item: NewsItem) -> list[SourceRef]:
        ordered: list[SourceRef] = []
        seen: set[str] = set()
        page_budget = [16]
        source_budget = [16]

        def add(source: SourceRef) -> None:
            key = _normalize(source.url)
            if key in seen: return
            if source.source_type is SourceType.OFFICIAL_SITE and sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered) >= 16:
                return
            seen.add(key)
            source.url = key
            if not source.domain: source.domain = urlsplit(key).hostname or "unknown"
            ordered.append(source)

        for url in news_item.source_urls:
            parts = urlsplit(url)
            if parts.scheme in {"http", "https"}:
                add(self._document_source(url))
        historical = list(self.history_lookup(news_item))
        for source in historical: add(source.model_copy(update={"discovered_via": DiscoveryMethod.HISTORY}))
        for source in list(ordered):
            source_budget[0] = min(source_budget[0], 16 - sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered))
            children = self.same_domain_lookup(source, news_item) if self.same_domain_lookup else self._crawl_same_domain(source, news_item, page_budget, source_budget)
            for child in children:
                add(child.model_copy(update={
                    "discovered_via": DiscoveryMethod.SAME_DOMAIN,
                    "root_url": child.root_url or source.root_url or source.url,
                    "parent_url": child.parent_url or source.url,
                }))
        for source in self.search_provider(news_item):
            add(source)
            # A trusted official search hit is an entry point, so perform the
            # same bounded gallery discovery as document/history sources.
            if source.source_type is SourceType.OFFICIAL_SITE and source.officiality >= 0.75 and _normalize(source.url) in seen:
                source_budget[0] = min(source_budget[0], 16 - sum(s.source_type is SourceType.OFFICIAL_SITE for s in ordered))
                for child in self._crawl_same_domain(source, news_item, page_budget, source_budget):
                    add(child.model_copy(update={
                        "discovered_via": DiscoveryMethod.SAME_DOMAIN,
                        "root_url": child.root_url or source.root_url or source.url,
                        "parent_url": child.parent_url or source.url,
                    }))
        return ordered
