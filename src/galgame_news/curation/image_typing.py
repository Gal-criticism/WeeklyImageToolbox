"""Deterministic image typing and news-specific image acceptance policy."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from ..config import ImageTypeConfig
from ..domain import EventType, ImageCandidate, ImageEvidence, ImageNeed, ImageType, NewsItem
from ..discovery.goods_context import is_goods_news


@dataclass(frozen=True)
class ImageTypeResult:
    image_type: ImageType
    confidence: float
    supporting_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageTypeDecision:
    accepted: bool
    fallback_only: bool = False
    rejection_reason: str | None = None
    requires_review: bool = False
    auto_select: bool = True
    type_match: float = 0.0


class ImageRequirement(str, Enum):
    CG = "cg"
    ANNOUNCEMENT = "announcement"
    GOODS = "goods"
    RELEASE = "release"
    GENERIC = "generic"
    UNKNOWN = "unknown"


_LOGO_TERMS = ("logo", "brand-logo", "site-logo", "logo_mark")
_BANNER_TERMS = ("banner", "bnr", "header", "footer", "mainvisual-banner", "campaign-banner")
_UI_TERMS = ("button", "btn", "icon", "arrow", "menu", "navigation", "loading", "spinner", "favicon", "steam_share_image")
_GOODS_TERMS = (
    "goods", "shop", "store", "product", "item", "tokuten", "tapestry", "acrylic", "badge", "standee", "merchandise",
    "stand", "figure", "フィギュア", "手办", "手辦", "特典", "商品", "周边", "周邊",
)
_COVER_TERMS = ("cover", "package", "jacket", "box", "pkg", "capsule")
_KEY_VISUAL_TERMS = ("keyvisual", "key_visual", "mainvisual", "main_visual", "kv", "visual")
_CHARACTER_TERMS = ("character", "chara", "standing", "sprite", "tachie", "立绘", "立繪")
_SCREENSHOT_TERMS = ("screenshot", "screenshots", "screen", "sample", "play", "ingame", "in_game")
_PHOTO_TERMS = ("photo", "photograph", "realphoto", "cosplay", "照片", "写真", "实拍", "實拍")
_ANNOUNCEMENT_TERMS = ("announcement", "commemorative", "celebration", "anniversary", "贺图", "賀圖", "纪念图", "紀念圖", "应援图", "應援圖", "倒计时图", "倒計時圖")
_TYPE_MATCH_SCORES = {
    ImageRequirement.CG: {ImageType.GAME_CG: 1.0, ImageType.GAMEPLAY_SCREENSHOT: 0.8, ImageType.KEY_VISUAL: 0.4, ImageType.BACKGROUND_ART: 0.6, ImageType.ANNOUNCEMENT_ART: 0.25, ImageType.UNKNOWN: 0.1},
    ImageRequirement.ANNOUNCEMENT: {ImageType.ANNOUNCEMENT_ART: 1.0, ImageType.KEY_VISUAL: 0.8, ImageType.GAME_CG: 0.35, ImageType.GAMEPLAY_SCREENSHOT: 0.25, ImageType.UNKNOWN: 0.1},
    ImageRequirement.GOODS: {ImageType.GOODS: 1.0, ImageType.ANNOUNCEMENT_ART: 0.7, ImageType.KEY_VISUAL: 0.5, ImageType.GAME_CG: 0.25, ImageType.GAMEPLAY_SCREENSHOT: 0.2, ImageType.UNKNOWN: 0.1},
    ImageRequirement.RELEASE: {ImageType.GAME_CG: 1.0, ImageType.GAMEPLAY_SCREENSHOT: 0.85, ImageType.KEY_VISUAL: 0.65, ImageType.COVER: 0.55, ImageType.BACKGROUND_ART: 0.7, ImageType.UNKNOWN: 0.1},
    ImageRequirement.GENERIC: {ImageType.GAME_CG: 0.9, ImageType.GAMEPLAY_SCREENSHOT: 0.8, ImageType.ANNOUNCEMENT_ART: 0.7, ImageType.KEY_VISUAL: 0.6, ImageType.BACKGROUND_ART: 0.7, ImageType.UNKNOWN: 0.1},
    ImageRequirement.UNKNOWN: {ImageType.GAME_CG: 0.9, ImageType.GAMEPLAY_SCREENSHOT: 0.8, ImageType.ANNOUNCEMENT_ART: 0.7, ImageType.KEY_VISUAL: 0.6, ImageType.BACKGROUND_ART: 0.7, ImageType.UNKNOWN: 0.1},
}


_LOCAL_ROLES = {
    "game_cg": ImageType.GAME_CG,
    "background_art": ImageType.BACKGROUND_ART,
    "decorative": ImageType.DECORATIVE,
    "goods": ImageType.GOODS,
    "logo": ImageType.LOGO,
    "character": ImageType.CHARACTER_ART,
    "character_art": ImageType.CHARACTER_ART,
    "cover": ImageType.COVER,
    "key_visual": ImageType.KEY_VISUAL,
    "announcement_art": ImageType.ANNOUNCEMENT_ART,
    "screenshot": ImageType.GAMEPLAY_SCREENSHOT,
    "gameplay_screenshot": ImageType.GAMEPLAY_SCREENSHOT,
    "banner": ImageType.BANNER,
    "ui": ImageType.UI,
}


def _same_page(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (a.scheme.casefold(), a.netloc.casefold(), a.path.rstrip("/"), a.query) == (b.scheme.casefold(), b.netloc.casefold(), b.path.rstrip("/"), b.query)


def _local_role_evidence(candidate: ImageCandidate) -> list[tuple[ImageEvidence, ImageType]]:
    """Return recognized image-local roles from the candidate's own source page."""
    values = []
    for evidence in candidate.evidence:
        role = _LOCAL_ROLES.get(_norm(evidence.role).strip())
        if role is None or evidence.method not in {"dom", "script", "browser"}:
            continue
        if not _same_page(evidence.page_url, candidate.source_url):
            continue
        values.append((evidence, role))
    return values


def _product_card_evidence(candidate: ImageCandidate) -> bool:
    for evidence, role in _local_role_evidence(candidate):
        if role is not ImageType.GOODS:
            continue
        local = _norm(" ".join((evidence.container, evidence.relationship, evidence.text)))
        if evidence.item_id or re.search(r"product|goods|item|catalog|card|商品|周边|周邊|グッズ", local):
            return True
    return False


def _norm(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _has_term(text: str, term: str) -> bool:
    term = _norm(term)
    if re.fullmatch(r"[a-z0-9_-]+", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?=$|[^a-z0-9]|\d)", text) is not None
    return term in text


def _any_term(text: str, terms: tuple[str, ...]) -> str | None:
    for term in terms:
        if _has_term(text, term):
            return term
    return None


def _signal_text(candidate: ImageCandidate, *, image_local: bool = False) -> str:
    values: list[str] = []
    for url in ((candidate.image_url,) if image_local else (candidate.image_url, candidate.source_url)):
        parts = urlsplit(url)
        path = parts.path
        segments = [segment for segment in path.split("/") if segment]
        # Japanese game sites commonly use /product/{brand}/{title}/ as the
        # route for every product page. That route prefix is not evidence that
        # each image linked from the page is merchandise.
        if (
            candidate.source_type.value == "official_site"
            and len(segments) >= 3
            and segments[0].casefold() in {"product", "products"}
        ):
            path = "/" + "/".join(segments[1:])
        values.extend((path, parts.query))
    generated = {
        "image_type_supporting_signals", "type_match", "auto_select", "fallback_only",
        "entity_match", "entity_match_confidence", "entity_match_evidence",
        "entity_conflict", "official_domain_match", "source_tier", "source_is_official",
        "source_linked", "candidate_status", "duplicate_of", "duplicate_kind", "possible_duplicate_of",
        "invalid_reason", "quality_eligible", "root_source_url", "parent_source_url",
        "source_chain_alternates", "conversion_error", "sharpness_score",
        "visible_detail_score", "animated_source", "animation_frame_count",
        "quality_analysis_failed",
        "gallery_evidence_source", "gallery_evidence_from", "media_variant_of",
        "gallery_excluded_reason", "gallery_recovery_original",
        "navigation_kind", "navigation_text",
    }
    for key, value in candidate.signals.items():
        if key in generated or key.startswith("visual_shadow_"):
            continue
        if image_local and key in {"page_title", "title", "description", "brand", "game_name", "entity", "source_discovery"}:
            continue
        values.append(str(key))
        if isinstance(value, str):
            values.append(value)
    values.extend(value for value in (candidate.image_alt, candidate.nearby_text) if value)
    return _norm(" ".join(values))


def _news_text(news: NewsItem) -> str:
    return _norm(" ".join([news.title, news.body, *news.keywords]))


def _aspect(candidate: ImageCandidate) -> float | None:
    if not candidate.width or not candidate.height:
        return None
    return candidate.width / candidate.height


def _official_news_linked_gallery(news: NewsItem, candidate: ImageCandidate, *, require_gallery: bool = True) -> bool:
    """Require a news-linked work page and image-local gallery evidence."""
    if candidate.source_type.value != "official_site" or (require_gallery and candidate.signals.get("gallery_path") is not True):
        return False
    root_url = candidate.signals.get("root_source_url")
    if not isinstance(root_url, str):
        return False
    root = urlsplit(root_url)
    if not any(
        (parts := urlsplit(value)).scheme.casefold() == root.scheme.casefold()
        and parts.netloc.casefold() == root.netloc.casefold()
        and parts.path.rstrip("/") == root.path.rstrip("/")
        and parts.query == root.query
        for value in news.source_urls
    ):
        return False
    page_host = (urlsplit(candidate.source_url).hostname or "").casefold().rstrip(".")
    root_host = (root.hostname or "").casefold().rstrip(".")
    same_site = bool(page_host and root_host and (
        page_host == root_host or page_host.endswith("." + root_host) or root_host.endswith("." + page_host)
    ))
    if not same_site:
        return False
    if candidate.signals.get("gallery_excluded_reason"):
        return False
    # A publisher homepage can link to many works. Its gallery marker alone
    # must not promote all of those images; a reached page must name this work.
    title = _norm(str(candidate.signals.get("page_title") or candidate.signals.get("title") or ""))
    names = [str(name) for name in news.game_names]
    for name in list(names):
        chapter = r"(?:chapter|ch\.?)[\s:：-]*[0-9０-９]+\b"
        names.append(re.split(rf"\s*{chapter}", name, maxsplit=1, flags=re.I)[0])
        names.append(re.sub(rf"^\s*{chapter}\s*", "", name, flags=re.I))
    named_work = any(len(_norm(name)) >= 4 and _norm(name) in title for name in names)
    root_path = root.path.rstrip("/")
    if root_path.casefold() in {"", "/index.html", "/index.htm", "/home", "/home.html"}:
        return named_work
    # A publisher can link between products: same host and a traceable root
    # alone cannot bind an unrelated product's gallery to this work.
    work_path = re.sub(r"/(?:index|home)\.html?$", "", root_path, flags=re.I)
    page_path = urlsplit(candidate.source_url).path.rstrip("/")
    if page_path.startswith(work_path + "/"):
        nested = page_path[len(work_path) + 1:].split("/")
        if any(part.casefold() in {"related", "recommend", "recommendation", "recommendations", "products", "product", "game", "games", "work", "works", "title", "titles"} for part in nested):
            return False
    if not (page_path == work_path or page_path.startswith(work_path + "/") or named_work):
        return False
    return True


_CG_NEWS_RE = re.compile(
    r"CG\s*(?:公开|公開|更新|追加|新增)|新\s*CG|(?:特殊)?(?:场景|場景|事件|イベント|シーン)\s*CG|"
    r"画面公开|畫面公開|游戏截图公开|遊戲截圖公開|gallery\s*更新|ギャラリー更新",
    re.I,
)


class ImageTypeClassifier:
    def __init__(self, config: ImageTypeConfig | None = None):
        self.config = config or ImageTypeConfig()

    def _result(self, image_type: ImageType, confidence: float, signals: tuple[str, ...]) -> ImageTypeResult:
        if confidence < self.config.minimum_type_confidence:
            return ImageTypeResult(ImageType.UNKNOWN, 0.0, ("below_minimum_confidence",))
        return ImageTypeResult(image_type, min(1.0, max(0.0, confidence)), signals)

    def _game_cg_result(self, gallery: bool, explicit_cg: bool, similar_gallery: bool, horizontal_scene: bool, cg_filename: bool, linked_news_gallery: bool = False, strong_cg_evidence: bool = False) -> ImageTypeResult:
        signals = ["gallery_page" if gallery else "cg_filename"]
        if linked_news_gallery:
            signals.append("official_news_linked_gallery")
        if explicit_cg:
            signals.append("explicit_cg")
        if similar_gallery:
            signals.append("similar_gallery_images")
        if horizontal_scene:
            signals.append("horizontal_scene")
        if strong_cg_evidence:
            signals.append("news_linked_or_named_cg_evidence")
        # File names, gallery routes and aspect ratio describe a likely image
        # type; they do not establish identity. High confidence requires an
        # explicit parser signal or the news-linked gallery plus an explicit
        # CG announcement.
        confidence = 0.92 if strong_cg_evidence else (0.68 if gallery or cg_filename or similar_gallery or horizontal_scene else 0.0)
        return self._result(ImageType.GAME_CG, confidence, tuple(signals))

    def classify(self, news: NewsItem, candidate: ImageCandidate) -> ImageTypeResult:
        text = _signal_text(candidate)
        local_text = _signal_text(candidate, image_local=True)
        news_text = _news_text(news)
        aspect = _aspect(candidate)
        linked_news_gallery = _official_news_linked_gallery(news, candidate)

        # Service-verified GOODS item roles are local item evidence. Keep this
        # ahead of filename/gallery heuristics so covers and ASMR jackets can
        # never be promoted to game CG by a broad page context.
        if candidate.signals.get("goods_context") is True and is_goods_news(news):
            item_id = candidate.signals.get("goods_item_id")
            from ..discovery.goods_context import has_item_evidence
            goods_evidence = has_item_evidence(candidate)
            explicit_priority_roles = {ImageType.LOGO, ImageType.DECORATIVE, ImageType.CHARACTER_ART}
            explicit_local_roles = {role for _, role in _local_role_evidence(candidate)}
            if goods_evidence and not (explicit_priority_roles & explicit_local_roles):
                return self._result(ImageType.GOODS, 0.96, ("goods_item_evidence",))

        # Explicit UI assets must win even when they come from an official page.
        if _has_term(local_text, "steam_share_image"):
            return self._result(ImageType.UI, 1.0, ("steam_share_image",))
        image_stem = urlsplit(candidate.image_url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
        if re.search(r"^(?:ogp|og[_-]?image|share[_-]?image|twitter[_-]?card)(?:$|[_-])", image_stem) or candidate.signals.get("social_share_image") is True:
            return self._result(ImageType.UI, 0.98, ("social_share_image",))
        # A parser's item-specific DOM/script role is local evidence.  Resolve
        # it before generic gallery, sample, or page-title language can blur
        # the type.  Backgrounds need a traceable official news relationship;
        # a filename alone is deliberately not accepted for that role.
        local_roles = [role for _, role in _local_role_evidence(candidate)]
        if ImageType.DECORATIVE in local_roles:
            return self._result(ImageType.DECORATIVE, 0.98, ("image_local_role:decorative",))
        # Old records may have an overly broad gallery role. An explicit
        # image alt still describes the image itself, not its parent section.
        own_alt = _norm(candidate.image_alt or "")
        for pattern, image_type in ((r"\blogo\b|ロゴ|标志",ImageType.LOGO),
                                    (r"\bbanner\b|バナー|横幅",ImageType.BANNER),
                                    (r"standing\s+(?:sprite|art)|立绘|立繪",ImageType.CHARACTER_ART)):
            if re.search(pattern,own_alt):
                return self._result(image_type,0.95,("explicit_image_alt_role",))
        for image_type in (ImageType.LOGO, ImageType.CHARACTER_ART, ImageType.COVER, ImageType.BANNER, ImageType.UI):
            if image_type in local_roles:
                return self._result(image_type, 0.95, (f"image_local_role:{image_type.value}",))
        if ImageType.GOODS in local_roles and _product_card_evidence(candidate):
            return self._result(ImageType.GOODS, 0.96, ("image_local_role:goods_product_card",))
        if ImageType.BACKGROUND_ART in local_roles:
            if candidate.source_type.value == "official_site" and _official_news_linked_gallery(news, candidate, require_gallery=False):
                return self._result(ImageType.BACKGROUND_ART, 0.92, ("image_local_role:background_art", "official_news_linked_source"))
            return ImageTypeResult(ImageType.UNKNOWN, 0.0, ("background_art_source_unlinked",))
        for image_type in (ImageType.KEY_VISUAL, ImageType.ANNOUNCEMENT_ART, ImageType.GAMEPLAY_SCREENSHOT):
            if image_type in local_roles:
                return self._result(image_type, 0.95, (f"image_local_role:{image_type.value}",))
        if ImageType.GAME_CG in local_roles:
            confidence = 0.92 if linked_news_gallery else 0.68
            return self._result(ImageType.GAME_CG, confidence, ("image_local_role:game_cg",))

        ui_term = _any_term(local_text, _UI_TERMS)
        if ui_term:
            return self._result(ImageType.UI, 0.98, (f"ui:{ui_term}",))

        logo_term = _any_term(local_text, _LOGO_TERMS)
        if logo_term:
            return self._result(ImageType.LOGO, 0.98, (f"logo:{logo_term}",))

        banner_term = _any_term(local_text, _BANNER_TERMS)
        extreme_aspect = aspect is not None and (aspect >= self.config.banner_aspect_ratio or aspect <= 1 / self.config.banner_aspect_ratio)
        explicit_non_banner_semantics = bool(_any_term(text, _GOODS_TERMS + _COVER_TERMS + _CHARACTER_TERMS + _KEY_VISUAL_TERMS + _SCREENSHOT_TERMS + _PHOTO_TERMS + _ANNOUNCEMENT_TERMS)) or bool(re.search(r"gallery|ギャラリー|ギャラリ|(?:^|[/_.-])(?:cg|ev|event|scene)(?:[_-]?\d+|cg)?(?:[/_.-]|$)", text))
        if banner_term or (extreme_aspect and not explicit_non_banner_semantics):
            signals: list[str] = []
            if banner_term:
                signals.append(f"banner:{banner_term}")
            if extreme_aspect:
                signals.append(f"banner_aspect:{aspect:.3f}")
            return self._result(ImageType.BANNER, 0.94 + 0.04 * bool(extreme_aspect), tuple(signals))

        steam_screenshot = candidate.source_type.value == "steam" and bool(re.search(r"screenshots?|(?:^|[/_.-])ss(?:[_-]?\d+)?(?:[/_.-]|$)", text))
        if steam_screenshot:
            return self._result(ImageType.GAMEPLAY_SCREENSHOT, 0.93, ("steam_screenshot",))

        goods_terms = tuple(term for term in _GOODS_TERMS if not (candidate.source_type.value == "steam" and term in {"store", "item"}))
        goods_term = _any_term(local_text, goods_terms)
        if goods_term is None and candidate.source_type.value != "official_site":
            goods_term = _any_term(urlsplit(candidate.source_url).path.casefold(), goods_terms)
        if goods_term:
            content_text = _norm(" ".join(filter(None,(candidate.image_alt,candidate.nearby_text))))
            local_goods_term = _any_term(content_text, goods_terms)
            confidence = 0.68 if candidate.source_type.value == "official_site" and not local_goods_term else 0.96
            return self._result(ImageType.GOODS, confidence, (f"goods:{goods_term}", "local_goods_text" if local_goods_term else "auxiliary_goods_hint"))

        cover_term = _any_term(local_text, _COVER_TERMS)
        if cover_term:
            signals = [f"cover:{cover_term}"]
            if aspect is not None and aspect < 1 / self.config.character_aspect_ratio:
                signals.append(f"cover_portrait:{aspect:.3f}")
            return self._result(ImageType.COVER, 0.91 + 0.05 * (len(signals) - 1), tuple(signals))

        # Explicit screenshot labels outrank broad gallery routes and aspect
        # ratio; a bare "sample" still does not satisfy this rule.
        screenshot_terms = tuple(term for term in _SCREENSHOT_TERMS if term not in {"sample", "play"})
        screenshot_term = _any_term(text, screenshot_terms)
        if screenshot_term:
            return self._result(ImageType.GAMEPLAY_SCREENSHOT, 0.93, (f"screenshot:{screenshot_term}",))

        # A quiz/countdown promotion on a broad publisher page is not a CG,
        # even if its CSS contains a generic class such as campaign__graphic.
        image_stem = urlsplit(candidate.image_url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
        if re.search(r"(?:^|[_-])(?:quiz|countdown)(?:$|[_-])", image_stem):
            return self._result(ImageType.ANNOUNCEMENT_ART, 0.92, ("promotion_filename",))
        if re.search(r"^(?:character|chara|standing|sprite|tachie)[_-]?[0-9]+(?:$|[_-])", image_stem):
            return self._result(ImageType.CHARACTER_ART, 0.93, ("numbered_character_asset",))

        announcement_term = _any_term(text, _ANNOUNCEMENT_TERMS)
        gift_news = bool(re.search(r"贺图|賀圖|纪念图|紀念圖|应援图|應援圖|倒计时图|倒計時圖|周年|週年|anniversary|commemorative|celebration", news_text))
        if announcement_term and (gift_news or "announcement" in text):
            return self._result(ImageType.ANNOUNCEMENT_ART, 0.94, (f"announcement:{announcement_term}", "gift_news"))

        gallery = bool(re.search(r"gallery|ギャラリー|ギャラリ", text)) or bool(candidate.signals.get("gallery")) or bool(candidate.signals.get("gallery_path"))
        cg_filename = bool(re.search(r"(?:^|[/_.-])(?:cg|ev|event|scene)(?:[_-]?\d+|cg)?(?:[/_.-]|$)", text))
        explicit_cg = bool(candidate.signals.get("cg_match")) or bool(re.search(r"event.?cg|イベントcg", text))
        parser_cg_match = isinstance(candidate.signals.get("cg_match"), (int, float)) and float(candidate.signals.get("cg_match", 0.0)) >= 0.8
        page_title = str(candidate.signals.get("page_title", ""))
        image_local_text = " ".join(value for value in (candidate.image_alt, candidate.nearby_text) if value)
        image_local_cg = bool(re.search(r"(?:event\s*)?cg|イベントcg", image_local_text, re.I))
        named_page_cg = bool(
            re.search(r"(?:event\s*)?cg|イベントcg|game\s*cg", page_title, re.I)
            and any(_norm(alias) in _norm(page_title) for alias in news.game_names if alias)
        )
        strong_cg_evidence = parser_cg_match or linked_news_gallery or named_page_cg or image_local_cg
        similar_gallery = any(
            isinstance(candidate.signals.get(key), (int, float)) and float(candidate.signals[key]) >= self.config.minimum_gallery_group_size
            for key in ("gallery_image_count", "gallery_group_size", "similar_gallery_images")
        )
        horizontal_scene = aspect is not None and aspect >= self.config.scene_aspect_ratio
        is_game_cg = (
            image_local_cg
            or parser_cg_match
            or (gallery and (cg_filename or explicit_cg or similar_gallery or horizontal_scene or linked_news_gallery))
            or (cg_filename and not _any_term(text, _LOGO_TERMS + _BANNER_TERMS + _GOODS_TERMS))
        )
        if candidate.signals.get("gallery_excluded_reason"):
            is_game_cg = False
        if is_game_cg and not _any_term(local_text, _CHARACTER_TERMS + _KEY_VISUAL_TERMS):
            return self._game_cg_result(gallery, explicit_cg, similar_gallery, horizontal_scene, cg_filename, linked_news_gallery, strong_cg_evidence)

        character_term = _any_term(local_text, _CHARACTER_TERMS)
        if character_term:
            signals = [f"character:{character_term}"]
            if aspect is not None and aspect <= 1 / self.config.character_aspect_ratio:
                signals.append(f"character_aspect:{aspect:.3f}")
            if candidate.signals.get("transparent") is True:
                signals.append("transparent_background")
            return self._result(ImageType.CHARACTER_ART, 0.9 + 0.03 * (len(signals) - 1), tuple(signals))

        key_visual_term = _any_term(local_text, _KEY_VISUAL_TERMS)
        if key_visual_term:
            return self._result(ImageType.KEY_VISUAL, 0.92, (f"key_visual:{key_visual_term}",))

        if is_game_cg:
            return self._game_cg_result(gallery, explicit_cg, similar_gallery, horizontal_scene, cg_filename, linked_news_gallery)

        # "sample" is a common product gallery label, not proof of a gameplay
        # screenshot.  Keep explicit screenshot terminology and Steam's own
        # screenshot URLs as the positive indicators.
        screenshot_terms = tuple(term for term in _SCREENSHOT_TERMS if term not in {"sample", "play"})
        screenshot_term = _any_term(text, screenshot_terms)
        if steam_screenshot or screenshot_term:
            return self._result(ImageType.GAMEPLAY_SCREENSHOT, 0.93, ("steam_screenshot" if steam_screenshot else f"screenshot:{screenshot_term}",))

        photo_term = _any_term(text, _PHOTO_TERMS)
        if photo_term:
            return self._result(ImageType.PHOTO, 0.9, (f"photo:{photo_term}",))

        # Anime-like names, officiality, and aspect ratio alone are not enough.
        return ImageTypeResult(ImageType.UNKNOWN, 0.0, ())


class ImageRequirementPolicy:
    def __init__(self, config: ImageTypeConfig | None = None):
        self.config = config or ImageTypeConfig()

    @staticmethod
    def _requirements(news: NewsItem) -> tuple[ImageRequirement, ...]:
        text = _news_text(news)
        found: list[ImageRequirement] = []
        if re.search(r"贺图|賀圖|纪念图|紀念圖|应援图|應援圖|倒计时图|倒計時圖|周年|週年|commemorative|celebration|anniversary", text):
            found.append(ImageRequirement.ANNOUNCEMENT)
        if news.event_type is EventType.GOODS or re.search(r"周边|周邊|商品|グッズ|goods|shop|merchandise", text, re.I):
            found.append(ImageRequirement.GOODS)
        if _CG_NEWS_RE.search(text):
            found.append(ImageRequirement.CG)
        if news.event_type is EventType.RELEASE or re.search(r"发售|発売|预约|預約|登陆steam|登录steam|上架steam|steam上线|steam上線", text, re.I):
            found.append(ImageRequirement.RELEASE)
        if news.image_need is ImageNeed.EXPLICIT_NEW_IMAGE and not found:
            found.append(ImageRequirement.GENERIC)
        return tuple(dict.fromkeys(found or [ImageRequirement.UNKNOWN]))

    @classmethod
    def _requirement(cls, news: NewsItem) -> ImageRequirement:
        return cls._requirements(news)[0]

    def evaluate(self, news: NewsItem, result: ImageTypeResult) -> ImageTypeDecision:
        requirements = self._requirements(news)
        rejected_by = [requirement.value for requirement in requirements if result.image_type.value in set(self.config.rejected_image_types_by_requirement.get(requirement.value, ()))]
        # A release date mentioned in a CG update must not reopen the gate
        # for character art or product images rejected by the explicit CG need.
        if ImageRequirement.CG in requirements and ImageRequirement.CG.value in rejected_by:
            return ImageTypeDecision(False, rejection_reason=f"{result.image_type.value} is not suitable for cg image news", type_match=0.0)
        if len(rejected_by) == len(requirements):
            return ImageTypeDecision(False, rejection_reason=f"{result.image_type.value} is not suitable for {', '.join(rejected_by)} image news", type_match=0.0)
        type_match = self.type_match(news, result.image_type)
        fallback_only = ImageRequirement.CG in requirements and result.image_type is ImageType.KEY_VISUAL
        requires_review = (result.image_type is ImageType.UNKNOWN and self.config.unknown_requires_review) or result.confidence < 0.8
        auto_select = True
        if result.image_type is ImageType.UNKNOWN or result.confidence < 0.8:
            # Unknown material remains visible for editorial review, but is
            # never allowed to satisfy an explicit CG/image requirement.
            auto_select = ImageRequirement.CG not in requirements and self.config.auto_select_unknown
        if result.image_type is ImageType.DECORATIVE:
            auto_select = False
        return ImageTypeDecision(True, fallback_only=fallback_only, requires_review=requires_review, auto_select=auto_select, type_match=type_match)

    def type_match(self, news: NewsItem, image_type: ImageType) -> float:
        return max(_TYPE_MATCH_SCORES[requirement].get(image_type, 0.25) for requirement in self._requirements(news))

    def is_fallback_only(self, news: NewsItem, image_type: ImageType) -> bool:
        return ImageRequirement.CG in self._requirements(news) and image_type is ImageType.KEY_VISUAL
