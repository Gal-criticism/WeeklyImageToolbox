from datetime import datetime, timezone

from galgame_news.domain import ImageCandidate, VideoCandidate, candidate_id_for, video_candidate_id_for
from galgame_news.review import ReviewSession


def provenance():
    return {"asset_id": "cloud:123", "source_id": "archive", "revision": "etag-1", "issue_number": "230"}


def image(news_id, archive=True):
    return ImageCandidate(news_id=news_id, image_url="https://example.org/a.png", source_url="https://example.org/", fetched_at=datetime.now(timezone.utc), **({"archive_provenance": provenance()} if archive else {}))


def video(news_id, archive=True):
    return VideoCandidate(news_id=news_id, video_url="https://example.org/a.mp4", source_url="https://example.org/", **({"archive_provenance": provenance()} if archive else {}))


def test_archive_occurrences_keep_per_news_identity_and_old_ids():
    assert image("one").id != image("two").id
    assert video("one").id != video("two").id
    assert image("one", False).id == candidate_id_for("https://example.org/a.png", "one")
    assert video("one", False).id == video_candidate_id_for("https://example.org/a.mp4")


def test_archive_provenance_survives_review_coercion():
    for raw, method in [(image("one").model_dump(mode="json"), ReviewSession._coerce_image), (video("one").model_dump(mode="json"), ReviewSession._coerce_video)]:
        candidate, stable_id = method(raw)
        assert candidate is not None
        assert candidate.archive_provenance == provenance()
        assert stable_id == candidate.id == raw["id"]
