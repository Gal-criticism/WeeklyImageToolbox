"""TOML-backed validation for curation and networking policy."""

from __future__ import annotations

import copy
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoringConfig(_ConfigModel):
    relevance: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)
    source_trust: float = Field(ge=0.0, le=1.0)
    quality: float = Field(ge=0.0, le=1.0)
    duplicate_penalty: float = Field(ge=0.0, le=1.0)
    risk_penalty: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScoringConfig":
        if abs((self.relevance + self.freshness + self.source_trust + self.quality) - 1.0) > 1e-6:
            raise ValueError("scoring weights must sum to 1")
        return self


class FilterConfig(_ConfigModel):
    min_width: int = Field(ge=1)
    min_height: int = Field(ge=1)
    min_pixels: int = Field(ge=1)
    max_response_bytes: int = Field(ge=1)
    max_image_bytes: int = Field(ge=1)


class NetworkConfig(_ConfigModel):
    timeout_seconds: float = Field(gt=0)
    search_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    news_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(ge=0, le=10)
    user_agent: str = Field(min_length=1)
    trust_env: bool = True
    page_concurrency: int = Field(default=4, ge=1, le=64)
    page_per_host: int = Field(default=2, ge=1, le=64)
    image_concurrency: int = Field(default=8, ge=1, le=64)
    image_per_host: int = Field(default=8, ge=1, le=64)
    image_processing_threads: int = Field(default=2, ge=1, le=16)
    socialdata_concurrency: int = Field(default=2, ge=1, le=16)


CONCURRENCY_FIELDS = frozenset({
    "search_timeout_seconds", "news_timeout_seconds",
    "page_concurrency", "page_per_host", "image_concurrency", "image_per_host",
    "image_processing_threads", "socialdata_concurrency", "trust_env",
})


class SelectionConfig(_ConfigModel):
    min_images: int = Field(ge=0)
    max_images: int = Field(ge=1)
    per_news_max: int = Field(ge=1)
    minimum_score: float = Field(ge=0.0, le=100.0)
    type_limits: dict[str, int] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_TYPE_LIMITS))

    @model_validator(mode="after")
    def type_limits_are_valid(self) -> "SelectionConfig":
        self.type_limits.setdefault("decorative", 0)
        if any(not isinstance(value, int) or value < 0 for value in self.type_limits.values()):
            raise ValueError("selection.type_limits values must be non-negative integers")
        return self

    @model_validator(mode="after")
    def limits_are_ordered(self) -> "SelectionConfig":
        if self.min_images > self.max_images:
            raise ValueError("min_images cannot exceed max_images")
        return self


class VideoConfig(_ConfigModel):
    enabled: bool = True
    max_height: int = Field(default=1080, ge=1)
    max_duration_seconds: int = Field(default=600, gt=0)
    max_file_bytes: int = Field(default=1073741824, gt=0)
    max_per_news: int = Field(default=3, ge=0)
    preferred_container: Literal["mp4", "webm"] = "mp4"
    # Optional directory or ffmpeg executable.  FFMPEG_LOCATION takes
    # precedence at runtime; an empty value means automatic discovery.
    ffmpeg_location: str | None = None


class SearchConfig(_ConfigModel):
    max_results: int = Field(ge=1)
    same_domain_depth: int = Field(ge=0, le=3)
    max_candidates_per_source: int = Field(ge=20, le=2000)


class VisualAnalysisConfig(_ConfigModel):
    enabled: bool = False
    mode: Literal["shadow"] = "shadow"
    model_name: str = "ViT-B-32"
    weights_path: str | None = None
    device: str = "cpu"
    batch_size: int = Field(default=8, ge=1, le=128)
    cache_path: str = "cache/visual_analysis.json"


DEFAULT_REJECTED_IMAGE_TYPES = {
    "cg": ["cover", "goods", "logo", "banner", "ui", "photo", "character_art", "announcement_art"],
    "announcement": ["logo", "banner", "ui", "photo", "goods", "cover"],
    "goods": ["logo", "banner", "ui", "photo", "game_cg", "gameplay_screenshot", "key_visual", "character_art", "cover"],
    "release": ["logo", "banner", "ui", "goods", "photo"],
    "generic": ["logo", "banner", "ui"],
    "unknown": ["logo", "banner", "ui"],
}


# Per-news image type caps.  Keeping these in the config model gives older
# TOML files a safe default while allowing editorial overrides in TOML.
DEFAULT_TYPE_LIMITS = {
    "game_cg": 20,
    "gameplay_screenshot": 10,
    "key_visual": 1,
    "cover": 1,
    "character_art": 4,
    "announcement_art": 3,
    "goods": 10,
    "background_art": 20,
    "decorative": 0,
    "unknown": 0,
    "photo": 0,
    "logo": 0,
    "banner": 0,
    "ui": 0,
}


class ImageTypeConfig(_ConfigModel):
    banner_aspect_ratio: float = Field(default=3.0, gt=1.0)
    character_aspect_ratio: float = Field(default=1.8, gt=1.0)
    scene_aspect_ratio: float = Field(default=1.15, gt=1.0)
    minimum_gallery_group_size: int = Field(default=2, ge=2)
    minimum_type_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    unknown_requires_review: bool = True
    fallback_penalty: float = Field(default=0.25, ge=0.0, le=1.0)
    auto_select_unknown: bool = False
    max_unknown_per_news: int = Field(default=0, ge=0)
    rejected_image_types_by_requirement: dict[str, list[str]] = Field(
        default_factory=lambda: copy.deepcopy(DEFAULT_REJECTED_IMAGE_TYPES)
    )


class BrowserConfig(_ConfigModel):
    enabled: bool = True
    timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    max_pages_per_news: int = Field(default=3, ge=0, le=3)
    max_operations: int = Field(default=40, ge=0, le=40)


class PrescanConfig(_ConfigModel):
    scoring: ScoringConfig
    filters: FilterConfig
    network: NetworkConfig
    selection: SelectionConfig
    search: SearchConfig
    image_types: ImageTypeConfig = Field(default_factory=ImageTypeConfig)
    visual_analysis: VisualAnalysisConfig = Field(default_factory=VisualAnalysisConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _default_path() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        candidates = []
        if meipass:
            candidates.append(Path(meipass) / "config" / "default.toml")
        executable = getattr(sys, "executable", None)
        if executable:
            candidates.append(Path(executable).resolve().parent / "config" / "default.toml")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return Path(__file__).resolve().parents[2] / "config" / "default.toml"


def load_config(path: Path | str | None = None) -> PrescanConfig:
    """Load repository defaults and recursively overlay an optional TOML file."""
    default_path = _default_path()
    if not default_path.is_file():
        raise FileNotFoundError(f"default configuration is missing: {default_path}")
    try:
        with default_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid default configuration: {exc}") from exc
    if path is not None:
        selected = Path(path)
        if not selected.is_file():
            raise FileNotFoundError(f"configuration is missing: {selected}")
        try:
            with selected.open("rb") as handle:
                payload = _merge(payload, tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid configuration: {exc}") from exc
    try:
        return PrescanConfig.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"invalid prescan configuration: {exc}") from exc
