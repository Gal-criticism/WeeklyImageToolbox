from __future__ import annotations

from galgame_news.pipeline.checkpoint import load_checkpoint

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
import threading
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
    events = []
    result = runner.run(TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=True), events.append)
    assert result.status == "completed"
    progress = [event.message for event in events if event.kind == "resolve_progress"]
    assert any("正在检查来源页面" in message and "game.example" in message for message in progress)
    assert any("正在搜索补充来源" in message for message in progress)
    assert any("搜索完成" in message for message in progress)
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


def test_search_timeout_is_recorded_and_next_news_can_continue(tmp_path):
    path, first_news, parser, analyzer = _fixture(tmp_path)
    second_news = NewsItem(
        issue_id="1", sequence=2, section="新作", title="Second Game CG更新",
        body="CG", game_names=["Second Game"],
    )
    analyzer.analyze = lambda _: Issue(
        issue_id="1", input_path=str(path),
        news_items=[first_news, second_news],
    )
    release = threading.Event()
    calls = []

    def search(news):
        calls.append(news.id)
        if news.id == first_news.id:
            release.wait(5)
        return []

    config = load_config()
    config.network.search_timeout_seconds = 0.03
    config.network.news_timeout_seconds = 0.5
    resolver = DefaultSourceResolver(
        same_domain_depth=0,
        search_provider=search,
        search_timeout=config.network.search_timeout_seconds,
    )
    runner = PipelineRunner(
        parser=parser, analyzer=analyzer, resolver=resolver,
        config=config, image_transport=_image_response,
    )
    try:
        result = asyncio.run(runner.run_async(TaskRequest(
            input_path=path, issue_id="1", output_dir=tmp_path / "out",
            no_videos=True,
        )))
    finally:
        release.set()

    assert result.status == "completed"
    assert calls == [first_news.id, second_news.id]
    assert any(value.code == "search_timeout" for value in result.failures)
    checkpoint = load_checkpoint(result.checkpoint_path)
    assert set(checkpoint.completed_news_ids) == {first_news.id, second_news.id}


def test_source_collect_timeout_does_not_block_remaining_sources(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)
    release = threading.Event()

    class Resolver:
        def resolve(self, _news):
            return [
                SourceRef(
                    url="https://slow.example/page", domain="slow.example",
                    source_type=SourceType.OFFICIAL_SITE,
                ),
                SourceRef(
                    url="https://fast.example/page", domain="fast.example",
                    source_type=SourceType.OFFICIAL_SITE,
                ),
            ]

    class Adapter:
        def __init__(self, source):
            self.source = source

        def collect(self, _news, source, _context):
            if source.url.startswith("https://slow.example"):
                release.wait(5)
            return CollectionResult(candidates=[ImageCandidate(
                news_id=news.id,
                image_url=f"{source.url}/cg.png",
                source_url=source.url,
                source_type=source.source_type,
                fetched_at=datetime.now(timezone.utc),
            )])

    config = load_config()
    config.network.news_timeout_seconds = 0.05
    runner = PipelineRunner(
        parser=parser, analyzer=analyzer, resolver=Resolver(),
        adapter_factory=lambda _news, source: Adapter(source),
        config=config, image_transport=_image_response,
    )
    try:
        result = asyncio.run(runner.run_async(TaskRequest(
            input_path=path, issue_id="1", output_dir=tmp_path / "out",
            no_videos=True,
        )))
    finally:
        release.set()

    assert result.status == "completed"
    assert any(value.code == "source_timeout" for value in result.failures)
    assert any(
        "fast.example" in value.image_url
        for value in result.result.all_candidates
    )


def test_stuck_async_collection_times_out_and_next_news_continues(tmp_path):
    path, first_news, parser, analyzer = _fixture(tmp_path)
    second_news = NewsItem(
        issue_id="1", sequence=2, section="新作", title="Second Game CG更新",
        body="CG", game_names=["Second Game"],
    )
    analyzer.analyze = lambda _: Issue(
        issue_id="1", input_path=str(path), news_items=[first_news, second_news],
    )
    reached = []

    class Resolver:
        def resolve(self, news):
            return [SourceRef(
                url=f"https://{news.id}.example/page", domain="example",
                source_type=SourceType.OFFICIAL_SITE,
            )]

    class WatchdogRunner(PipelineRunner):
        async def _resolve_async(self, news, request):
            sources = await super()._resolve_async(news, request)

            async def collect_many(item, sources, request):
                reached.append(item.id)
                if item.id == first_news.id:
                    await asyncio.Event().wait()
                return []

            self._network_runtime.collect_many = collect_many
            return sources

    config = load_config()
    config.network.news_timeout_seconds = 0.05
    runner = WatchdogRunner(
        parser=parser, analyzer=analyzer, resolver=Resolver(), config=config,
        image_transport=_image_response,
    )
    result = asyncio.run(asyncio.wait_for(runner.run_async(TaskRequest(
        input_path=path, issue_id="1", output_dir=tmp_path / "out", no_videos=True,
    )), timeout=5))

    checkpoint = load_checkpoint(result.checkpoint_path)
    assert result.status == "completed"
    assert reached == [first_news.id, second_news.id]
    assert any(value.code == "news_timeout" and value.news_id == first_news.id for value in result.failures)
    assert any(value.code == "news_failed" and value.news_id == first_news.id for value in result.failures)
    assert first_news.id not in checkpoint.completed_news_ids
    assert second_news.id in checkpoint.completed_news_ids


def test_hanging_goods_fallback_search_is_bounded_and_keeps_candidates(tmp_path):
    from types import SimpleNamespace
    from galgame_news.pipeline.goods_collection import GoodsCollectionState, search_goods_after_download

    path, news, parser, analyzer = _fixture(tmp_path)
    candidate = ImageCandidate(
        news_id=news.id, image_url="https://images.example/original.jpg",
        source_url="https://game.example/page", source_type=SourceType.OFFICIAL_SITE,
        fetched_at=datetime.now(timezone.utc), original_path=str(tmp_path / "original.jpg"),
    )
    resolver = DefaultSourceResolver(search_provider=lambda _news: [])
    config = load_config()
    config.network.search_timeout_seconds = 0.03

    class Runtime:
        def __init__(self):
            self.runner = SimpleNamespace(adapter_factory=None, config=config, resolver=resolver)
            self._goods_states = {news.id: GoodsCollectionState(seen=set(), pages=1)}
            self.calls = 0

        async def call_sync(self, call):
            self.calls += 1
            if self.calls == 1:
                return False  # no already-qualified goods image
            await asyncio.Event().wait()

    runtime = Runtime()
    result = asyncio.run(search_goods_after_download(runtime, news, [], SimpleNamespace(offline=False), [candidate]))

    assert runtime.calls == 2
    assert candidate.original_path == str(tmp_path / "original.jpg")
    assert result[0] == []
    assert result[1][0].failures[0].code == "search_timeout"
    assert result[1][0].failures[0].retryable is True


def test_timed_out_source_cannot_starve_single_worker_and_next_news_persists(tmp_path):
    path, first_news, parser, analyzer = _fixture(tmp_path)
    second_news = NewsItem(
        issue_id="1", sequence=2, section="新作", title="Second Game CG更新",
        body="CG", game_names=["Second Game"],
    )
    analyzer.analyze = lambda _: Issue(
        issue_id="1", input_path=str(path), news_items=[first_news, second_news],
    )
    entered, release = threading.Event(), threading.Event()

    class Resolver:
        def resolve(self, news):
            direct = news.id == second_news.id
            return [SourceRef(
                url=("https://images.example/second.png" if direct else "https://slow.example/page"),
                domain="images.example" if direct else "slow.example",
                source_type=SourceType.DIRECT_IMAGE if direct else SourceType.OFFICIAL_SITE,
            )]

    class Adapter:
        def collect(self, _news, source, _context):
            if source.url.startswith("https://slow.example"):
                entered.set()
                release.wait(5)
                return CollectionResult()
            return CollectionResult(candidates=[ImageCandidate(
                news_id=second_news.id, image_url=source.url, source_url=source.url,
                source_type=source.source_type, downloadable=True,
                fetched_at=datetime.now(timezone.utc),
            )])

    config = load_config()
    config.network.page_concurrency = 1
    config.network.news_timeout_seconds = 0.5
    runner = PipelineRunner(
        parser=parser, analyzer=analyzer, resolver=Resolver(),
        adapter_factory=lambda _news, _source: Adapter(),
        config=config, image_transport=_image_response,
    )
    try:
        result = asyncio.run(asyncio.wait_for(runner.run_async(TaskRequest(
            input_path=path, issue_id="1", output_dir=tmp_path / "out", no_videos=True,
        )), timeout=5))
    finally:
        release.set()

    checkpoint = load_checkpoint(result.checkpoint_path)
    assert entered.is_set()
    assert result.status == "completed"
    assert any(value.code == "source_timeout" and value.news_id == first_news.id for value in result.failures)
    assert second_news.id in checkpoint.completed_news_ids
    assert any(value.news_id == second_news.id and value.download_status == "downloaded"
               for value in result.result.all_candidates), (
                   [(value.news_id, value.download_status, value.download_error_code, value.downloadable) for value in result.result.all_candidates],
                   [(value.news_id, value.code, value.message) for value in result.failures],
               )


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


def test_slow_final_export_uses_task_budget_not_request_timeout(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)

    class Resolver:
        def resolve(self, _news):
            return []

    class SlowExportRunner(PipelineRunner):
        def _publish_output(self, result, raw_dir, task_dir):
            time.sleep(0.1)
            return super()._publish_output(result, raw_dir, task_dir)

    config = load_config()
    config.network.timeout_seconds = 0.02
    config.network.news_timeout_seconds = 0.5
    runner = SlowExportRunner(
        parser=parser, analyzer=analyzer, resolver=Resolver(), config=config,
    )

    result = asyncio.run(runner.run_async(TaskRequest(
        input_path=path, issue_id="1", output_dir=tmp_path / "out", no_videos=True,
    )))

    assert result.status == "completed"
    assert result.output_dir.is_dir()


def test_completed_checkpoint_export_uses_task_budget_not_request_timeout(tmp_path):
    path, news, parser, analyzer = _fixture(tmp_path)

    class Resolver:
        def resolve(self, _news):
            return []

    class SlowResumeExportRunner(PipelineRunner):
        slow_export = False

        def _export_review_delivery(self, raw_dir, task_dir):
            if self.slow_export:
                time.sleep(0.1)
            return PipelineRunner._export_review_delivery(raw_dir, task_dir)

    config = load_config()
    config.network.timeout_seconds = 0.02
    config.network.news_timeout_seconds = 0.5
    runner = SlowResumeExportRunner(
        parser=parser, analyzer=analyzer, resolver=Resolver(), config=config,
    )
    request = TaskRequest(
        input_path=path, issue_id="1", output_dir=tmp_path / "out", no_videos=True,
    )
    first = asyncio.run(runner.run_async(request))
    assert first.status == "completed"

    runner.slow_export = True
    resumed = asyncio.run(runner.run_async(replace(
        request, resume=True, task_dir=first.task_dir,
    )))

    assert resumed.status == "completed", resumed.failures
    assert resumed.resumed is True
