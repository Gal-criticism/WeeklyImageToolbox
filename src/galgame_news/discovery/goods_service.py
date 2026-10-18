"""Product collection boundary shared by desktop, CLI and review additions.

Known stores use item-scoped parsers rather than the generic page-wide image
scan. All I/O goes through the task's existing safe public HTTP client.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from ..domain import (CollectionResult, FailureRecord, FailureStage, ImageEvidence,
                      ReviewReason)
from .adapters.common import _candidate
from .adapters.html import (OfficialHtmlAdapter, _explicit_age_confirmation_cookie,
                           _is_age_gate)
from .goods_context import association, is_goods_news, item_identity, unwrap_goods_url
from .goods_pages import goods_site, parse_goods_page
from ..pipeline.contracts import CancellationRequested


def goods_page_text(response) -> str:
    """Respect legacy store encodings even when only the HTML declares them."""
    content = getattr(response, "content", b"")
    if isinstance(content, bytes) and content:
        match = re.search(rb"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content[:8192], re.I)
        if match:
            try:
                return content.decode(match.group(1).decode("ascii"), "replace")
            except (LookupError, UnicodeError):
                pass
    return response.text


def explicit_goods_age_links(soup, page_url):
    from urllib.parse import urljoin
    found = []
    for anchor in soup.select("#yes a[href], a#yes[href], a#enter-yes[href], a[href*='/age_check/']"):
        target = urljoin(page_url, str(anchor["href"]))
        affirmative = re.search(r"declared=yes(?:/|[?&]|$)", target, re.I) or anchor.get("id") in {"yes", "enter-yes"} or anchor.find_parent(id="yes")
        if affirmative and urlsplit(target).hostname == urlsplit(page_url).hostname:
            found.append((target, "明确年龄确认入口", "age_confirmation"))
    return list(dict.fromkeys(found))


def page_problem(html: str, url: str, status: int = 200) -> tuple[str, str, bool] | None:
    if not 200 <= status < 300:
        code = "goods_access_denied" if status in {401, 403} else "goods_page_missing" if status in {404, 410} else "goods_http_error"
        return code, f"商品来源返回 HTTP {status}，其他来源继续采集。", status in {408, 429} or status >= 500
    # A successful HTTP status may be a regional restriction or a soft 404.
    text = BeautifulSoup(html[:1_000_000], "html.parser").get_text(" ", strip=True)
    if "not-available-in-your-region" in url or re.search(r"not available in your (?:region|country)|お住まいの地域から|このサービスはお客様の地域", text, re.I):
        return "goods_region_restricted", "商品页面存在地区限制，保留链接并继续其他来源。", False
    if re.search(r"robot check|automated access|ロボットではない|アクセスが制限|captcha", text[:5000], re.I):
        return "goods_access_denied", "商品来源要求访问验证，未取得公开商品内容。", False
    return None


class GoodsSourceService:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def supports(news, source) -> bool:
        return is_goods_news(news) and bool(goods_site(unwrap_goods_url(source.url)))

    def collect(self, news, source, context):
        try:
            response = self.client.get(unwrap_goods_url(source.url))
            page_url = str(response.url)
            html = goods_page_text(response)
            problem = page_problem(html, page_url, response.status_code)
            if problem:
                return self._failure(news, source.url, *problem)
            soup = BeautifulSoup(html, "html.parser")
            cookie = _explicit_age_confirmation_cookie(soup, html)
            if cookie:
                response = self.client.get(page_url, cookies={"PermitRate": cookie})
                html, page_url = goods_page_text(response), str(response.url)
                problem = page_problem(html, page_url, response.status_code)
                if problem:
                    return self._failure(news, source.url, *problem)
            return self.parse(news, source, context, html, page_url=page_url)
        except CancellationRequested:
            raise
        except Exception as exc:
            # Never persist arbitrary exception text containing URLs/credentials.
            name = type(exc).__name__
            timeout = "Timeout" in name
            return self._failure(news, source.url,
                "goods_timeout" if timeout else "goods_connection_failed",
                f"商品来源{'连接超时' if timeout else '获取失败'}（{name}），保留链接并继续其他来源。", True)

    @staticmethod
    def _failure(news, url, code, message, retryable=False):
        return CollectionResult(failures=[FailureRecord(stage=FailureStage.COLLECT,
            news_id=news.id, source_url=url, code=code, message=message, retryable=retryable)])

    def parse(self, news, source, context, html, *, page_url=None, method="dom"):
        page_url = page_url or source.url
        problem = page_problem(html, page_url)
        if problem:
            return self._failure(news, source.url, *problem)
        soup = BeautifulSoup(html, "html.parser")
        if _is_age_gate(soup, html) or explicit_goods_age_links(soup, page_url):
            return CollectionResult(manual_review_reasons=[ReviewReason.AGE_GATE], failures=[
                FailureRecord(stage=FailureStage.COLLECT, news_id=news.id, source_url=source.url,
                    code="goods_age_confirmation", message="商品页面需要明确年龄确认，未将确认页当作商品。", retryable=False)])
        page = parse_goods_page(html, page_url)
        result = CollectionResult()
        for image in page.images[:context.max_candidates]:
            parts = urlsplit(image.url)
            if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
                continue
            identity = item_identity(page_url) or image.item_id
            seller = image.seller
            if not seller and goods_site(page_url) == "amazon_jp":
                seller = parse_qs(urlsplit(source.root_url or source.url).query).get("me", [""])[0]
            binding, reason = association(news, url=page_url, title=image.title,
                work=image.work, maker=image.maker, activity=image.activity,
                item_id=identity, root=source.root_url or source.url,
                require_specific=source.navigation_kind == "goods_search")
            candidate = _candidate(news, image.url, source, context)
            candidate.parent_source_url = page_url
            candidate.image_alt = image.title
            candidate.nearby_text = image.text
            chain = list(dict.fromkeys(u for u in [*(source.source_chain or [source.root_url or source.url]), source.parent_url, page_url] if u))
            candidate.evidence.append(ImageEvidence(page_url=page_url, container=image.container,
                item_id=identity, role="goods", text=image.text.strip() or image.title,
                method=method, relationship="商品项明确图片资源", variant_of=image.thumbnail_url or None,
                product_title=image.title, seller=seller, work=image.work, maker=image.maker,
                activity=image.activity, image_role=image.role, stage=image.stage, source_chain=chain))
            candidate.signals.update(goods_context=True, goods_item_id=identity,
                goods_title=image.title, goods_role=image.role, goods_site=goods_site(page_url),
                goods_seller=seller, goods_stage=image.stage,
                goods_association=binding, goods_association_reason=reason,
                goods_original=image.original, goods_reference=goods_site(page_url) == "figsoku-b" or image.role == "reference",
                page_title=page.title, source_chain=" → ".join(chain))
            if binding != "matched":
                candidate.review_reasons.append(ReviewReason.UNCERTAIN_MATCH)
            result.candidates.append(candidate)
        if not page.images and not page.links:
            result.failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                code="goods_images_not_found", message="未发现可确认的公开商品图片或详情链接，保留来源待复核。",
                source_url=source.url, retryable=False))
        return result

    @staticmethod
    def needs_render(html, result, page_url=""):
        page = parse_goods_page(html, page_url)
        soup = BeautifulSoup(html, "html.parser")
        return page.dynamic and not page_problem(html, page_url) and not (_is_age_gate(soup, html) or explicit_goods_age_links(soup, page_url))

    @staticmethod
    def links(news, source, html, page_url):
        """Only relevant item links on this store; no recommendation expansion."""
        soup = BeautifulSoup(html, "html.parser")
        age_links = explicit_goods_age_links(soup, page_url)
        if age_links:
            return age_links
        page = parse_goods_page(html, page_url)
        ranked = []
        base_host = (urlsplit(page_url).hostname or "").removeprefix("www.")
        for link in page.links:
            target = unwrap_goods_url(link.url)
            parts = urlsplit(target)
            if parts.scheme not in {"http", "https"} or parts.username or parts.password:
                continue
            if (parts.hostname or "").removeprefix("www.") != base_host:
                continue
            if link.kind == "listing_page":
                ranked.append((target, link.title, "goods_listing"))
                continue
            binding, _ = association(news, url=target, title=link.title,
                item_id=link.item_id, root=source.root_url or source.url,
                require_specific=source.navigation_kind == "goods_search")
            if binding != "matched":
                continue
            ranked.append((target, link.title, "goods"))
        return list(dict.fromkeys(ranked))
