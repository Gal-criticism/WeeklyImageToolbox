from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from ..config import PrescanConfig, load_config
from ..domain import CurationResult, FailureRecord, FailureStage, ImageCandidate, ImageCurationStatus, ImageType, Issue, ReviewReason
from .allocator import ImageAllocator
from .dedup import Deduplicator
from .entity_matching import EntityMatcher
from .image_typing import ImageRequirementPolicy, ImageTypeClassifier, ImageTypeDecision, ImageTypeResult, _official_news_linked_gallery
from .gallery_evidence import inherit_gallery_evidence, reset_selection
from .ranker import ImageRanker
from .source_policy import SourceTrustPolicy
from .visual_analysis import VisualAnalysisService, create_local_openclip_analyzer
from ..delivery.helpers import section_prefix, section_label
from ..localization.policy import LocalizationImagePolicy
from ..discovery.goods_context import is_goods_news


class ImageCurator:
    """Coordinate image typing, policy filtering, deduplication, ranking and allocation."""

    def __init__(self, config: PrescanConfig | None = None, *, visual_analyzer=None):
        self.config = config or load_config()
        self.visual_service = None
        self.visual_initialization_error = None
        if self.config.visual_analysis.enabled:
            try:
                analyzer = visual_analyzer or create_local_openclip_analyzer(
                    self.config.visual_analysis.model_name,
                    self.config.visual_analysis.weights_path or "",
                    self.config.visual_analysis.device,
                )
                self.visual_service = VisualAnalysisService(
                    analyzer,
                    cache_path=self.config.visual_analysis.cache_path,
                    batch_size=self.config.visual_analysis.batch_size,
                )
            except Exception as exc:
                # Optional shadow analysis must never stop the rule-based path.
                self.visual_initialization_error = str(exc)

    @staticmethod
    def _source_key(value: str) -> str:
        parts = urlsplit(value.strip())
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), parts.query, ""))

    @classmethod
    def _source_linked(cls, item, candidate: ImageCandidate) -> bool:
        if not item.source_urls:
            return False
        roots = {cls._source_key(url) for url in item.source_urls if url}
        candidate_sources = [candidate.source_url, candidate.news_source_url or "", candidate.signals.get("root_source_url", "")]
        alternates = candidate.signals.get("source_chain_alternates", "")
        if isinstance(alternates, str):
            candidate_sources.extend(alternates.split("|"))
        return any(cls._source_key(str(url)) in roots for url in candidate_sources if url)

    def curate(self, issue: Issue, candidates: Iterable[ImageCandidate], history=None) -> CurationResult:
        values = list(candidates)
        for candidate in values:
            reset_selection(candidate)
        failures: list[FailureRecord] = []
        if self.config.visual_analysis.enabled:
            if self.visual_initialization_error:
                failures.append(FailureRecord(
                    stage=FailureStage.CURATE,
                    code="visual_shadow_unavailable",
                    message=self.visual_initialization_error,
                    retryable=False,
                ))
            elif self.visual_service is not None:
                result = self.visual_service.annotate(values)
                if result.failed:
                    failures.append(FailureRecord(
                        stage=FailureStage.CURATE,
                        code="visual_shadow_inference_failed",
                        message=f"local visual shadow inference failed for {result.failed} image(s)",
                        retryable=True,
                    ))
        historical_hashes = {}
        if history is not None and hasattr(history, "known_image"):
            for candidate in values:
                try:
                    if history.known_image(candidate.sha256, candidate.perceptual_hash) is not None:
                        if ReviewReason.HISTORICAL_DUPLICATE not in candidate.review_reasons:
                            candidate.review_reasons.append(ReviewReason.HISTORICAL_DUPLICATE)
                except Exception as exc:
                    failures.append(FailureRecord(stage=FailureStage.HISTORY, news_id=candidate.news_id, candidate_id=candidate.id, code="history_lookup_error", message=str(exc), retryable=True))
        by_news = {item.id: item for item in issue.news_items}
        classifier = ImageTypeClassifier(self.config.image_types)
        policy = ImageRequirementPolicy(self.config.image_types)
        entity_matcher = EntityMatcher()
        inherit_gallery_evidence(by_news, values, _official_news_linked_gallery, classifier,
                                 lambda news, candidate: entity_matcher.match(news, candidate).matched is True)
        source_policy = SourceTrustPolicy()
        eligible: list[ImageCandidate] = []
        filtered: list[ImageCandidate] = []
        for candidate in values:
            item = by_news.get(candidate.news_id)
            x_api_photo = (
                candidate.signals.get("x_api_photo") is True
                or candidate.signals.get("socialdata_photo") is True
            )
            priority_x_photo = False
            candidate.selection_reasons.clear()
            try:
                host = (urlsplit(candidate.source_url).hostname or "").casefold()
                localization_candidate = item is not None and section_prefix(section_label(item)) == "h" and (
                    candidate.localization_provenance is not None
                    or host in {"vndb.org", "www.vndb.org", "store.steampowered.com"}
                ) and not x_api_photo
                if localization_candidate:
                    linked = self._source_linked(item, candidate)
                    decision = LocalizationImagePolicy().evaluate(item, candidate, linked)
                    candidate.image_type = decision.image_type
                    candidate.image_type_confidence = decision.confidence
                    candidate.selection_reasons.extend(decision.reasons)
                    candidate.signals.update(
                        auto_select=decision.auto_select,
                        localization_qualified=decision.auto_select,
                        localization_rank_priority=decision.rank_priority,
                        source_linked=linked, source_officiality=0.85,
                        entity_match=decision.entity_confirmed,
                        entity_match_confidence=1.0 if decision.entity_confirmed else 0.0,
                        game_match=1.0 if decision.entity_confirmed else 0.0,
                        type_match=1.0 if decision.auto_select else 0.5,
                    )
                    if candidate.signals.get("invalid_reason"):
                        candidate.curation_status = ImageCurationStatus.INVALID
                        candidate.signals["auto_select"] = False
                    if not candidate.signals["auto_select"] and ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
                    eligible.append(candidate)
                    continue
                if item is None:
                    typed = ImageTypeResult(ImageType.UNKNOWN, 0.0, ("news_not_found",))
                    decision = ImageTypeDecision(True, requires_review=True, auto_select=False, type_match=0.1)
                else:
                    typed = classifier.classify(item, candidate)
                    decision = policy.evaluate(item, typed)
                candidate.image_type = typed.image_type
                candidate.image_type_confidence = typed.confidence
                if not decision.auto_select and not candidate.signals.get("invalid_reason"):
                    candidate.selection_reasons.append("type_evidence_insufficient" if typed.confidence < 0.8 else "automatic_type_selection_disabled")
                source_result = source_policy.classify(candidate)
                candidate.signals["source_tier"] = source_result.tier
                candidate.signals["source_officiality"] = source_result.officiality
                candidate.signals["source_is_official"] = source_result.is_official
                source_linked = item is not None and self._source_linked(item, candidate)
                candidate.signals["source_linked"] = source_linked
                priority_x_photo = x_api_photo and source_linked
                if priority_x_photo:
                    candidate.selection_reasons.append("socialdata_x_photo_priority")
                if not source_linked:
                    candidate.selection_reasons.append("source_not_linked_to_news")
                    if ReviewReason.UNCERTAIN_MATCH not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.UNCERTAIN_MATCH)
                    failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="news_source_unlinked", message="candidate source is not linked to this news item's source URLs", source_url=candidate.source_url, retryable=False))
                if typed.supporting_signals:
                    candidate.signals["image_type_supporting_signals"] = ",".join(typed.supporting_signals)
                candidate.signals["type_match"] = decision.type_match
                if typed.image_type is ImageType.DECORATIVE:
                    candidate.selection_reasons.append("decorative_asset_not_auto_selected")
                width, height = candidate.width or 0, candidate.height or 0
                quality_eligible = (
                    width >= self.config.filters.min_width
                    and height >= self.config.filters.min_height
                    and width * height >= self.config.filters.min_pixels
                )
                candidate.signals["quality_eligible"] = quality_eligible
                animated_source = candidate.animated_source or candidate.signals.get("animated_source") is True
                if animated_source:
                    candidate.animated_source = True
                    candidate.selection_reasons.append("animated_source_requires_review")
                    if ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
                candidate.signals["auto_select"] = (
                    (priority_x_photo or (decision.auto_select and quality_eligible))
                    and source_linked
                    and not animated_source
                )
                goods_active = candidate.signals.get("goods_context") is True and item is not None and is_goods_news(item)
                goods_role = str(candidate.signals.get("goods_role") or "").casefold()
                goods_reference = candidate.signals.get("goods_reference") is True or goods_role == "reference"
                if goods_active:
                    if goods_reference:
                        candidate.signals["auto_select"] = False
                        candidate.selection_reasons.append("goods_reference_requires_manual_review")
                    elif candidate.signals.get("goods_original") is False:
                        candidate.signals["auto_select"] = False
                        candidate.selection_reasons.append("goods_non_original_requires_manual_review")
                    if (goods_reference or candidate.signals.get("goods_original") is False) and ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
                if not quality_eligible:
                    candidate.selection_reasons.append("below_minimum_resolution")
                    if ReviewReason.IMAGE_QUALITY_REVIEW not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.IMAGE_QUALITY_REVIEW)
                    if not priority_x_photo:
                        failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="image_quality_below_auto_select_threshold", message="image is retained for review but does not meet automatic selection dimensions", source_url=candidate.image_url, retryable=False))
                if candidate.signals.get("invalid_reason"):
                    candidate.curation_status = ImageCurationStatus.INVALID
                    candidate.signals["auto_select"] = False
                    candidate.selection_reasons.append(f"invalid:{candidate.signals['invalid_reason']}")
                if candidate.signals.get("conversion_error"):
                    candidate.signals["auto_select"] = False
                    candidate.selection_reasons.append("review_image_conversion_failed")
                if priority_x_photo:
                    candidate.signals.pop("fallback_only", None)
                elif decision.fallback_only:
                    candidate.signals["fallback_only"] = True
                if decision.requires_review and ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                    candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
                try:
                    # Remove the previous pass's derived conflict before
                    # recomputing entity evidence for replay/review workflows.
                    candidate.signals.pop("entity_conflict", None)
                    match = entity_matcher.match(item, candidate) if item is not None else None
                except Exception as exc:
                    # A malformed page-local signal must not discard the
                    # other candidates for this news item.
                    candidate.signals["entity_match"] = "unknown"
                    candidate.signals["entity_match_confidence"] = 0.0
                    candidate.signals["auto_select"] = False
                    if ReviewReason.UNCERTAIN_MATCH not in candidate.review_reasons:
                        candidate.review_reasons.append(ReviewReason.UNCERTAIN_MATCH)
                    eligible.append(candidate)
                    failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="entity_match_error", message=str(exc), source_url=candidate.image_url, retryable=False))
                    continue
                if match is not None:
                    candidate.signals["entity_match"] = match.matched if match.matched is not None else "unknown"
                    candidate.signals["entity_match_confidence"] = match.confidence
                    candidate.signals["entity_match_evidence"] = ",".join(match.supporting_signals)
                    candidate.signals["official_domain_match"] = match.official_domain_match
                    if match.conflicting_entities:
                        candidate.signals["entity_conflict"] = ",".join(match.conflicting_entities)
                    if match.matched is False:
                        if priority_x_photo:
                            candidate.selection_reasons.append("socialdata_x_photo_source_overrides_entity_classifier")
                        else:
                            candidate.selected = False
                            candidate.selection_reasons.append("entity_conflict")
                            filtered.append(candidate)
                            failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="entity_mismatch", message="candidate conflicts with the news game entity", source_url=candidate.image_url, retryable=False))
                            continue
                    if match.matched is None:
                        candidate.selection_reasons.append("entity_unverified")
                        if not priority_x_photo:
                            candidate.signals["auto_select"] = False
                            if ReviewReason.UNCERTAIN_MATCH not in candidate.review_reasons:
                                candidate.review_reasons.append(ReviewReason.UNCERTAIN_MATCH)
                            failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="entity_unverified", message="candidate lacks sufficient game/entity context", source_url=candidate.image_url, retryable=False))
                    elif not priority_x_photo and (
                        (not source_result.is_official and match.confidence < 0.8)
                        or (
                            source_result.tier in {"official_brand_page", "official_store"}
                            and not any(signal in match.supporting_signals for signal in ("entity_name_in_context", "page_context", "steam_app_id_match", "explicit_entity_signal", "official_news_linked_goods", "goods_item_match"))
                        )
                    ):
                        candidate.signals["auto_select"] = False
                        if ReviewReason.UNCERTAIN_MATCH not in candidate.review_reasons:
                            candidate.review_reasons.append(ReviewReason.UNCERTAIN_MATCH)
                        failures.append(FailureRecord(stage=FailureStage.CURATE, news_id=candidate.news_id, candidate_id=candidate.id, code="entity_unverified", message="non-official candidate requires stronger entity evidence", source_url=candidate.image_url, retryable=False))
                if decision.accepted or priority_x_photo:
                    eligible.append(candidate)
                else:
                    candidate.selected = False
                    candidate.selection_reasons.append(f"image_type_rejected:{typed.image_type.value}")
                    filtered.append(candidate)
                    failures.append(FailureRecord(
                        stage=FailureStage.CURATE,
                        news_id=candidate.news_id,
                        candidate_id=candidate.id,
                        code="image_type_rejected",
                        message=f"image_type={typed.image_type.value}; reason={decision.rejection_reason or 'rejected by image requirement policy'}",
                        source_url=candidate.image_url,
                        retryable=False,
                    ))
            except Exception as exc:
                candidate.image_type = ImageType.UNKNOWN
                candidate.image_type_confidence = 0.0
                candidate.signals["type_match"] = 0.1
                candidate.signals["auto_select"] = False
                if ReviewReason.IMAGE_TYPE_REVIEW not in candidate.review_reasons:
                    candidate.review_reasons.append(ReviewReason.IMAGE_TYPE_REVIEW)
                eligible.append(candidate)
                failures.append(FailureRecord(
                    stage=FailureStage.CURATE,
                    news_id=candidate.news_id,
                    candidate_id=candidate.id,
                    code="image_type_error",
                    message=str(exc),
                    source_url=candidate.image_url,
                    retryable=False,
                ))
        ranker = ImageRanker(self.config.scoring, self.config.image_types)
        ranked_all: list[ImageCandidate] = []
        for news_id in sorted({candidate.news_id for candidate in values}):
            item = by_news.get(news_id)
            group = [candidate for candidate in values if candidate.news_id == news_id]
            ranked_all.extend(ranker.rank(item, group) if item is not None else group)

        # Deduplication is news-local and affects only automatic selection.
        # Every valid member stays in the result for review and provenance.
        eligible_ids = {id(candidate) for candidate in eligible}
        ranked_eligible = [candidate for candidate in ranked_all if id(candidate) in eligible_ids]
        all_deduped = Deduplicator().deduplicate(ranked_eligible, historical_hashes=historical_hashes)
        auto_eligible_ids = {
            id(candidate) for candidate in eligible
            if candidate.signals.get("auto_select", True) is not False
        }
        manual_candidates = [
            candidate for candidate in eligible
            if candidate.signals.get("auto_select", True) is False
        ]
        deduped = Deduplicator().deduplicate(
            (candidate for candidate in ranked_eligible if id(candidate) in auto_eligible_ids),
            historical_hashes=historical_hashes,
        )
        # Preserve duplicate relationships even when one member is not
        # eligible for automatic selection.
        for candidate in all_deduped.duplicates:
            if candidate not in deduped.duplicates and candidate.signals.get("duplicate_of"):
                candidate.selected = False
        def goods_rank(candidate):
            item = by_news.get(candidate.news_id)
            if not (candidate.signals.get("goods_context") is True and item is not None and is_goods_news(item)
                    and candidate.signals.get("goods_association") == "matched"):
                return 3
            role = str(candidate.signals.get("goods_role") or "").casefold()
            if role in {"main", "design", "bonus", "cover", "sample"}:
                return 0
            if role == "collection":
                return 1
            if candidate.signals.get("goods_reference") is True or role == "reference":
                return 2
            return 3

        ranked = sorted(deduped.unique, key=lambda c: (
            not (c.signals.get("x_api_photo") is True or c.signals.get("socialdata_photo") is True),
            goods_rank(c),
            -int(c.signals.get("localization_rank_priority", 0)),
            -(c.score.total if c.score else 0.0),
            -(c.score.source_trust if c.score else 0.0),
            -((c.width or 0) * (c.height or 0)),
            c.id or "",
        ))
        allocated = ImageAllocator(
            min_images=self.config.selection.min_images,
            max_images=None,
            per_news_max=self.config.selection.per_news_max,
            minimum_score=self.config.selection.minimum_score,
            max_unknown_per_news=self.config.image_types.max_unknown_per_news,
            type_limits=self.config.selection.type_limits,
        ).allocate(issue.news_items, ranked)
        selected_ids = {id(candidate) for candidate in allocated if candidate.selected}
        for candidate in [*allocated, *deduped.duplicates, *manual_candidates, *filtered]:
            if candidate.curation_status is ImageCurationStatus.INVALID:
                continue
            if candidate.signals.get("duplicate_of"):
                if "duplicate_of_better_candidate" not in candidate.selection_reasons:
                    candidate.selection_reasons.append("duplicate_of_better_candidate")
            elif id(candidate) in selected_ids:
                candidate.selection_reasons.append("selected_by_rank_and_policy")
            elif not candidate.selection_reasons:
                reason = "news_selection_limit" if sum(c.selected for c in allocated if c.news_id == candidate.news_id) >= self.config.selection.per_news_max else "type_limit_or_minimum_score"
                candidate.selection_reasons.append(reason)
        result_candidates = [*allocated, *deduped.duplicates, *manual_candidates]
        sort_key = lambda c: (c.news_id, not (c.signals.get("x_api_photo") is True or c.signals.get("socialdata_photo") is True), -(c.score.total if c.score else 0.0), -(c.score.source_trust if c.score else 0.0), -((c.width or 0) * (c.height or 0)), c.id or "")
        result_candidates.sort(key=sort_key)
        filtered.sort(key=sort_key)
        return CurationResult(candidates=result_candidates, filtered_candidates=filtered, failures=failures, selection_shortfall=allocated.selection_shortfall)
