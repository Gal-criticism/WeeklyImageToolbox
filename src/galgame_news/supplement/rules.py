"""Explainable metadata triage. Pixel cleanliness always needs human review."""
from __future__ import annotations
import re
import unicodedata
from ..config import ImageTypeConfig
from ..curation.image_typing import ImageTypeClassifier


def _norm(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text)).casefold())


def _versions(text):
    return set(re.findall(r"after\s*episode\s*\d*|アフターエピソード\s*\d*|remake|リメイク|重[制製]|(?:^|\s)fd(?:\s|$)|(?:episode|season|ver(?:sion)?)\s*\d+", str(text).casefold()))


def classify_candidate(news, candidate):
    signals = candidate.signals
    page_title = str(signals.get("supplement_page_title") or candidate.nearby_text or "")
    names = [n for n in news.game_names if len(_norm(n)) >= 2]
    matched = any(_norm(n) in _norm(page_title) for n in names)
    bound = signals.get("localization_binding") == "confirmed"
    version_conflict = bool(_versions(page_title) - _versions(" ".join(names)))
    if version_conflict:
        relation = "version_uncertain"
    elif not (matched or bound):
        relation = "unrelated"
    else:
        # A generic gallery is not evidence of an event, lottery, or goods announcement.
        direct = (candidate.source_url in news.source_urls and _norm(news.title) in _norm(page_title))
        relation = "direct" if direct else "general"
    kind = ImageTypeClassifier(ImageTypeConfig()).classify(news, candidate).image_type.value
    invalid = relation == "unrelated" or bool(re.search(r"(?:avatar|favicon|site-logo|footer|qrcode)", candidate.image_url, re.I))
    return {"supplement_relation": relation, "supplement_image_type": kind,
            "supplement_quality": "invalid" if invalid else "needs_review",
            "reasons": "图片内容、UI及水印需要人工审核" + ("；作品版本待核" if version_conflict else "")}
