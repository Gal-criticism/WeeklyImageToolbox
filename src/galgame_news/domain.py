"""Stable, validated data contracts shared by all prescan modules."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StringEnum(str, Enum):
    @classmethod
    def _missing_(cls, value: object):
        return cls.UNKNOWN


class EventType(_StringEnum):
    NEW_TITLE = "new_title"
    RELEASE = "release"
    DEMO = "demo"
    UPDATE = "update"
    EVENT = "event"
    GOODS = "goods"
    LOCALIZATION = "localization"
    UNKNOWN = "unknown"


class VideoStatus(_StringEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SourceType(_StringEnum):
    OFFICIAL_SITE = "official_site"
    OFFICIAL_X = "official_x"
    STEAM = "steam"
    DIRECT_IMAGE = "direct_image"
    VIDEO = "video"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class ImageType(_StringEnum):
    GAME_CG = "game_cg"
    BACKGROUND_ART = "background_art"
    DECORATIVE = "decorative"
    GAMEPLAY_SCREENSHOT = "gameplay_screenshot"
    KEY_VISUAL = "key_visual"
    CHARACTER_ART = "character_art"
    COVER = "cover"
    ANNOUNCEMENT_ART = "announcement_art"
    GOODS = "goods"
    LOGO = "logo"
    BANNER = "banner"
    UI = "ui"
    PHOTO = "photo"
    UNKNOWN = "unknown"


class ImageCurationStatus(_StringEnum):
    SELECTED = "selected"
    UNSELECTED = "unselected"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class ImageNeed(_StringEnum):
    EXPLICIT_NEW_IMAGE = "explicit_new_image"
    EVENT_IMAGE = "event_image"
    GENERIC_EDITORIAL = "generic_editorial"
    UNKNOWN = "unknown"


class DiscoveryMethod(_StringEnum):
    DOCUMENT = "document"
    HISTORY = "history"
    SAME_DOMAIN = "same_domain"
    BRAVE = "brave"
    DDGS = "ddgs"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class ReviewReason(_StringEnum):
    X_SOURCE = "x_source"
    AGE_GATE = "age_gate"
    DYNAMIC_PAGE = "dynamic_page"
    UNKNOWN_PUBLISH_TIME = "unknown_publish_time"
    UNCERTAIN_MATCH = "uncertain_match"
    CLOSE_SCORES = "close_scores"
    NETWORK_RESTRICTED = "network_restricted"
    FALLBACK_OLD_MATERIAL = "fallback_old_material"
    HISTORICAL_DUPLICATE = "historical_duplicate"
    ADULT_OR_UNKNOWN = "adult_or_unknown"
    IMAGE_TYPE_REVIEW = "image_type_review"
    IMAGE_QUALITY_REVIEW = "image_quality_review"


class FailureStage(_StringEnum):
    PARSE = "parse"
    ANALYZE = "analyze"
    RESOLVE = "resolve"
    COLLECT = "collect"
    DOWNLOAD = "download"
    CURATE = "curate"
    OUTPUT = "output"
    HISTORY = "history"
    UNKNOWN = "unknown"


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalized_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, parts.query, ""))


def news_id_for(issue_id: str, sequence: int, title: str) -> str:
    raw = f"{issue_id}\0{sequence}\0{_normalized_text(title)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def candidate_id_for(image_url: str, news_id: str | None = None) -> str:
    """Return a stable image identity scoped to its news item when available."""
    key = f"{news_id}\0{_normalized_url(image_url)}" if news_id else _normalized_url(image_url)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def video_candidate_id_for(video_url: str) -> str:
    """Return a stable id for a video URL after removing fragments."""
    return hashlib.sha256(_normalized_url(video_url).encode("utf-8")).hexdigest()[:16]


def video_id_for(video_url: str) -> str:
    """Backward-friendly alias for callers that use the shorter name."""
    return video_candidate_id_for(video_url)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class NewsDraft(ContractModel):
    sequence: int = Field(ge=1)
    section: str = Field(min_length=1)
    title: str = Field(min_length=1)
    author: str | None = None
    body: str
    source_urls: list[str] = Field(default_factory=list)


class IssueDraft(ContractModel):
    issue_id: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    published_at: datetime | None = None
    entries: list[NewsDraft] = Field(default_factory=list)

    _published_at_aware = field_validator("published_at")(_aware)


class LocalizationContext(ContractModel):
    """Explicit work identities discovered for one translation news item."""
    vndb_ids: list[str] = Field(default_factory=list)
    steam_app_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)


class LocalizationProvenance(ContractModel):
    source: str = ""
    work_id: str = ""
    binding: str = "uncertain"
    resolution: str = "unknown"
    screenshot: bool = False
    native: bool = False
    source_chain: list[str] = Field(default_factory=list)



class NewsItem(ContractModel):
    id: str | None = None
    issue_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    section: str = Field(min_length=1)
    title: str = Field(min_length=1)
    author: str | None = None
    body: str
    game_names: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    event_type: EventType = EventType.UNKNOWN
    event_at: datetime | None = None
    keywords: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.0, ge=0.0, le=1.0)
    image_need: ImageNeed = ImageNeed.UNKNOWN
    localization_context: LocalizationContext | None = None

    @model_validator(mode="after")
    def assign_stable_id(self) -> "NewsItem":
        object.__setattr__(self, "id", news_id_for(self.issue_id, self.sequence, self.title))
        return self

    _event_at_aware = field_validator("event_at")(_aware)


class Issue(ContractModel):
    schema_version: Literal[1] = Field(default=1, frozen=True)
    issue_id: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    published_at: datetime | None = None
    news_items: list[NewsItem] = Field(default_factory=list)

    _published_at_aware = field_validator("published_at")(_aware)


class SourceRef(ContractModel):
    url: str = Field(min_length=1)
    source_type: SourceType = SourceType.UNKNOWN
    domain: str = Field(min_length=1)
    discovered_via: DiscoveryMethod = DiscoveryMethod.UNKNOWN
    officiality: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_review: bool = False
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    root_url: str | None = None
    parent_url: str | None = None
    navigation_kind: str = ""
    navigation_text: str = ""
    navigation_depth: int = Field(default=0, ge=0)
    source_chain: list[str] = Field(default_factory=list)


class ScoreBreakdown(ContractModel):
    relevance: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)
    source_trust: float = Field(ge=0.0, le=1.0)
    quality: float = Field(ge=0.0, le=1.0)
    duplicate_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    type_match: float = Field(default=0.0, ge=0.0, le=1.0)
    total: float = Field(ge=0.0, le=100.0)


class ImageEvidence(ContractModel):
    """Image-local provenance; never a classifier's derived verdict."""

    page_url: str
    container: str = ""
    item_id: str = ""
    role: str = "unknown"
    text: str = ""
    method: str = "dom"
    relationship: str = "image attribute"
    variant_of: str | None = None
    product_title: str = ""
    seller: str = ""
    work: str = ""
    maker: str = ""
    activity: str = ""
    image_role: str = ""
    stage: str = ""
    source_chain: list[str] = Field(default_factory=list)


class ImageCandidate(ContractModel):
    localization_provenance: LocalizationProvenance | None = None
    id: str | None = None
    news_id: str = Field(min_length=1)
    image_url: str = Field(min_length=1)
    media_source_url: str | None = None
    downloaded_url: str | None = None
    expected_width: int | None = Field(default=None, ge=1)
    expected_height: int | None = Field(default=None, ge=1)
    download_status: Literal["pending", "downloaded", "retryable_failed", "permanent_failed"] = "pending"
    download_error_code: str | None = None
    source_url: str = Field(min_length=1)
    source_type: SourceType = SourceType.UNKNOWN
    image_type: ImageType = ImageType.UNKNOWN
    image_type_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    published_at: datetime | None = None
    fetched_at: datetime
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    mime_type: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    perceptual_hash: str | None = None
    downloadable: bool = False
    local_path: str | None = None
    original_path: str | None = None
    original_mime_type: str | None = None
    original_byte_size: int | None = Field(default=None, ge=0)
    original_sha256: str | None = None
    original_width: int | None = Field(default=None, ge=1)
    original_height: int | None = Field(default=None, ge=1)
    output_mime_type: str | None = None
    output_byte_size: int | None = Field(default=None, ge=0)
    output_sha256: str | None = None
    output_width: int | None = Field(default=None, ge=1)
    output_height: int | None = Field(default=None, ge=1)
    news_source_url: str | None = None
    parent_source_url: str | None = None
    image_alt: str | None = None
    evidence: list[ImageEvidence] = Field(default_factory=list)
    nearby_text: str | None = None
    selection_reasons: list[str] = Field(default_factory=list)
    animated_source: bool = False
    animation_frame_index: int | None = Field(default=None, ge=0)
    review_reasons: list[ReviewReason] = Field(default_factory=list)
    signals: dict[str, float | int | str | bool] = Field(default_factory=dict)
    score: ScoreBreakdown | None = None
    selected: bool = False
    curation_status: ImageCurationStatus = ImageCurationStatus.UNSELECTED

    @model_validator(mode="after")
    def assign_stable_id(self) -> "ImageCandidate":
        object.__setattr__(self, "id", candidate_id_for(self.image_url, self.news_id))
        if self.signals.get("invalid_reason") or self.curation_status is ImageCurationStatus.INVALID:
            object.__setattr__(self, "selected", False)
            object.__setattr__(self, "curation_status", ImageCurationStatus.INVALID)
        else:
            object.__setattr__(self, "curation_status", ImageCurationStatus.SELECTED if self.selected else ImageCurationStatus.UNSELECTED)
        return self

    _published_at_aware = field_validator("published_at")(_aware)
    _fetched_at_aware = field_validator("fetched_at")(_aware)


class VideoCandidate(ContractModel):
    id: str | None = None
    news_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    video_url: str = Field(min_length=1)
    title: str = ""
    uploader: str = ""
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    format: str | None = None
    mime_type: str | None = None
    byte_size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    local_path: str | None = None
    downloadable: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    status: VideoStatus = VideoStatus.DISCOVERED
    failure_reason: str | None = None
    discovered_via: DiscoveryMethod = DiscoveryMethod.UNKNOWN

    @model_validator(mode="after")
    def assign_stable_id(self) -> "VideoCandidate":
        object.__setattr__(self, "id", video_candidate_id_for(self.video_url))
        return self


class FailureRecord(ContractModel):
    stage: FailureStage = FailureStage.UNKNOWN
    news_id: str | None = None
    candidate_id: str | None = None
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_url: str | None = None
    retryable: bool = False
    occurrences: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _occurred_at_aware = field_validator("occurred_at")(_aware)


class HistoricalImage(ContractModel):
    image_id: str
    sha256: str | None = None
    perceptual_hash: str | None = None
    first_seen_issue: str
    last_seen_issue: str
    local_path: str | None = None


class SearchResult(ContractModel):
    url: str = Field(min_length=1)
    title: str = ""
    snippet: str = ""
    domain: str = ""


class CollectionContext(ContractModel):
    timeout_seconds: float = Field(default=12.0, gt=0)
    max_candidates: int = Field(default=20, ge=1)
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _now_aware = field_validator("now")(_aware)


class CollectionResult(ContractModel):
    candidates: list[ImageCandidate] = Field(default_factory=list)
    video_candidates: list[VideoCandidate] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    manual_review_reasons: list[ReviewReason] = Field(default_factory=list)


class NewsResult(ContractModel):
    news_item: NewsItem
    sources: list[SourceRef] = Field(default_factory=list)
    candidates: list[ImageCandidate] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _recorded_at_aware = field_validator("recorded_at")(_aware)


class CurationResult(ContractModel):
    candidates: list[ImageCandidate] = Field(default_factory=list)
    filtered_candidates: list[ImageCandidate] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    selection_shortfall: int = Field(default=0, ge=0)


class PipelineResult(ContractModel):
    schema_version: Literal[1] = Field(default=1, frozen=True)
    issue: Issue
    candidates: list[ImageCandidate] = Field(default_factory=list)
    videos: list[VideoCandidate] = Field(default_factory=list)
    filtered_candidates: list[ImageCandidate] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    review_required: list[ImageCandidate] = Field(default_factory=list)

    @property
    def all_candidates(self) -> list[ImageCandidate]:
        return [*self.candidates, *self.filtered_candidates]


class OutputManifest(ContractModel):
    schema_version: Literal[1] = Field(default=1, frozen=True)
    issue_id: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    image_count: int = Field(default=0, ge=0)
    files: list[str] = Field(default_factory=list)


class HistoryStore(Protocol):
    def known_image(self, sha256: str | None, perceptual_hash: str | None) -> HistoricalImage | None: ...

    def sources_for(self, news_item: NewsItem) -> list[SourceRef]: ...

    def record_news_result(self, result: NewsResult) -> None: ...
