"""HTML-backed source adapters."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ...domain import CollectionContext, CollectionResult, FailureRecord, FailureStage, ImageEvidence, ReviewReason
from ...domain import DiscoveryMethod, SourceType
from ...video.discovery import discover_html_video_urls
from ..gallery_scripts import gallery_script_sources, parse_gallery_script
from ..image_evidence import evidence_for_css_image, evidence_for_image
from ..relevance import news_relevance
from ..http import SafeHttpClient
from .common import (
    _candidate,
    _image_priority,
    _looks_like_image_url,
    _srcset_largest,
    _upgrade_image_url,
    _video_candidates,
)


def _explicit_age_confirmation_cookie(soup: BeautifulSoup, html: str) -> str | None:
    """Recognize the publisher's explicit 18+ action and its cookie effect."""
    confirmation = soup.find(id="confirm-yes")
    if confirmation is None or not re.search(r"18", confirmation.get_text(" ", strip=True)):
        return None
    match = re.search(
        r"Cookies\s*\.\s*set\s*\(\s*(['\"])PermitRate\1\s*,\s*(['\"])18\2",
        html,
        flags=re.I,
    )
    return "18" if match else None


def _is_age_gate(soup: BeautifulSoup, html: str) -> bool:
    lowered = html.casefold()
    return bool(
        soup.find("meta", attrs={"name": lambda value: bool(value and (
            re.search(r"(?:^|[-_ ])(?:age|adult)(?:[-_ ]|$)", value.casefold())
            and re.search(r"(?:^|[-_ ])(?:gate|verify|verification|confirm)(?:[-_ ]|$)", value.casefold())
        ))})
        or "age-verification" in lowered
        or "age gate" in lowered
        or soup.find(id="confirm-yes") is not None
        or (soup.select_one("#yes a[href], a#yes[href], a#enter-yes[href]") is not None
            and bool(re.search(r"18\s*歳|18\s*岁|18\s*years", soup.get_text(" ", strip=True), re.I)))
    )


def _page_identity_evidence(news_item, source_ref, soup: BeautifulSoup, page_url: str) -> tuple[bool, bool]:
    """Return (accepted, has_identity_text) using primary page content only."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not title:
        title = " ".join(str(meta.get("content") or "") for meta in soup.find_all("meta")
                         if (meta.get("property") or meta.get("name") or "").casefold() in {"og:title", "twitter:title"})
    description = " ".join(str(meta.get("content") or "") for meta in soup.find_all("meta")
                           if (meta.get("property") or meta.get("name") or "").casefold() in {"description", "og:description", "twitter:description"})
    content_soup = deepcopy(soup)
    for node in list(content_soup.find_all(["nav", "footer", "aside", "script", "style", "noscript", "iframe"] )):
        node.decompose()
    for node in list(content_soup.find_all(True)):
        labels = " ".join([str(node.get("id") or ""), " ".join(node.get("class") or [])]).casefold()
        if re.search(r"recommend|related|suggest|popular|ranking|advert|sponsor|footer|breadcrumb|sidebar", labels):
            node.decompose()
    primary = content_soup.select_one("main, article, [role=main]") or content_soup.body or content_soup
    body_text = " ".join(primary.stripped_strings)[:12000]
    search_text = f"{source_ref.search_title} {source_ref.search_snippet}"
    title_identity = news_relevance(news_item, f"{title} {description}", page_url)
    body_identity = news_relevance(news_item, body_text, "")
    search_identity = news_relevance(news_item, search_text, source_ref.url)
    identity = title_identity or body_identity
    # Search-provider hits and explicitly unverified sources must establish
    # their own identity. A search snippet is supporting evidence only; it
    # cannot replace a page whose primary content identifies something else.
    searched = source_ref.discovered_via in {DiscoveryMethod.DDGS, DiscoveryMethod.BRAVE} or source_ref.source_type is SourceType.UNVERIFIED
    if searched:
        accepted = identity
    else:
        page_identity_text = f"{title} {description}".strip()
        explicit_game_context = re.search(
            r"visual\s*novel|galgame|game|游戏|遊戲|ノベルゲーム|ビジュアルノベル|ギャルゲー",
            page_identity_text,
            re.I,
        ) is not None
        conflicting_page_identity = explicit_game_context and not title_identity
        trusted_chain = (
            not conflicting_page_identity
            and source_ref.source_type is SourceType.OFFICIAL_SITE
            and source_ref.officiality >= 0.7
            and bool(set(news_item.source_urls).intersection(source_ref.source_chain))
            and search_identity
        )
        accepted = identity or trusted_chain
    has_text = bool((title + description + body_text).strip())
    return accepted, has_text


def _should_gate_page(source_ref) -> bool:
    return (
        source_ref.discovered_via in {DiscoveryMethod.DDGS, DiscoveryMethod.BRAVE}
        or source_ref.discovered_via is DiscoveryMethod.DOCUMENT
        or source_ref.discovered_via is DiscoveryMethod.SAME_DOMAIN
        or source_ref.source_type is SourceType.UNVERIFIED
        or bool((source_ref.search_title or "").strip() or (source_ref.search_snippet or "").strip())
    )


class OfficialHtmlAdapter:
    def __init__(self, *, transport: Callable[..., Any] | None = None, client: SafeHttpClient | None = None):
        self.client = client or SafeHttpClient(transport=transport, timeout=20.0, max_retries=2)

    def collect(self, news_item, source_ref, context: CollectionContext) -> CollectionResult:
        try:
            response = self.client.get(source_ref.url)
            page_url = str(getattr(response, "url", source_ref.url) or source_ref.url)
            html = getattr(response, "text", "") or ""
            status = int(getattr(response, "status_code", 200))
            if not 200 <= status < 300:
                return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT, news_id=news_item.id,
                    code="page_http_error", message=f"来源页面返回 HTTP {status}，未取得页面内容。", source_url=page_url,
                    retryable=status in {408,429} or status >= 500)])
            soup = BeautifulSoup(html, "html.parser")
            confirmation_cookie = _explicit_age_confirmation_cookie(soup, html)
            confirmed_age_gate = False
            if confirmation_cookie:
                response = self.client.get(page_url, cookies={"PermitRate": confirmation_cookie})
                page_url = str(getattr(response, "url", page_url) or page_url)
                html = getattr(response, "text", "") or ""
                soup = BeautifulSoup(html, "html.parser")
                confirmed_age_gate = not _is_age_gate(soup, html)
                status = int(getattr(response, "status_code", 200))
                if not 200 <= status < 300:
                    return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT,news_id=news_item.id,
                        code="page_http_error",message=f"年龄确认后的页面返回 HTTP {status}。",source_url=page_url,
                        retryable=status in {408,429} or status >= 500)])
            if not _is_age_gate(soup, html) and _should_gate_page(source_ref):
                accepted, has_text = _page_identity_evidence(news_item, source_ref, soup, page_url)
                if not accepted:
                    code = "page_relevance_rejected" if has_text else "page_relevance_unverified"
                    source_ref.requires_review = True
                    return CollectionResult(failures=[FailureRecord(
                        stage=FailureStage.COLLECT, news_id=news_item.id, code=code,
                        message="页面身份未能与本条游戏新闻核对，已在素材提取前跳过。",
                        source_url=page_url, retryable=False,
                    )])
            scripts = {}
            script_failures = []
            if not _is_age_gate(soup, html):
                for script_url in gallery_script_sources(soup, page_url):
                    try:
                        script_response = self.client.get(script_url)
                        script_status = int(getattr(script_response, "status_code", 200))
                        if not 200 <= script_status < 300:
                            script_failures.append(FailureRecord(stage=FailureStage.COLLECT,news_id=news_item.id,
                                code="gallery_script_http_error",message=f"图库脚本返回 HTTP {script_status}。",source_url=script_url,
                                retryable=script_status in {408,429} or script_status >= 500))
                            continue
                        scripts[script_url] = getattr(script_response, "text", "") or ""
                    except Exception as exc:
                        script_failures.append(FailureRecord(stage=FailureStage.COLLECT,news_id=news_item.id,
                            code="gallery_script_fetch_failed",message=f"图库脚本获取失败（{type(exc).__name__}）。",source_url=script_url,retryable=True))
                        continue
            result = self.parse(
                news_item, source_ref, context, html, page_url=page_url,
                method="dom", _script_sources=scripts, _confirmed_age_gate=confirmed_age_gate,
            )
            result.failures.extend(script_failures)
            if self.needs_render(html, result):
                source_ref.requires_review = True
                source_ref.review_reasons = list(dict.fromkeys([*source_ref.review_reasons, ReviewReason.DYNAMIC_PAGE]))
                result.manual_review_reasons = list(dict.fromkeys([*result.manual_review_reasons, ReviewReason.DYNAMIC_PAGE]))
            return result
        except Exception as exc:
            return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT, news_id=news_item.id, code="adapter_error", message=str(exc).strip() or f"页面采集失败（{type(exc).__name__}）。", source_url=source_ref.url, retryable=True)])

    @staticmethod
    def needs_render(html: str, result: CollectionResult) -> bool:
        """Flag unresolved DOM gallery shells for browser rendering/review."""
        soup = BeautifulSoup(html or "", "html.parser")
        has_gallery_shell = bool(
            soup.find(id="galleryPreview")
            or soup.find(id="galleryThumbnailTrack")
            or soup.select_one("[id^='gallery_cg_box_'], [class*='gallery_cg_box']")
        )
        if not has_gallery_shell:
            has_gallery_shell = bool(soup.select_one("[data-gallery], .gallery, #gallery, #GALLERY")) and bool(soup.find("script"))
        if not has_gallery_shell:
            return False
        # A lone fallback preview does not prove that a script gallery was
        # collected. Require an expanded list or actual thumbnail items.
        if soup.find(id="galleryThumbnailTrack") is not None:
            return not any(e.method == "script" and e.role in {"game_cg","background_art"} for c in result.candidates for e in c.evidence) and not bool(
                soup.select_one("#galleryThumbnailTrack img"))
        preview = soup.find(id="galleryPreview")
        if preview is not None and soup.find("script") and not any(e.method == "script" and e.role in {"game_cg","background_art"} for c in result.candidates for e in c.evidence):
            return not any(img is not preview and evidence_for_image(img, soup, "https://local.example/") is not None
                           and evidence_for_image(img, soup, "https://local.example/").role in {"game_cg", "background_art"}
                           for img in soup.find_all("img"))
        return not any(e.role in {"game_cg", "background_art"} for c in result.candidates for e in c.evidence)

    def parse(
        self, news_item, source_ref, context: CollectionContext, html: str,
        page_url: str | None = None, method: str = "dom", *,
        _script_sources: dict[str, str] | None = None, _confirmed_age_gate: bool = False,
    ) -> CollectionResult:
        try:
            page_url = page_url or source_ref.url
            soup = BeautifulSoup(html or "", "html.parser")
            confirmed_age_gate = _confirmed_age_gate
            if _is_age_gate(soup, html or ""):
                source_ref.requires_review = True
                source_ref.review_reasons = list(dict.fromkeys([*source_ref.review_reasons, ReviewReason.AGE_GATE]))
                return CollectionResult(manual_review_reasons=[ReviewReason.AGE_GATE])
            if _should_gate_page(source_ref):
                accepted, has_text = _page_identity_evidence(news_item, source_ref, soup, page_url)
                if not accepted:
                    source_ref.requires_review = True
                    return CollectionResult(failures=[FailureRecord(
                        stage=FailureStage.COLLECT, news_id=news_item.id,
                        code="page_relevance_rejected" if has_text else "page_relevance_unverified",
                        message="页面身份未能与本条游戏新闻核对，已在素材提取前跳过。",
                        source_url=page_url, retryable=False,
                    )])
            urls: list[str] = []
            page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if not page_title:
                for meta in soup.find_all("meta"):
                    key = (meta.get("property") or meta.get("name") or "").casefold()
                    if key in {"og:title", "twitter:title"} and meta.get("content"):
                        page_title = str(meta["content"]).strip()
                        break
            image_context: dict[str, list[tuple[str, str]]] = {}
            image_gallery_context: dict[str, list[str]] = {}
            image_variant_context: dict[str, str] = {}
            image_exclusions: dict[str, str] = {}
            image_evidence_context: dict[str, list[ImageEvidence]] = {}
            video_urls = discover_html_video_urls(html, page_url, primary_content_only=True)
            script_gallery_entries = []
            all_scripts = {f"inline:{index}": tag.get_text() for index, tag in enumerate(soup.find_all("script")) if not tag.get("src")}
            all_scripts.update(_script_sources or {})
            for script_url, script_text in all_scripts.items():
                entries = parse_gallery_script(script_text, page_url, soup)
                script_gallery_entries.extend(entries)
                preview_evidence = evidence_for_image(soup.find(id="galleryPreview"),soup,page_url,method="script")
                for entry in entries:
                    urls.append(entry.url)
                    normalized = _upgrade_image_url(urljoin(page_url, entry.url))
                    if normalized:
                        image_evidence_context.setdefault(normalized, []).append(ImageEvidence(
                            page_url=page_url, container="#galleryPreview / #galleryThumbnailTrack",
                            item_id=entry.item_id, role=preview_evidence.role if preview_evidence else "unknown",
                            text=preview_evidence.text if preview_evidence and preview_evidence.text else "Gallery image",
                            method="script", relationship=f"{entry.relationship} ({script_url})",
                            variant_of=entry.thumbnail_url,
                        ))

            def excluded_role(tag) -> str | None:
                # An explicit local image role takes precedence over a gallery
                # ancestor. Do not use asset filenames or the page title here.
                for node in [tag, *list(tag.parents)]:
                    if getattr(node, "name", None) in {"html", "body", "head", "[document]"}:
                        continue
                    if getattr(node, "name", None) in {"header", "footer", "nav"}:
                        return str(node.name)
                    tokens = " ".join([
                        str(node.get("id") or ""), *map(str, node.get("class") or []),
                        str(node.get("data-section") or ""),
                        str(node.get("alt") or "") if node is tag else "",
                    ])
                    match = re.search(
                        r"(?:^|[\s_-])(logo|header|footer|nav|navbar|navigation|character|chara|goods|merchandise|special|campaign|package|news)(?:$|[\s_-])|ロゴ|キャラクター|グッズ",
                        tokens, re.I,
                    )
                    if match:
                        return match.group(1) or match.group(0)
                return None

            def gallery_scoped(tag, anchor=None) -> str | None:
                if excluded_role(tag):
                    return None
                nodes = [tag, *list(tag.parents)]
                if anchor is not None:
                    nodes.append(anchor)
                for node in nodes:
                    if getattr(node, "name", None) in {"html", "body", "head", "[document]"}:
                        continue
                    tokens = [str(node.get("id") or ""), *map(str, node.get("class") or []), str(node.get("data-section") or "")]
                    if any(re.match(r"^(?:gallery|graphic)(?:$|[_-])", token, re.I) or token in {"ギャラリー", "画像一覧"} for token in tokens):
                        label = node.name + ("#" + str(node["id"]) if node.get("id") else "".join("." + str(value) for value in node.get("class") or []))
                        return "dom:" + label
                return None

            def remember_context(raw_url: str, tag, anchor=None, *, relationship="image attribute", reference=None, evidence_tag=None) -> None:
                if not isinstance(raw_url, str):
                    return
                normalized = _upgrade_image_url(urljoin(page_url, raw_url))
                context_tag = evidence_tag if evidence_tag is not None else tag
                alt = str(tag.get("alt") or context_tag.get("alt") or "").strip()
                # Broad section/document text is not image-local evidence.
                local_parent = getattr(context_tag, "parent", None)
                nearby = local_parent.get_text(" ", strip=True) if local_parent and local_parent.name in {"a", "figure", "figcaption", "picture", "li"} else ""
                nearby = re.sub(r"\s+", " ", nearby)[:500]
                if normalized and (alt or nearby):
                    image_context.setdefault(normalized, []).append((alt, nearby))
                if normalized:
                    if reference and normalized != reference:
                        image_variant_context.setdefault(normalized, reference)
                    provenance = evidence_for_image(
                        evidence_tag if evidence_tag is not None else tag,
                        soup, page_url, method=method, relationship=relationship,
                        variant_of=reference if reference and normalized != reference else None,
                    )
                    if provenance is not None:
                        image_evidence_context.setdefault(normalized, []).append(provenance)
                        if provenance.text:
                            image_context.setdefault(normalized, []).append((alt, provenance.text))
                    role = excluded_role(tag)
                    if role:
                        image_exclusions[normalized] = role
                    gallery_source = gallery_scoped(evidence_tag if evidence_tag is not None else tag, anchor)
                    if gallery_source and not role:
                        image_gallery_context.setdefault(normalized, []).append(f"{gallery_source} via {relationship}")

            image_attrs = ("data-original", "data-full", "data-large", "data-hires", "data-zoom-image", "src", "data-src", "data-lazy-src")

            def image_variants(tag) -> list[tuple[str, str]]:
                variants = [(str(tag[attr]), f"img[{attr}]") for attr in image_attrs if tag.get(attr)]
                for attr in ("srcset", "data-srcset"):
                    if tag.get(attr):
                        largest = _srcset_largest(str(tag[attr]))
                        if largest:
                            variants.append((largest, f"img[{attr}]"))
                picture = tag.find_parent("picture")
                if picture is not None and len(picture.find_all("img")) == 1:
                    for source in picture.find_all("source"):
                        for attr in ("srcset", "data-srcset"):
                            if source.get(attr):
                                largest = _srcset_largest(str(source[attr]))
                                if largest:
                                    variants.append((largest, f"picture/source[{attr}]"))
                return variants

            for anchor in soup.find_all("a", href=True):
                href = anchor["href"]
                if _looks_like_image_url(href) or (anchor.find("img") is not None and any(token in urlsplit(href).path.casefold() for token in ("gallery", "cg", "image", "media"))):
                    urls.append(href)
            for tag in soup.find_all("meta"):
                key = (tag.get("property") or tag.get("name") or "").casefold()
                if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"} and tag.get("content"):
                    urls.append(tag["content"])
            for tag in soup.find_all("img"):
                # The HTML fallback preview is one presentation slot, not an
                # additional collection item, when a bounded script list gave
                # us the gallery's actual distinct assets.
                if script_gallery_entries and tag.get("id") == "galleryPreview":
                    continue
                variants = image_variants(tag)
                anchor = tag.find_parent(["a", "button"])
                linked_full = None
                if anchor is not None and len(anchor.find_all("img")) == 1:
                    href = str(anchor.get("href") or "")
                    if _looks_like_image_url(href):
                        linked_full = href
                        variants.append((href, "anchor[href]"))
                # Static references to one modal image are an explicit
                # relationship even when the full image lives elsewhere.
                for trigger in (tag, tag.parent, anchor):
                    if trigger is None or (trigger is not tag and len(trigger.find_all("img")) != 1):
                        continue
                    for attr in ("href", "data-target", "data-bs-target", "data-modal", "data-izimodal-open", "aria-controls"):
                        selector = str(trigger.get(attr) or "")
                        target_id = selector.removeprefix("#") if selector.startswith("#") or attr == "aria-controls" else ""
                        target = soup.find(id=target_id) if target_id else None
                        target_images = target.find_all("img") if target is not None else []
                        if len(target_images) == 1:
                            for value, relation in image_variants(target_images[0]):
                                reference = _upgrade_image_url(urljoin(page_url, str(tag.get("src") or tag.get("data-src") or variants[0][0]))) if variants else None
                                trigger_name = "anchor" if trigger.name == "a" else trigger.name
                                remember_context(value, target_images[0], relationship=f"{trigger_name}[{attr}] -> #{target_id}/{relation}", reference=reference, evidence_tag=tag)
                                urls.append(value)
                # Full-size attributes on a one-image wrapper refer to this
                # item; shared containers with several images are ambiguous.
                for wrapper in (tag.parent, anchor):
                    if wrapper is not None and len(wrapper.find_all("img")) == 1:
                        for attr in image_attrs[:5]:
                            if wrapper.get(attr):
                                variants.append((str(wrapper[attr]), f"{wrapper.name}[{attr}]"))
                reference = _upgrade_image_url(urljoin(page_url, str(tag.get("src") or tag.get("data-src") or variants[0][0]))) if variants else None
                for value, relationship in variants:
                    remember_context(value, tag, anchor, relationship=relationship, reference=reference)
                    if not (linked_full and value in {tag.get(attr) for attr in ("src", "data-src", "data-lazy-src")}):
                        urls.append(value)
            for tag in soup.find_all("source"):
                for attr in ("srcset", "data-srcset"):
                    if tag.get(attr):
                        largest = _srcset_largest(str(tag[attr]))
                        if largest:
                            urls.append(largest)
                            picture = tag.find_parent("picture")
                            # A fallback img already provided this source's
                            # image-local context, including any excluded role.
                            if picture is None or picture.find("img") is None or excluded_role(tag):
                                remember_context(largest, tag, relationship=f"source[{attr}]")
            for tag in soup.find_all(style=True):
                for value in re.findall(r"url\(\s*['\"]?([^'\")]+)", tag.get("style", ""), flags=re.I):
                    urls.append(value)
                    normalized = _upgrade_image_url(urljoin(page_url, value))
                    if normalized and re.search(r"background(?:-image)?\s*:", tag.get("style", ""), re.I):
                        image_evidence_context.setdefault(normalized, []).append(
                            evidence_for_css_image(tag, soup, page_url, method=method)
                        )
            for tag in soup.find_all(True):
                for attr in ("data-background", "data-bg", "data-image"):
                    if tag.get(attr):
                        urls.append(tag[attr])
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or script.get_text())
                    def product_nodes(value: Any):
                        if isinstance(value, dict):
                            kind = value.get("@type", "")
                            kinds = kind if isinstance(kind, list) else [kind]
                            if any(str(item).casefold() == "product" for item in kinds):
                                name = str(value.get("name") or "").strip()
                                image_value = value.get("image", [])
                                image_values = image_value if isinstance(image_value, list) else [image_value]
                                for image_value in image_values:
                                    if isinstance(image_value, dict):
                                        image_value = image_value.get("url") or image_value.get("contentUrl")
                                    if isinstance(image_value, str):
                                        normalized = _upgrade_image_url(urljoin(page_url, image_value))
                                        urls.append(image_value)
                                        if normalized and name:
                                            image_evidence_context.setdefault(normalized, []).append(ImageEvidence(
                                                page_url=page_url, container="jsonld:Product",
                                                item_id=str(value.get("@id") or ""), role="goods",
                                                text=name, method=method, relationship="JSON-LD Product.image",
                                            ))
                            for child in value.values():
                                product_nodes(child)
                        elif isinstance(value, list):
                            for child in value:
                                product_nodes(child)
                    product_nodes(data)
                    values = data.get("image", []) if isinstance(data, dict) else []
                    urls.extend(values if isinstance(values, list) else [values])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

            def collect_json_images(value: Any) -> None:
                if isinstance(value, str):
                    if value.startswith(("http://", "https://", "/", "wix:image://v1/")):
                        urls.append(value)
                elif isinstance(value, dict):
                    for child in value.values():
                        collect_json_images(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_json_images(child)

            for script in soup.find_all("script"):
                text = script.string or script.get_text()
                if script.get("type") == "application/json" or script.get("id") == "__NEXT_DATA__" or "wix-warmup" in (script.get("id") or "").casefold() or "wix-viewer" in (script.get("id") or "").casefold():
                    try:
                        collect_json_images(json.loads(text))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
            embedded = html.replace(r"\/", "/")
            urls.extend(re.findall(r"https://static\.wixstatic\.com/media/[^\"'<>\\\s]+", embedded))
            urls.extend(re.findall(r"wix:image://v1/[^\"'<>\\\s]+", embedded))
            candidates = []
            seen = set()
            for value in urls:
                if not isinstance(value, str):
                    continue
                image_url = _upgrade_image_url(urljoin(page_url, value))
                if not image_url or image_url in seen or urlsplit(image_url).scheme not in {"http", "https"}:
                    continue
                if not _looks_like_image_url(image_url):
                    continue
                seen.add(image_url)
                effective_source = source_ref.model_copy(update={"url": page_url, "root_url": source_ref.root_url or source_ref.url})
                candidate = _candidate(news_item, image_url, effective_source, context)
                if page_title:
                    candidate.signals["page_title"] = page_title
                if image_context.get(image_url):
                    candidate.image_alt = " ".join(dict.fromkeys(alt for alt, _ in image_context[image_url] if alt)) or None
                    candidate.nearby_text = " ".join(dict.fromkeys(text for _, text in image_context[image_url] if text)) or None
                    if candidate.image_alt:
                        candidate.signals["alt"] = candidate.image_alt
                    if candidate.nearby_text:
                        candidate.signals["nearby_text"] = candidate.nearby_text
                if image_url in image_variant_context:
                    candidate.signals["media_variant_of"] = image_variant_context[image_url]
                if image_evidence_context.get(image_url):
                    unique_evidence = []
                    seen_evidence = set()
                    for item in image_evidence_context[image_url]:
                        key = item.model_dump_json()
                        if key not in seen_evidence:
                            seen_evidence.add(key)
                            unique_evidence.append(item)
                    candidate.evidence = unique_evidence
                    # Provenance is item-local, while the legacy fields remain
                    # useful for existing scoring and review screens.
                    local_text = " ".join(dict.fromkeys(item.text for item in candidate.evidence if item.text))
                    if local_text:
                        candidate.nearby_text = candidate.nearby_text or local_text
                    roles = {item.role for item in candidate.evidence}
                    if "decorative" in roles:
                        candidate.signals["gallery_excluded_reason"] = "decorative"
                    elif roles.intersection({"goods", "logo", "character_art", "cover", "ui", "banner"}):
                        candidate.signals["gallery_excluded_reason"] = sorted(roles.intersection({"goods", "logo", "character_art", "cover", "ui", "banner"}))[0]
                    elif roles.intersection({"game_cg", "background_art"}) and not roles.intersection({"goods", "logo", "character_art", "cover", "ui", "banner"}):
                        candidate.signals["gallery_path"] = True
                        candidate.signals["gallery_evidence_source"] = "; ".join(
                            f"{e.method}:{e.container} via {e.relationship}" for e in candidate.evidence if e.role in {"game_cg", "background_art"})
                if candidate.signals.get("gallery_excluded_reason"):
                    candidate.signals.pop("gallery_path", None)
                    candidate.signals.pop("gallery_evidence_source", None)
                if image_url in image_exclusions:
                    candidate.signals["gallery_excluded_reason"] = image_exclusions[image_url]
                    candidate.signals.pop("gallery_path", None)
                    candidate.signals.pop("gallery_evidence_source", None)
                elif image_url in image_gallery_context and not candidate.signals.get("gallery_excluded_reason"):
                    candidate.signals["gallery_path"] = True
                    candidate.signals["gallery_evidence_source"] = "; ".join(dict.fromkeys(image_gallery_context[image_url]))
                if confirmed_age_gate:
                    candidate.signals["age_gate_confirmed"] = True
                candidates.append(candidate)
            if not candidates and not video_urls and soup.find("script"):
                source_ref.requires_review = True
                source_ref.review_reasons = list(dict.fromkeys([*source_ref.review_reasons, ReviewReason.DYNAMIC_PAGE]))
                return CollectionResult(manual_review_reasons=[ReviewReason.DYNAMIC_PAGE])
            candidates.sort(key=lambda candidate: _image_priority(candidate.image_url))
            return CollectionResult(candidates=candidates[:context.max_candidates], video_candidates=_video_candidates(news_item, source_ref, video_urls, page_title))
        except Exception as exc:
            return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT, news_id=news_item.id, code="adapter_error", message=str(exc).strip() or f"图片证据解析失败（{type(exc).__name__}）。", source_url=source_ref.url, retryable=True)])


class SteamAdapter(OfficialHtmlAdapter):
    """Steam pages use the same HTML metadata plus screenshot links."""


class DynamicPageAdapter(OfficialHtmlAdapter):
    """Metadata-only adapter for JavaScript pages; always requests review."""

    def collect(self, news_item, source_ref, context: CollectionContext) -> CollectionResult:
        result = super().collect(news_item, source_ref, context)
        source_ref.requires_review = True
        source_ref.review_reasons = list(dict.fromkeys([*source_ref.review_reasons, ReviewReason.DYNAMIC_PAGE]))
        result.manual_review_reasons = list(dict.fromkeys([*result.manual_review_reasons, ReviewReason.DYNAMIC_PAGE]))
        return result
