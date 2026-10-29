"""Pure helpers for extracting explicitly linked video URLs from pages."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..discovery.http import UnsafeUrlError
from ..domain import (
    CollectionContext,
    FailureRecord,
    FailureStage,
    NewsItem,
    SourceRef,
    SourceType,
    VideoCandidate,
)


_VIDEO_SUFFIXES = (".mp4", ".webm", ".m3u8")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def is_direct_video_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return path.endswith(_VIDEO_SUFFIXES)


def is_youtube_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").casefold() in _YOUTUBE_HOSTS


def is_x_status_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        (parts.hostname or "").casefold() in _X_HOSTS
        and "/status/" in parts.path.casefold()
    )


def is_video_url(url: str) -> bool:
    return is_direct_video_url(url) or is_youtube_url(url) or is_x_status_url(url)


def canonical_video_url(raw_url: str, base_url: str | None = None) -> str | None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    value = urljoin(base_url or "", raw_url.strip())
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"}:
        return None
    host = (parts.hostname or "").casefold()
    if host in _YOUTUBE_HOSTS:
        video_id = ""
        if host == "youtu.be":
            video_id = parts.path.strip("/").split("/", 1)[0]
        else:
            query_id = parse_qs(parts.query).get("v", [""])[0]
            path_bits = [bit for bit in parts.path.split("/") if bit]
            if path_bits and path_bits[0].casefold() in {"embed", "shorts", "live"}:
                video_id = path_bits[1] if len(path_bits) > 1 else ""
            else:
                video_id = query_id
        if not video_id:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"
    if is_x_status_url(value):
        path = parts.path.rstrip("/")
        return urlunsplit(("https", "x.com", path, "", ""))
    if is_direct_video_url(value):
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, parts.query, ""))
    return None


def video_candidate(
    news_item: NewsItem,
    video_url: str,
    source_ref: SourceRef,
    *,
    title: str = "",
) -> VideoCandidate | None:
    canonical = canonical_video_url(video_url)
    if canonical is None:
        return None
    return VideoCandidate(
        news_id=news_item.id or "",
        source_url=source_ref.url,
        video_url=canonical,
        title=title,
        downloadable=True,
        discovered_via=source_ref.discovered_via,
    )


def discover_html_video_urls(
    html: str,
    page_url: str,
    *,
    include_anchor_links: bool = True,
    primary_content_only: bool = False,
) -> list[str]:
    """Extract explicit video tags/links and recognized embeds.

    X's public HTML is handled separately with ``include_anchor_links=False``;
    arbitrary navigation anchors must never become post entities.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    excluded = {"nav", "footer", "aside", "script", "style", "noscript"}

    def in_non_primary(node) -> bool:
        if not primary_content_only:
            return False
        while node is not None and getattr(node, "name", None):
            if node.name in excluded:
                return True
            marker = " ".join([str(node.get("id") or ""), " ".join(node.get("class") or []),
                               str(node.get("aria-label") or ""), str(node.get("data-testid") or "")]).casefold()
            if any(word in marker for word in ("recommend", "related", "suggest", "advert", "sponsor", "sidebar", "popular")):
                return True
            node = node.parent
        return False

    raw_values: list[str] = []
    for tag in soup.find_all(("video", "source")):
        if in_non_primary(tag):
            continue
        for attr in ("src", "data-src"):
            if tag.get(attr):
                raw_values.append(tag[attr])
    for tag in soup.find_all("iframe", src=True):
        if in_non_primary(tag):
            continue
        raw_values.append(tag["src"])
    if include_anchor_links:
        for tag in soup.find_all("a", href=True):
            if in_non_primary(tag):
                continue
            href = tag["href"]
            if is_video_url(urljoin(page_url, href)):
                raw_values.append(href)
    # Some pages serialize an iframe/video URL in JSON-LD or application state.
    for script in (() if primary_content_only else soup.find_all("script", type="application/ld+json")):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        raw_values.extend(_video_strings(payload))
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        canonical = canonical_video_url(raw, page_url)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def _video_strings(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str) and is_video_url(value):
        values.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            values.extend(_video_strings(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(_video_strings(child))
    return values


def explicit_x_html_entity_urls(html: str, page_url: str) -> list[str]:
    """Extract only links with explicit post/card context from public X HTML.

    X's public HTML contains many navigation anchors.  A link is accepted only
    when it is inside a ``tweetText``/card container (or carries that marker);
    X/Twitter internal navigation is never treated as an external entity.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    result: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        node = tag
        in_post_context = False
        while node is not None and getattr(node, "name", None):
            markers: list[str] = []
            for key in ("data-testid", "aria-label", "class"):
                value = node.get(key)
                if isinstance(value, (list, tuple)):
                    markers.extend(str(item) for item in value)
                elif value:
                    markers.append(str(value))
            marker = " ".join(markers).casefold()
            if "tweettext" in marker or "card" in marker:
                in_post_context = True
                break
            node = node.parent
        if not in_post_context:
            continue
        href = urljoin(page_url, str(tag["href"]).strip())
        if not href:
            continue
        host = (urlsplit(href).hostname or "").casefold()
        if host in _X_HOSTS:
            # Status/user/home/intent/share/hashtag/login links are internal
            # navigation, not expanded post entities.
            continue
        if href not in seen:
            seen.add(href)
            result.append(href)
    return result


def explicit_entity_urls(payload: Any) -> list[str]:
    """Extract expanded/unwound entity URLs without treating free text as links."""
    result: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"expanded_url", "unwound_url"} and isinstance(child, str):
                    result.append(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return list(dict.fromkeys(result))


def _failure_for_entity(
    news_item: NewsItem,
    source_ref: SourceRef,
    raw_url: str,
    exc: Exception,
) -> FailureRecord:
    unsafe = isinstance(exc, UnsafeUrlError)
    return FailureRecord(
        stage=FailureStage.COLLECT,
        news_id=news_item.id,
        code="unsafe_external_url" if unsafe else "external_video_fetch_failed",
        message=str(exc),
        source_url=raw_url or source_ref.url,
        retryable=not unsafe,
    )


def collect_entity_videos(
    news_item: NewsItem,
    source_ref: SourceRef,
    payload: Any,
    *,
    fetch_html: Callable[[str], str | tuple[str, str]] | None = None,
    url_validator: Callable[[str], None] | None = None,
    failures: list[FailureRecord] | None = None,
) -> list[VideoCandidate]:
    """Collect one-hop entity videos with optional production URL validation."""
    result: list[VideoCandidate] = []
    seen: set[str] = set()
    for raw_url in explicit_entity_urls(payload):
        try:
            if url_validator is not None:
                url_validator(raw_url)
            candidate = video_candidate(news_item, raw_url, source_ref)
            if candidate is not None:
                if candidate.video_url not in seen:
                    seen.add(candidate.video_url)
                    result.append(candidate)
                continue
            if fetch_html is None:
                continue
            parts = urlsplit(raw_url)
            if parts.scheme.casefold() not in {"http", "https"}:
                continue
            fetched = fetch_html(raw_url)
            child_base = raw_url
            child_html = fetched
            if isinstance(fetched, tuple):
                child_base, child_html = fetched
            # A t.co/expanded entity may redirect straight to a recognized video.
            redirected = video_candidate(news_item, child_base, source_ref)
            if redirected is not None and redirected.video_url not in seen:
                seen.add(redirected.video_url)
                result.append(redirected)
                continue
            for child_url in discover_html_video_urls(child_html, child_base):
                if url_validator is not None:
                    url_validator(child_url)
                child = video_candidate(
                    news_item,
                    child_url,
                    SourceRef(
                        url=raw_url,
                        domain=parts.hostname or "unknown",
                        source_type=SourceType.OFFICIAL_SITE,
                    ),
                )
                if child is not None and child.video_url not in seen:
                    seen.add(child.video_url)
                    result.append(child)
        except Exception as exc:
            if failures is not None:
                failures.append(_failure_for_entity(news_item, source_ref, raw_url, exc))
            continue
    return result
