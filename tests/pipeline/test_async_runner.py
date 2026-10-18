from __future__ import annotations

from galgame_news.pipeline.checkpoint import load_checkpoint

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
import time

from PIL import Image

from galgame_news.config import load_config, CONCURRENCY_FIELDS
from galgame_news.domain import CollectionResult, ImageCandidate, NewsItem, Issue, SourceRef, SourceType
from galgame_news.discovery.resolver import DefaultSourceResolver
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest
from galgame_news.pipeline.checkpoint import config_hash


def _fixture(tmp_path):
    path = tmp_path / "fixture.docx"
    path.write_bytes(b"fixture")
    news = NewsItem(issue_id="1", sequence=1, section="新作", title="Game Alpha CG更新", body="CG", game_names=["Game Alpha"])
    class Parser:
        def parse(self, *_):
            return None
    class Analyzer:
        def analyze(self, _):
            return Issue(issue_id="1", input_path=str(path), news_items=[news])
    return path, news, Parser(), Analyzer()


def _image_response(url, **kwargs):
    image = BytesIO()
    Image.new("RGB", (640, 480), "red").save(image, format="PNG")
    return type("Response", (), {"url": url, "headers": {"content-type": "image/png"}, "content": image.getvalue(), "status_code": 200})()


def test_async_sources_finish_out_of_order_but_merge_in_order_and_isolate_failure(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)
    finished = []
    class Resolver:
        def resolve(self, news):
            return [SourceRef(url=f"https://site.example/{i}", domain="site.example", source_type=SourceType.OFFICIAL_SITE) for i in range(4)]
    class Adapter:
        def __init__(self, source):
            self.source = source
        def collect(self, news, source, context):
            index = int(source.url.rsplit("/", 1)[1])
            time.sleep((4-index) * 0.02)
            finished.append(index)
            if index == 2:
                raise ValueError("one source failed")
            return CollectionResult(candidates=[ImageCandidate(news_id=news.id, image_url=f"https://images.example/cg{index}.png", source_url=source.url, source_type=source.source_type, fetched_at=datetime.now(timezone.utc))])
    class RecordingRunner(PipelineRunner):
        def _curate_result(self, issue, candidates, *args):
            self.merged_urls = [c.image_url for c in candidates]
            return super()._curate_result(issue, candidates, *args)
    runner = RecordingRunner(parser=parser, analyzer=analyzer, resolver=Resolver(), adapter_factory=lambda n,s: Adapter(s), image_transport=_image_response)
    result = asyncio.run(runner.run_async(TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=True)))
    assert result.status == "completed"
    assert finished != sorted(finished)
    assert [url.rsplit("cg", 1)[1] for url in runner.merged_urls] == ["0.png", "1.png", "3.png"]
    assert any(f.code == "source_failed" for f in result.failures)
    assert runner.network_metrics["socialdata_requests"] == 0
    assert not list(result.task_dir.rglob("*.part"))
    for candidate in result.result.all_candidates:
        with Image.open(candidate.local_path) as decoded:
            assert decoded.format in {"PNG", "JPEG"}


def test_gallery_fragments_and_age_state_share_page_cache(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)
    news.source_urls = ["https://game.example/index.html", "https://game.example/index.html#GALLERY"]
    calls = []
    def transport(url, **kwargs):
        cookie = kwargs["headers"].get("cookie", "")
        calls.append((url, cookie))
        if not cookie:
            html = '<button id="confirm-yes">18歳以上</button><script>Cookies.set("PermitRate", "18")</script>'
        else:
            html = '<title>Game Alpha</title><section id="GALLERY"><img src="cg01.png" alt="Game Alpha event CG"></section>'
        return type("Response", (), {"url":url, "status_code":200, "headers":{"content-type":"text/html; charset=utf-8"}, "text":html})()
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=DefaultSourceResolver(search_provider=lambda _: []), source_transport=transport, image_transport=_image_response)
    result = runner.run(TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=True))
    assert result.status == "completed"
    assert len(calls) == 2
    assert len(result.result.all_candidates) == 1
    assert result.result.all_candidates[0].signals["gallery_path"] is True
    assert runner.network_metrics["pages"]["cache_hits"] >= 2


def test_concurrency_settings_do_not_change_old_checkpoint_hash():
    config = load_config()
    old_config = config.model_dump(mode="json")
    for field in CONCURRENCY_FIELDS:
        old_config["network"].pop(field)
    old_config.pop("browser", None)
    old_config["selection"]["type_limits"].pop("decorative", None)
    for values in old_config["image_types"]["rejected_image_types_by_requirement"].values():
        if "decorative" in values:
            values.remove("decorative")
    import hashlib
    expected = hashlib.sha256(json.dumps({"config": old_config, "request": {}}, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    assert config_hash(config) == expected
    config.network.page_concurrency = 1
    config.network.image_concurrency = 3
    assert config_hash(config) == expected


def test_stop_during_async_download_leaves_no_partial_or_completed_news(tmp_path):
    async def run():
        path, news, parser, analyzer = _fixture(tmp_path)
        token = CancellationToken()
        entered = asyncio.Event()
        class Resolver:
            def resolve(self, _):
                return [SourceRef(url="https://images.example/cg.png", source_type=SourceType.DIRECT_IMAGE, domain="images.example")]
        async def transport(url, **kwargs):
            entered.set()
            await asyncio.sleep(10)
            return _image_response(url)
        runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=Resolver(), image_transport=transport)
        task = asyncio.create_task(runner.run_async(TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=True), cancellation_token=token))
        await entered.wait()
        token.cancel()
        result = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled"
        assert load_checkpoint(result.checkpoint_path).model_dump(mode="json")["completed_news_ids"] == []
        assert not list(result.task_dir.rglob("*.part"))
    asyncio.run(run())


def test_resume_after_concurrency_change_skips_finished_news(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)
    calls = []
    class Resolver:
        def resolve(self, _):
            calls.append(1)
            return []
    config = load_config()
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=Resolver(), config=config)
    request = TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=True)
    first = runner.run(request)
    config.network.page_concurrency = 1
    resumed = runner.run(replace(request, resume=True, task_dir=first.task_dir))
    assert resumed.status == "completed" and resumed.resumed
    assert calls == [1]
