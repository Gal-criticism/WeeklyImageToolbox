"""Conservative news and URL identity helpers for the z-only goods path."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from ..delivery.helpers import section_label, section_prefix


def compact(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def is_goods_news(news) -> bool:
    if section_prefix(section_label(news)) != "z":
        return False
    text = " ".join((news.title, news.body, *news.keywords))
    return news.event_type.value == "goods" or bool(re.search(
        r"周边|周邊|商品|手办|手辦|フィギュア|原型|灰模|グッズ|再販|通贩|通販|抽奖|抽選|くじ|"
        r"联动|聯動|コラボ|快闪|快閃|ポップアップ|popup|ASMR|音声作品|merchandise", text, re.I))


def unwrap_goods_url(url: str) -> str:
    """Decode known tracking destinations, retaining the original as provenance.

    This only parses strings; the shared HTTP client validates every destination.
    Arbitrary query parameters are deliberately not treated as redirects.
    """
    current = url.strip()
    for _ in range(3):
        try:
            parts = urlsplit(current)
            host = (parts.hostname or "").casefold()
            if (parts.scheme in {"http", "https"} and not parts.username and not parts.password
                    and host.removeprefix("www.") in {"amazon.co.jp", "amazon.jp"}):
                asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parts.path, re.I)
                if asin:
                    return f"https://www.amazon.co.jp/dp/{asin[1].upper()}"
            if "/age_check/" in parts.path:
                break
            if not (host == "rcv.ixd.dmm.com" or host.endswith(".dmm.com") or host.endswith(".dmm.co.jp")):
                break
            query = parse_qs(parts.query)
            target = next((query[k][0] for k in ("lurl", "rurl", "redirect_url") if query.get(k)), "")
            if urlsplit(target).scheme not in {"http", "https"} or target == current:
                break
            current = target
        except ValueError:
            break
    return current


def item_identity(url: str) -> str:
    parts = urlsplit(unwrap_goods_url(url))
    for pattern in (r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)",
                    r"/products/detail/(\d+)", r"/(?:product_id/)?([RV]J\d+)",
                    r"/(posts|products)/(\d+)", r"/kuji/detail/([^/]+)", r"/cid=([^/]+)"):
        match = re.search(pattern, parts.path, re.I)
        if match:
            return match.group(match.lastindex).casefold()
    query = parse_qs(parts.query)
    return next((query[key][0].casefold() for key in ("id", "product_id", "asin") if query.get(key)), "")


def has_item_evidence(candidate) -> bool:
    """A scoped titled item can establish identity without a numeric SKU."""
    identity = candidate.signals.get("goods_item_id")
    title = candidate.signals.get("goods_title")
    return any(e.role == "goods" and (
        bool(identity and e.item_id == identity)
        or bool(not identity and title and e.product_title == title and e.container)
    ) for e in candidate.evidence)


def association(news, *, url: str, title: str, work="", maker="", activity="", root="", item_id="", require_specific=False) -> tuple[str, str]:
    """A store or brand alone never establishes the product's relationship."""
    local = compact(" ".join((title, work, activity)))
    explicit = [unwrap_goods_url(u) for u in news.source_urls]
    identity = (item_id or item_identity(url)).casefold()
    from .goods_pages import goods_site
    site = goods_site(url)
    def same_store(u):
        return goods_site(u) == site if site else (urlsplit(u).hostname or "").removeprefix("www.") == (urlsplit(url).hostname or "").removeprefix("www.")
    same_host_ids = {item_identity(u) for u in explicit if item_identity(u) and same_store(u)}
    if same_host_ids and identity and identity not in same_host_ids:
        return "conflict", "商品编号与新闻明确链接不同"
    if identity and identity in same_host_ids:
        return "matched", "新闻明确商品编号与详情一致"
    names = list(news.game_names)
    names.extend(re.findall(r"[《「『]([^》」』]+)[》」』]", news.title + " " + news.body))
    news_text = compact(news.title + " " + news.body)
    work_match = any(len(compact(n)) >= 3 and compact(n) in local for n in names)
    # Named characters and specific product/event keywords also discriminate
    # a figure or a multi-work collaboration from the store's other products.
    specifics = [p for p in news.people + news.keywords if len(compact(p)) >= 3
                 and not re.fullmatch(r"周边|商品|グッズ|手办|フィギュア|ASMR|联动|抽奖", p, re.I)]
    specific_match = any(compact(p) in local for p in specifics)
    event_match = bool(activity and len(compact(activity)) >= 4 and compact(activity) in news_text)
    if require_specific and (work_match or specific_match or event_match):
        # Public search can return an old game or another product of the same
        # work. Product/activity identity must survive the search expansion.
        news_description = news.title + " " + news.body
        requirements = (
            (r"手办|手辦|フィギュア|figure", r"フィギュア|figure|PVC|完成品|スケール|1/\d"),
            (r"抽奖|抽選|くじ|lottery", r"くじ|抽選|lottery"),
            (r"ASMR|音声作品", r"ASMR|音声|ボイス"),
            (r"咖啡|カフェ|cafe", r"カフェ|cafe|コラボ"),
            (r"快闪|快閃|ポップアップ|popup", r"ポップアップ|popup|フェア|pop.up"),
        )
        if any(re.search(wanted, news_description, re.I) and not re.search(proof, title + " " + activity, re.I)
               for wanted, proof in requirements):
            return "uncertain", "补搜结果仅匹配作品，缺少新闻所述商品种类或活动证据"
        anniversary = re.search(r"(\d+)\s*周年", news_description)
        if anniversary and not re.search(re.escape(anniversary[1]) + r"\s*周年", title + " " + activity):
            return "uncertain", "补搜结果缺少本次周年活动证据"
    if work_match or specific_match or event_match:
        return "matched", "商品局部标题、作品或活动与新闻一致"
    if work and names and all(compact(n) not in compact(work) and compact(work) not in compact(n) for n in names):
        return "conflict", "商品明确所属作品与新闻不符"
    return "uncertain", "缺少商品、角色或活动的对应证据；商城或厂牌身份不足以确认"


def cached_product_urls(cache, news) -> list[tuple[str, str]]:
    """Only examine responses already present for this news's explicit posts."""
    found = []
    for original in news.source_urls:
        parts = urlsplit(original)
        if (parts.hostname or "").removeprefix("www.") not in {"x.com", "twitter.com"}:
            continue
        match = re.search(r"/status/(\d+)", parts.path)
        payload = cache.get(match.group(1)) if match else None
        if not isinstance(payload, dict):
            continue
        # Restrict to URL entities; do not follow media, avatars or quoted posts.
        containers = [payload, payload.get("data", {})]
        for container in containers:
            if not isinstance(container, dict):
                continue
            urls = container.get("entities", {}).get("urls", []) if isinstance(container.get("entities"), dict) else []
            for entry in urls:
                if not isinstance(entry, dict):
                    continue
                target = entry.get("unwound_url") or entry.get("expanded_url")
                if isinstance(target, str) and urlsplit(target).scheme in {"http", "https"}:
                    pair = (unwrap_goods_url(target), original)
                    if pair not in found:
                        found.append(pair)
    return found
