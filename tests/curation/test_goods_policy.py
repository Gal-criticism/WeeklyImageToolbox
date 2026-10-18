from datetime import datetime, timezone
from PIL import Image

from galgame_news.config import load_config
from galgame_news.curation.curator import ImageCurator
from galgame_news.curation.entity_matching import EntityMatcher
from galgame_news.curation.image_typing import ImageTypeClassifier
from galgame_news.curation.source_policy import SourceTrustPolicy
from galgame_news.domain import EventType, ImageCandidate, ImageEvidence, ImageNeed, ImageType, Issue, NewsItem, SourceType

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def goods_news():
    return NewsItem(issue_id="goods", sequence=1, section="周边", title="新品周边", body="商品公开", event_type=EventType.GOODS,
                    image_need=ImageNeed.EXPLICIT_NEW_IMAGE, game_names=["Example Game"], source_urls=["https://shop.getchu.com/item/42"])


def goods_candidate(**signals):
    item = goods_news()
    return ImageCandidate(news_id=item.id, image_url="https://shop.getchu.com/assets/cg-cover.jpg",
        source_url="https://shop.getchu.com/item/42", source_type=SourceType.UNVERIFIED,
        fetched_at=NOW, width=1280, height=720, downloadable=True,
        evidence=[ImageEvidence(page_url="https://shop.getchu.com/item/42", item_id="42", role="goods", text="Example Game jacket")],
        signals={"goods_context": True, "goods_association": "matched", "goods_item_id": "42", "goods_role": "cover", "goods_site": "getchu", "goods_original": True, **signals})


def test_goods_matched_evidence_overrides_unrelated_page_title_but_explicit_conflict_wins():
    item = goods_news()
    candidate = goods_candidate(page_title="Unrelated title", conflicting_entity="Another Game")
    assert EntityMatcher().match(item, candidate).matched is False
    candidate.signals.pop("conflicting_entity")
    match = EntityMatcher().match(item, candidate)
    assert (match.matched, match.confidence) == (True, 0.96)


def test_uncertain_goods_does_not_match_on_merchant_alone():
    candidate = goods_candidate(goods_association="uncertain")
    assert EntityMatcher().match(goods_news(), candidate).matched is None


def test_goods_evidence_for_a_different_item_does_not_match():
    item = goods_news()
    candidate = goods_candidate()
    candidate.evidence[0].item_id = "other-item"
    assert EntityMatcher().match(item, candidate).matched is None


def test_goods_cover_is_goods_not_cg_and_store_trust_requires_matched_item():
    candidate = goods_candidate(page_title="Example Game event CG gallery", gallery=True)
    typed = ImageTypeClassifier(load_config().image_types).classify(goods_news(), candidate)
    assert (typed.image_type, typed.confidence) == (ImageType.GOODS, 0.96)
    assert SourceTrustPolicy().classify(candidate).tier == "official_store"
    candidate.signals["goods_association"] = "uncertain"
    assert SourceTrustPolicy().classify(candidate).tier != "official_store"


def test_goods_fields_do_not_activate_outside_z_news():
    item = goods_news()
    item.section = "新作"
    candidate = goods_candidate()
    candidate.evidence.clear()
    match = EntityMatcher().match(item, candidate)
    assert match.matched is None
    typed = ImageTypeClassifier(load_config().image_types).classify(item, candidate)
    assert "goods_item_evidence" not in typed.supporting_signals


def test_explicit_local_logo_decorative_and_character_roles_survive_goods_evidence():
    from galgame_news.domain import ImageEvidence

    for role, expected in (("logo", ImageType.LOGO), ("decorative", ImageType.DECORATIVE), ("character_art", ImageType.CHARACTER_ART)):
        candidate = goods_candidate()
        candidate.evidence.append(ImageEvidence(page_url=candidate.source_url, item_id="42", role=role, method="dom"))
        typed = ImageTypeClassifier(load_config().image_types).classify(goods_news(), candidate)
        assert typed.image_type is expected


def test_reference_is_third_party_and_non_original_goods_are_manual_in_curator():
    item = goods_news()
    candidate = goods_candidate(goods_role="reference", goods_reference=True, goods_original=False)
    assert SourceTrustPolicy().classify(candidate).tier == "third_party_news"
    result = ImageCurator(load_config()).curate(Issue(issue_id="goods", input_path="goods.json", news_items=[item]), [candidate])
    assert candidate.signals["auto_select"] is False
    assert "goods_reference_requires_manual_review" in candidate.selection_reasons
    assert "automatic_type_selection_disabled" not in candidate.selection_reasons


def test_matched_primary_goods_can_be_selected_with_decodable_local_image(tmp_path):
    from galgame_news.domain import ImageCurationStatus

    item = goods_news()
    candidate = goods_candidate()
    image_path = tmp_path / "goods.png"
    Image.new("RGB", (1280, 720), "#884466").save(image_path)
    candidate.local_path = str(image_path)
    candidate.original_path = str(image_path)
    result = ImageCurator(load_config()).curate(Issue(issue_id="goods", input_path="goods.json", news_items=[item]), [candidate])
    assert candidate in result.candidates
    assert candidate.selected is True
    assert candidate.curation_status is ImageCurationStatus.SELECTED
