from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest
import asyncio

from galgame_news.config import load_config
from galgame_news.domain import CollectionResult, ImageCandidate, Issue, NewsItem, SourceType
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest


SOURCE = "https://official.example/news"
IMAGE = "https://official.example/image.jpg"


def _fixture(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    news = NewsItem(issue_id="261", sequence=1, section="新作", title="测试作品更新", body="详情")
    object.__setattr__(news, "id", "news-1")
    issue = Issue(issue_id="261", input_path=str(root / "input.docx"), news_items=[news])

    class Adapter:
        def collect(self, item, source, context):
            return CollectionResult(candidates=[ImageCandidate(
                news_id=item.id, image_url=IMAGE, source_url=source.url,
                source_type=SourceType.OFFICIAL_SITE,
                nearby_text=item.title,
                fetched_at=datetime.now(timezone.utc),
            )])

    image = BytesIO()
    Image.new("RGB", (800, 600), "cyan").save(image, format="JPEG")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=image.getvalue(),
        headers={"content-type": "image/jpeg"}, request=request))
    config = load_config()
    config.filters.min_width = 100
    config.filters.min_height = 100
    runner = PipelineRunner(config=config, adapter_factory=lambda *_: Adapter(),
        image_transport=lambda url, **kwargs: transport.handle_request(httpx.Request("GET", url)))
    request = TaskRequest(input_path=root / "input.docx", issue_id="261", output_dir=root, no_videos=True)
    runner._write_checkpoint(root / "checkpoint.json", status="completed", task_id="task-1",
        issue=issue, input_hash="input-hash", config_hash="config-hash",
        completed_news_ids=[news.id], candidates=[], videos=[], failures=[], source_map={})
    return runner, root, news, request


def test_retry_news_async_uses_task_request_and_exposes_attempt_result(tmp_path, monkeypatch):
    runner, root, news, request = _fixture(tmp_path)
    from galgame_news.curation import ImageCurator
    original_curate = ImageCurator.curate

    def curate(self, issue, candidates, history=None):
        result = original_curate(self, issue, candidates, history)
        for candidate in [*result.candidates, *result.filtered_candidates]:
            candidate.selected = True
        return result

    monkeypatch.setattr(ImageCurator, "curate", curate)
    from galgame_news.delivery.output import OutputManager
    monkeypatch.setattr(OutputManager, "_save_review_image", classmethod(lambda cls, candidate, source, root, destination: True))
    events = []
    result = asyncio.run(runner.retry_news_async(task_root=root, issue_id="261", news_id=news.id,
        official_url=SOURCE, news_item=news, request=request, event_sink=events.append))

    payload = json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))
    assert result.attempt_id.startswith("retry-")
    assert result.path.is_dir()
    assert payload["kind"] == "retry"
    assert payload["candidates"][0]["selected"] is True
    assert result.status_counts["downloaded"] == 1
    assert events


def test_sync_retry_news_keeps_legacy_attempt_id_return(tmp_path):
    runner, root, news, _request = _fixture(tmp_path)
    attempt_id = runner.retry_news(task_root=root, issue_id="261", news_id=news.id,
        official_url=SOURCE, input_path=root / "input.docx")
    assert isinstance(attempt_id, str) and attempt_id.startswith("retry-")


def test_retry_rejects_corrupt_checkpoint(tmp_path):
    runner, root, news, request = _fixture(tmp_path)
    (root / "checkpoint.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(runner.retry_news_async(task_root=root, issue_id="261", news_id=news.id,
            official_url=SOURCE, news_item=news, request=request))


def test_retry_without_checkpoint_requires_actual_news_item(tmp_path):
    runner, root, news, request = _fixture(tmp_path)
    (root / "checkpoint.json").unlink()
    result = asyncio.run(runner.retry_news_async(task_root=root, issue_id="261", news_id=news.id,
        official_url=SOURCE, news_item=news, request=request))
    assert result.status in {"completed", "partial"}
    assert json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))["news_items"][0]["title"] == news.title

    other_root = tmp_path / "without_news"
    other_root.mkdir()
    with pytest.raises(ValueError, match="未找到当前新闻"):
        asyncio.run(runner.retry_news_async(task_root=other_root, issue_id="261", news_id="unknown",
            official_url=SOURCE, request=request))


def test_retry_skips_malformed_prior_batches_and_candidates_but_keeps_valid_records(tmp_path):
    from galgame_news.pipeline.supplement import _prior_candidates

    runner, root, news, _request = _fixture(tmp_path)
    valid = ImageCandidate(news_id=news.id, image_url=IMAGE, source_url=SOURCE,
        source_type=SourceType.OFFICIAL_SITE, fetched_at=datetime.now(timezone.utc))
    retry_root = root / "retries" / news.id
    (retry_root / "retry-bad").mkdir(parents=True)
    (retry_root / "retry-bad" / "image_index.json").write_text("{broken", encoding="utf-8")
    (retry_root / "retry-good").mkdir()
    batch = {"kind": "retry", "source_url": SOURCE, "sources": [], "candidates": [
        valid.model_dump(mode="json"), {"news_id": news.id, "id": "malformed"},
    ]}
    (retry_root / "retry-good" / "image_index.json").write_text(json.dumps(batch), encoding="utf-8")

    candidates, sources, failures = _prior_candidates(root.resolve(), news.id, SOURCE, "retry")
    assert valid.id in candidates
    assert sources == []
    assert {failure.code for failure in failures} == {"prior_retry_batch_invalid", "prior_candidate_invalid"}


def test_corrupt_raw_image_index_remains_a_critical_error(tmp_path):
    from galgame_news.pipeline.supplement import _prior_candidates

    root = tmp_path / "task"
    (root / "raw").mkdir(parents=True)
    (root / "raw" / "image_index.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _prior_candidates(root.resolve(), "news-1", SOURCE, "retry")


def test_retry_cancelled_before_collection_writes_empty_cancelled_attempt(tmp_path):
    runner, root, news, request = _fixture(tmp_path)
    token = CancellationToken()
    token.cancel()
    result = asyncio.run(runner.retry_news_async(task_root=root, issue_id="261", news_id=news.id,
        official_url=SOURCE, news_item=news, request=request, cancellation_token=token))

    manifest = json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))
    assert result.status == "cancelled"
    assert manifest["status"] == "cancelled"
    assert manifest["candidates"] == []
    assert not any(f.code == "supplement_prepare_failed" for f in result.failures)
