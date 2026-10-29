from datetime import datetime, timezone
import hashlib

from PIL import Image
import pytest

from galgame_news.domain import ImageCandidate
from galgame_news.media_sync import reconcile_task
from galgame_news.tasks.repository import TaskRepository


@pytest.mark.parametrize("media_dir", ["assets/originals", "retries/n1/attempt/images"])
def test_unchanged_normal_image_keeps_review_and_replacement_resets_it(tmp_path, media_dir):
    root = tmp_path / "task"
    repo = TaskRepository(root)
    repo.activate()
    repo.save_checkpoint({"issue_id": "261", "issue": {"issue_id": "261", "news_items": [
        {"id": "n1", "sequence": 1, "section": "新作", "title": "Game", "game_names": ["Game"]}
    ]}})
    source = root / media_dir / "flat.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (64, 48), "red").save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = ImageCandidate(news_id="n1", image_url="https://example.invalid/a.png",
        source_url="https://example.invalid/game", fetched_at=datetime.now(timezone.utc),
        local_path=str(source), original_path=str(source), sha256=digest,
        original_sha256=digest, selected=True, download_status="downloaded")
    repo.upsert_candidates([candidate.model_dump(mode="json")])
    key = str(candidate.id)
    repo.save_review_state({"decisions": {key: "accepted"}, "manual_decisions": [key]})
    reconcile_task(root)
    reconcile_task(root)
    assert repo.load_review_state()["decisions"][key] == "accepted"

    Image.new("RGB", (64, 48), "blue").save(source)
    report = reconcile_task(root)
    assert key in report["review_reset"]
    assert repo.load_review_state()["decisions"][key] == "pending"
    reconcile_task(root)
    assert len(list(repo.iter_candidates())) == 1
