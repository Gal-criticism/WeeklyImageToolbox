"""Centralized source trust tiers used by curation and ranking."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ..domain import ImageCandidate, SourceType


@dataclass(frozen=True)
class SourceTrustResult:
    tier: str
    officiality: float
    is_official: bool
    requires_review: bool = False


class SourceTrustPolicy:
    """Classify provenance without promoting a CDN or image proxy."""

    _third_party = {
        "cbrimages.com", "3dmgame.com", "amazon.com", "www.amazon.com",
        "media-amazon.com", "images-na.ssl-images-amazon.com", "kun", "kunimg.com",
    }
    _trusted_db = {"vndb.org", "vndb.net", "vgmdb.net"}
    _aggregators = {"fandom.com", "wikipedia.org", "myanimelist.net", "bangumi.tv", "game8.jp"}

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").casefold().rstrip(".")

    @classmethod
    def _is_host_or_subdomain(cls, host: str, names: set[str]) -> bool:
        return any(host == name or host.endswith("." + name) for name in names if "." in name)

    def classify(self, candidate: ImageCandidate) -> SourceTrustResult:
        host = self._host(candidate.source_url)
        image_host = self._host(candidate.image_url)
        if candidate.signals.get("goods_context") is True:
            role = str(candidate.signals.get("goods_role") or "").casefold()
            reference = candidate.signals.get("goods_reference") is True or role == "reference"
            from ..discovery.goods_context import has_item_evidence
            evidence = has_item_evidence(candidate)
            association_ok = candidate.signals.get("goods_association") == "matched" and evidence
            merchant_sites = {"eeo", "getchu", "amazon_jp", "gamers", "dmm", "fantia", "dlsite"}
            merchant = candidate.signals.get("goods_site") in merchant_sites
            if reference:
                return SourceTrustResult("third_party_news", 0.4, False, True)
            if merchant and association_ok:
                return SourceTrustResult("official_store", 0.86, True)
        if candidate.source_type is SourceType.OFFICIAL_X:
            return SourceTrustResult("official_event_page", 0.88, True)
        if candidate.source_type is SourceType.STEAM:
            return SourceTrustResult("steam", 0.82, True)
        if candidate.source_type is SourceType.OFFICIAL_SITE:
            path = (urlsplit(candidate.source_url).path or "").casefold()
            if any(token in path for token in ("/event", "/news", "/announce", "/update")):
                return SourceTrustResult("official_event_page", 0.98, True)
            if any(token in path for token in ("/brand", "/company", "/about")):
                return SourceTrustResult("official_brand_page", 0.9, True)
            if any(token in path for token in ("/store", "/shop", "/goods")):
                return SourceTrustResult("official_store", 0.86, True)
            return SourceTrustResult("official_game_page", 0.95, True)
        if candidate.source_type is SourceType.DIRECT_IMAGE:
            if candidate.signals.get("source_page_official") is True:
                return SourceTrustResult("official_game_page", 0.9, True)
            return SourceTrustResult("image_proxy", 0.3, False, True)
        if self._is_host_or_subdomain(host, self._third_party) or "kun" in host:
            tier = "image_proxy" if "amazon" in host or "image" in host or host == "kun" else "third_party_news"
            return SourceTrustResult(tier, 0.18, False, True)
        if self._is_host_or_subdomain(host, self._trusted_db):
            return SourceTrustResult("trusted_database", 0.42, False, True)
        if self._is_host_or_subdomain(host, self._aggregators):
            return SourceTrustResult("aggregator", 0.12, False, True)
        if host.startswith(("cdn.", "static.", "images.", "img.", "media.")) or image_host != host:
            return SourceTrustResult("image_proxy", 0.25, False, True)
        if host:
            return SourceTrustResult("unknown", 0.1, False, True)
        return SourceTrustResult("unknown", 0.0, False, True)


__all__ = ["SourceTrustPolicy", "SourceTrustResult"]
