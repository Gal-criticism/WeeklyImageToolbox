"""Conservative relevance checks for public search results and fetched pages."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


_GAME_CONTEXT = re.compile(
    r"(?<![A-Za-z0-9])game(?![A-Za-z0-9])|visual\s*novel|galgame|adventure\s*game|游戏官网|游戏|遊戲|视觉小说|視覺小說|"
    r"ノベルゲーム|ビジュアルノベル|アドベンチャーゲーム|ギャルゲー|美少女ゲーム|作品公式",
    re.I,
)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _contains(text: str, term: str) -> bool:
    term = unicodedata.normalize("NFKC", term).strip()
    if not term:
        return False
    if _CJK.search(term):
        # CJK names have no whitespace boundaries; require non-CJK edges to
        # avoid matching a short title inside a longer unrelated word.
        escaped = re.escape(term)
        return re.search(rf"(?<![\u3040-\u30ff\u3400-\u9fff]){escaped}(?![\u3040-\u30ff\u3400-\u9fff])", text, re.I) is not None
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.I) is not None


def _work_names(news) -> list[str]:
    return list(dict.fromkeys(name.strip() for name in news.game_names if name.strip()))


def _organization_names(news) -> list[str]:
    names = [name.strip() for name in news.organizations if name.strip()]
    # Minimal fallback for unparsed maker names: use distinctive uppercase
    # tokens explicitly present in the headline or its linked social handle.
    headline_matches = re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![A-Za-z0-9])", news.title)
    handles = []
    for source_url in news.source_urls:
        path = urlsplit(source_url).path
        handle = re.search(r"/(?:@)?([A-Za-z][A-Za-z0-9_-]{2,})/(?:status|posts?)/", path)
        if handle:
            maker = re.sub(r"[-_](?:official|staff|info|account)$", "", handle.group(1), flags=re.I)
            if re.search(r"[A-Z]", maker) or "-" in maker or "_" in maker:
                handles.append(maker.replace("_", "-"))
    for value in [*headline_matches, *handles]:
        if value.casefold() not in {name.casefold() for name in names}:
            names.append(value)
    return names


def matches_work(news, text: str) -> bool:
    """Return whether text contains a bounded, explicit work name."""
    return any(_contains(text, name) for name in _work_names(news))


def _primary_text(value: str) -> str:
    """Drop common related/recommendation tails before treating text as evidence."""
    return re.split(
        r"\b(?:recommended|related (?:videos?|articles?|games?)|you may also like|suggested(?: videos?)?)\b|"
        r"推荐|推薦|猜你喜欢|相關影片|関連記事|関連動画|おすすめ",
        value or "", maxsplit=1, flags=re.I,
    )[0]


def news_relevance(news, text: str, url: str = "") -> bool:
    """Check result/page evidence without mixing in the originating news copy.

    Distinctive full names stand on their own. Short names need maker identity
    or an explicit game/visual-novel context in the result itself.
    """
    text_evidence = _primary_text(text or "")
    evidence = f"{text_evidence} {url or ''}"
    found = [name for name in _work_names(news) if _contains(evidence, name)]
    if not found:
        return False
    for name in found:
        has_maker = any(_contains(text_evidence, org) for org in _organization_names(news))
        has_game_context = _GAME_CONTEXT.search(text_evidence) is not None
        if has_maker or has_game_context:
            return True
    return False

