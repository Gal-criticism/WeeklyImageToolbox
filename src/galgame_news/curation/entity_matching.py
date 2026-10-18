"""Conservative game/entity matching for image candidates.

The matcher intentionally returns ``None`` when the page does not contain
enough context.  Callers can retain such candidates for editorial review
without risking that a random CDN image is selected automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from ..domain import ImageCandidate, NewsItem, SourceType
from ..discovery.goods_context import is_goods_news
from .image_typing import _official_news_linked_gallery as _linked_gallery_evidence, _product_card_evidence


@dataclass(frozen=True)
class EntityMatchResult:
    matched: bool | None
    confidence: float
    matched_entities: tuple[str, ...] = ()
    conflicting_entities: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()
    official_domain_match: bool = False


def _compact(value: object) -> str:
    """Normalize Unicode and remove spacing/decorative punctuation.

    Keeping only letters and numbers makes full/half-width, quote and bracket
    variants compare identically while preserving digits and subtitle words.
    """

    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(char for char in text if unicodedata.category(char)[0] in {"L", "N"})


def _domain(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold().rstrip(".")


def _source_key(value: str) -> tuple[str, str, str, str]:
    parts = urlsplit(value.strip())
    return (parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), parts.query)


def _work_aliases(names: list[str]) -> list[str]:
    """Include the parent work when a news item names a particular chapter."""
    aliases = list(names)
    for name in names:
        chapter = r"(?:chapter|ch\.?)[\s:：-]*[0-9０-９]+\b"
        parents = (
            re.split(rf"\s*{chapter}", name, maxsplit=1, flags=re.I)[0],
            re.sub(rf"^\s*{chapter}\s*", "", name, flags=re.I),
        )
        for parent in parents:
            parent = parent.strip(" ＊*:-：")
            if parent and parent != name and len(_compact(parent)) >= 4:
                aliases.append(parent)
    return list(dict.fromkeys(aliases))


def _publisher_home(path: str) -> bool:
    return path.casefold().rstrip("/") in {"", "/", "/index.html", "/index.htm", "/home", "/home.html"}


def _official_news_linked_gallery(news: NewsItem, candidate: ImageCandidate) -> bool:
    """Recognize a gallery reached from this news item's own official source."""
    return _linked_gallery_evidence(news, candidate)


def _app_ids(values: list[str]) -> set[str]:
    found: set[str] = set()
    for value in values:
        found.update(re.findall(r"(?:/app/|/steam/apps/|steam_app_id[=:])([0-9]{3,})", value, re.I))
    return found


def _official_news_linked_goods(news: NewsItem, candidate: ImageCandidate) -> bool:
    """Bind product-card evidence to an official navigation path from this news.

    A publisher may sell goods for many works.  The page title by itself is
    never enough: the candidate must come from a product card on a page reached
    through this news item's explicitly linked official goods/shop/event route.
    """
    if candidate.source_type is not SourceType.OFFICIAL_SITE:
        return False
    root_url = candidate.signals.get("root_source_url") or candidate.news_source_url
    if not isinstance(root_url, str) or not any(_source_key(root_url) == _source_key(url) for url in news.source_urls):
        return False
    goods_context = news.event_type.value == "goods" or bool(re.search(r"周边|周邊|商品|グッズ|通贩|通販|复刻|復刻|goods|merchandise", news.title + " " + news.body, re.I))
    if not goods_context:
        return False
    if candidate.signals.get("navigation_kind") not in {"goods", "shop", "event"}:
        # A product card on the news's explicitly supplied catalogue is also
        # a direct association; it need not first navigate to its detail page.
        def same_explicit_page(value):
            a, b = urlsplit(value), urlsplit(candidate.source_url)
            return ((a.hostname or "").removeprefix("www.") == (b.hostname or "").removeprefix("www.")
                    and a.path.rstrip("/") == b.path.rstrip("/") and a.query == b.query)
        if not any(same_explicit_page(url) for url in news.source_urls):
            return False
        if not re.search(r"/(?:item|goods|shop|store|catalog|product)(?:s)?(?:/|[._-])", urlsplit(candidate.source_url).path, re.I):
            return False
    return _product_card_evidence(candidate)


class EntityMatcher:
    """Match a candidate to a news item's game using page-local evidence."""

    _TEXT_SIGNAL_KEYS = {
        "page_title", "title", "alt", "nearby_text", "caption", "description",
        "game_slug", "entity", "game_name", "brand", "original_url", "tweet_text",
    }
    _NUMERIC_EVIDENCE = ("game_match", "organization_match", "page_match", "event_match", "character_match")
    _NON_GAME_MEDIA_MARKERS = (
        "tv guide", "where to watch", "/movies/", "/movie/", "/anime/",
        "streaming", "episode guide", "film review", "/folklore", "folklore",
    )
    _GAME_CONTEXT_MARKERS = (
        "visual novel", "galgame", "steam", "pc game", "game official",
        "ゲーム", "ギャラリー", "公式サイト", "game cg",
    )
    _GENERIC_SOURCE_SLUGS = {"shop", "store", "product", "products", "item", "items", "detail", "goods"}
    _GENERIC_IMAGE_STEMS = {
        "image", "img", "photo", "picture", "visual", "keyvisual", "cover",
        "banner", "logo", "thumbnail", "thumb", "goods", "product", "item",
        "main", "sub", "detail", "intro", "sample", "screenshot", "screen",
    }

    @classmethod
    def _non_game_media_conflict(cls, candidate: ImageCandidate, page_values: list[str]) -> bool:
        """Reject obvious film/TV pages that merely share the game's title."""

        if candidate.source_type is not SourceType.UNVERIFIED:
            return False
        context = " ".join([candidate.source_url, *page_values]).casefold()
        return (
            any(marker in context for marker in cls._NON_GAME_MEDIA_MARKERS)
            and not any(marker in context for marker in cls._GAME_CONTEXT_MARKERS)
        )

    @classmethod
    def _conflicting_product_image_slug(cls, candidate: ImageCandidate) -> str | None:
        """Find named assets for a different product embedded in a store page.

        Product pages commonly include recommendation carousels. Page-level
        title evidence must not be inherited by clearly named image files for
        another product. Numeric, hashed and generic asset names remain
        undecided and continue through the normal image/type filters.
        """

        if candidate.source_type is not SourceType.UNVERIFIED:
            return None
        source = urlsplit(candidate.source_url)
        source_segments = [unquote(part) for part in source.path.split("/") if part]
        if not any(part.casefold() in {"shop", "store", "product", "products", "goods"} for part in source_segments):
            return None
        product_slug = next(
            (
                part for part in reversed(source_segments)
                if part.casefold() not in cls._GENERIC_SOURCE_SLUGS
                and not part.isdigit()
                and len(_compact(part)) >= 4
            ),
            None,
        )
        if not product_slug:
            return None

        image_path = unquote(urlsplit(candidate.image_url).path)
        filename = image_path.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0]
        compact_stem = _compact(stem)
        compact_product = _compact(product_slug)
        compact_image_path = _compact(image_path)
        if (
            not compact_stem
            or compact_stem.isdigit()
            or compact_stem in cls._GENERIC_IMAGE_STEMS
            or re.fullmatch(r"[0-9a-f]{16,}", compact_stem)
            or not any(char.isalpha() for char in compact_stem)
        ):
            return None
        if compact_product in compact_image_path or compact_stem in compact_product:
            return None
        return stem

    def match(self, news: NewsItem, candidate: ImageCandidate) -> EntityMatchResult:
        linked_official_gallery = _official_news_linked_gallery(news, candidate)
        linked_official_work = _linked_gallery_evidence(news, candidate, require_gallery=False)
        linked_official_goods = _official_news_linked_goods(news, candidate)
        aliases = [str(name) for name in news.game_names if str(name).strip()]
        # A title often contains the canonical game name even when an older
        # parser did not populate game_names.
        title_aliases = re.findall(r"《([^》]+)》|\[([^\]]+)\]", news.title)
        aliases.extend(next((part for part in pair if part), "") for pair in title_aliases)
        aliases = _work_aliases(list(dict.fromkeys(name for name in aliases if name)))

        url_values = [candidate.image_url, candidate.source_url]
        text_values: list[str] = list(url_values)
        page_values: list[str] = []
        for key, value in candidate.signals.items():
            if key in self._TEXT_SIGNAL_KEYS:
                text_values.append(str(value))
                page_values.append(str(value))
        for value in (candidate.news_source_url, candidate.parent_source_url, candidate.image_alt, candidate.nearby_text):
            if value:
                text_values.append(value)
                page_values.append(value)
        evidence = " ".join(unquote(value) for value in text_values)
        compact_evidence = _compact(evidence)
        page_text = _compact(" ".join(page_values))

        # Explicit parser evidence is stronger than a filename heuristic.
        supporting: list[str] = []
        for key in self._NUMERIC_EVIDENCE:
            value = candidate.signals.get(key)
            if isinstance(value, (int, float)) and float(value) >= 0.8:
                supporting.append(key)

        news_domains = {_domain(url) for url in news.source_urls if _domain(url)}
        candidate_domain = _domain(candidate.source_url)
        official_domain_match = bool(candidate_domain and any(
            candidate_domain == domain or candidate_domain.endswith("." + domain) or domain.endswith("." + candidate_domain)
            for domain in news_domains
        ))
        if official_domain_match:
            supporting.append("official_domain_match")

        # Steam IDs are an unambiguous conflict signal.
        news_ids = _app_ids(list(news.source_urls) + [str(value) for value in news.keywords])
        candidate_ids = _app_ids(url_values + [f"{key}={value}" for key, value in candidate.signals.items()])
        if news_ids and candidate_ids and news_ids.isdisjoint(candidate_ids):
            return EntityMatchResult(False, 1.0, conflicting_entities=tuple(sorted(candidate_ids)), supporting_signals=("steam_app_id_mismatch",), official_domain_match=official_domain_match)
        if news_ids and candidate_ids and not news_ids.isdisjoint(candidate_ids):
            return EntityMatchResult(True, 0.98, supporting_signals=("steam_app_id_match",), official_domain_match=official_domain_match)

        if candidate.source_type is SourceType.DIRECT_IMAGE and any(
            _source_key(candidate.source_url) == _source_key(url) for url in news.source_urls
        ):
            return EntityMatchResult(True, 0.9, supporting_signals=("image_linked_from_news_source",), official_domain_match=official_domain_match)

        conflicts: list[str] = []
        # entity_conflict is a derived result written by ImageCurator; feeding
        # it back into a later curation pass makes old false positives sticky.
        for key in ("conflicting_entity", "other_game_name", "other_title"):
            value = candidate.signals.get(key)
            if value not in (None, False, "", 0):
                conflicts.append(str(value) if value is not True else key)
        goods_context = candidate.signals.get("goods_context") is True and is_goods_news(news)
        goods_assoc = candidate.signals.get("goods_association")
        if goods_context and conflicts:
            return EntityMatchResult(False, 1.0, conflicting_entities=tuple(dict.fromkeys(conflicts)), supporting_signals=("explicit_goods_entity_conflict",), official_domain_match=official_domain_match)
        from ..discovery.goods_context import has_item_evidence
        goods_evidence = has_item_evidence(candidate)
        if goods_context and goods_assoc == "matched" and goods_evidence:
            return EntityMatchResult(True, 0.96, matched_entities=(str(candidate.signals.get("goods_item_id")),), supporting_signals=("goods_item_match",), official_domain_match=official_domain_match)
        if goods_context and goods_assoc == "matched":
            return EntityMatchResult(None, 0.0, supporting_signals=("goods_item_evidence_missing",), official_domain_match=official_domain_match)
        if goods_context and goods_assoc == "conflict":
            return EntityMatchResult(False, 1.0, conflicting_entities=(str(candidate.signals.get("goods_association_reason") or "goods_association_conflict"),), supporting_signals=("goods_association_conflict",), official_domain_match=official_domain_match)
        if goods_context and goods_assoc == "uncertain":
            return EntityMatchResult(None, 0.0, supporting_signals=("goods_association_uncertain",), official_domain_match=official_domain_match)
        if candidate.signals.get("brand_match") is False:
            conflicts.append("brand_mismatch")
        host_text = _compact(candidate_domain)
        # A branded domain containing a distinctive numbered title is strong
        # evidence of a different work when the target entity is absent.
        target_text = " ".join(_compact(alias) for alias in aliases)
        if "9nineproject" in host_text and "9nine" not in target_text:
            conflicts.append("9-nine")
        if self._non_game_media_conflict(candidate, page_values):
            conflicts.append("non_game_media_page")
        conflicting_slug = self._conflicting_product_image_slug(candidate)
        if conflicting_slug:
            conflicts.append(f"other_product_asset:{conflicting_slug}")
        # A third-party page title that clearly names another work is a
        # contradiction, not merely missing evidence.  Generic words such as
        # "CG" or "image" are deliberately ignored.
        page_title = str(candidate.signals.get("page_title") or candidate.signals.get("title") or "")
        title_has_work = any(_compact(alias) in _compact(page_title) for alias in aliases if _compact(alias))
        product_path = bool(re.search(r"/(?:products?|games?|titles?|works?)/", urlsplit(candidate.source_url).path, re.I))
        page_is_publisher_home = _publisher_home(urlsplit(candidate.source_url).path)
        brand_boilerplate = bool(re.search(r"official\s*web\s*site|公式サイト", page_title, re.I))
        # A publisher's homepage title names the brand, not a competing game.
        # A distinct titled work page can contradict the news work; image alt
        # text and navigation labels are not page titles.
        title_can_conflict = (
            candidate.source_type is not SourceType.OFFICIAL_SITE
            or product_path
            or (not page_is_publisher_home and not brand_boilerplate)
        )
        if page_title and not (linked_official_gallery or linked_official_work or linked_official_goods) and not title_has_work and title_can_conflict:
            title_tokens = re.findall(r"[A-Z][A-Za-z0-9]{2,}|\d[\w-]{2,}|[\u3040-\u30ff\u4e00-\u9fff]{2,}", page_title)
            if title_tokens:
                generic = {"CG", "GAME", "IMAGE", "SCREENSHOT", "OFFICIAL", "GALLERY", "GRAPHIC", "WEB", "SITE", "CHAPTER", "公式", "公式サイト", "官方网站", "画像", "画像一覧", "画像公開", "ギャラリー", "ゲーム", "作品", "トップ", "ニュース", "新着"}
                other = [token for token in title_tokens if token.casefold() not in {word.casefold() for word in generic}]
                if candidate.source_type is SourceType.OFFICIAL_SITE:
                    publisher = {_compact(name) for name in news.organizations}
                    other = [token for token in other if _compact(token) not in publisher]
                if other:
                    conflicts.extend(other)
        if conflicts:
            return EntityMatchResult(False, 1.0, conflicting_entities=tuple(dict.fromkeys(conflicts)), supporting_signals=tuple(dict.fromkeys(supporting)), official_domain_match=official_domain_match)

        if linked_official_goods:
            return EntityMatchResult(
                True,
                0.88,
                supporting_signals=("official_news_linked_goods", "product_card_evidence", "official_source_chain"),
                official_domain_match=official_domain_match,
            )

        root_path = urlsplit(str(candidate.signals.get("root_source_url") or "")).path
        if linked_official_gallery and (title_has_work or not _publisher_home(root_path)):
            return EntityMatchResult(
                True,
                0.92,
                supporting_signals=("official_news_linked_gallery", "official_domain_match"),
                official_domain_match=official_domain_match,
            )
        matched: list[str] = []
        for alias in aliases:
            normalized = _compact(alias)
            if not normalized:
                continue
            if normalized in compact_evidence:
                # Very short names are common words and need page/brand or
                # official-domain context before they can match.
                is_short = (normalized.isascii() and len(normalized) <= 3) or (not normalized.isascii() and len(normalized) <= 2)
                contextual = official_domain_match or bool(page_text and normalized in page_text) or bool(supporting)
                if not is_short or contextual:
                    matched.append(alias)

        if matched:
            supporting.append("entity_name_in_context")
            confidence = 0.95 if official_domain_match else 0.86
            if page_text and any(_compact(alias) in page_text for alias in matched):
                supporting.append("page_context")
                confidence = min(0.98, confidence + 0.04)
            return EntityMatchResult(True, confidence, tuple(matched), supporting_signals=tuple(dict.fromkeys(supporting)), official_domain_match=official_domain_match)

        if any(key in supporting for key in ("game_match", "organization_match", "page_match", "event_match")):
            return EntityMatchResult(True, 0.9, supporting_signals=tuple(dict.fromkeys([*supporting, "explicit_entity_signal"])), official_domain_match=official_domain_match)
        if linked_official_work and not _publisher_home(root_path):
            return EntityMatchResult(
                True, 0.9, supporting_signals=("official_news_linked_work", "official_domain_match"),
                official_domain_match=official_domain_match,
            )
        # A shared official domain is provenance only. It cannot establish
        # that this particular image belongs to the news item's game.
        if candidate.source_type is SourceType.OFFICIAL_SITE and candidate_domain and not supporting:
            compact_aliases = [_compact(alias) for alias in aliases]
            if any(((alias.isascii() and len(alias) <= 3) or (not alias.isascii() and len(alias) <= 2)) and alias in compact_evidence for alias in compact_aliases):
                return EntityMatchResult(None, 0.0, supporting_signals=("short_name_needs_context",), official_domain_match=official_domain_match)
            return EntityMatchResult(None, 0.0, supporting_signals=("official_source_without_name",), official_domain_match=official_domain_match)
        # VNDB screenshots are retained as a trusted-database fallback for
        # legacy issues; they remain lower-trust and are still subject to the
        # image-type gate and review metadata.
        if candidate.source_type is SourceType.UNVERIFIED and candidate_domain.endswith(("vndb.org", "vndb.net")):
            return EntityMatchResult(True, 0.82, supporting_signals=("trusted_database_context",), official_domain_match=False)
        if supporting:
            # Numeric parser evidence is useful, but without a named entity it
            # is not enough to call a third-party image a safe match.
            return EntityMatchResult(None, 0.0, supporting_signals=tuple(dict.fromkeys([*supporting, "no_entity_name"])), official_domain_match=official_domain_match)
        return EntityMatchResult(None, 0.0, supporting_signals=("no_entity_context",), official_domain_match=official_domain_match)


__all__ = ["EntityMatchResult", "EntityMatcher"]
