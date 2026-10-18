from galgame_news.pipeline.checkpoint import load_checkpoint
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from galgame_news.config import load_config
from galgame_news.domain import CollectionResult, FailureRecord, FailureStage, ImageCandidate, Issue, NewsItem, SourceRef, SourceType, VideoCandidate, VideoStatus
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest
from galgame_news.discovery.async_x_api import AsyncSocialDataTweetTransport


@pytest.fixture(autouse=True)
def public_mock_media_dns(monkeypatch):
    """Mock downloads must not depend on system DNS/proxy addresses."""
    from galgame_news.pipeline import network_runtime
    client_class = network_runtime.AsyncHttpClient

    def create_client(**kwargs):
        return client_class(resolver=lambda _: ["93.184.216.34"], **kwargs)

    monkeypatch.setattr(network_runtime, "AsyncHttpClient", create_client)


def fixture(tmp_path, transport, *, no_videos=True, video_downloader_factory=None, video_urls=()):
    path = tmp_path / "weekly.docx"
    path.write_bytes(b"fixture")
    post = "https://x.com/studio/status/123"
    item = NewsItem(issue_id="1", sequence=1, section="新作", title="Game CG更新", body="CG", game_names=["Game"], source_urls=[post])
    calls = []
    class Parser:
        def parse(self, *args): return None
    class Analyzer:
        def analyze(self, *args): return Issue(issue_id="1", input_path=str(path), news_items=[item])
    class Resolver:
        def resolve(self, *args):
            return [SourceRef(url=post, domain="x.com", source_type=SourceType.OFFICIAL_X)]
    class Adapter:
        def collect(self, news, source, context):
            calls.append(source.url)
            return CollectionResult(candidates=[ImageCandidate(news_id=news.id,
                image_url=f"https://pbs.twimg.com/media/{name}.jpg?name=orig", source_url=post,
                source_type=SourceType.OFFICIAL_X, signals={"socialdata_photo": True}, fetched_at=datetime.now(timezone.utc))
                for name in ("good", "bad")], video_candidates=[VideoCandidate(
                    news_id=news.id, source_url=post, video_url=url,
                ) for url in video_urls])
    config = load_config()
    config.network.max_retries = 1
    config.network.image_concurrency = 2
    def make_runner():
        return PipelineRunner(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(), config=config,
            adapter_factory=lambda *args: Adapter(), image_transport=transport,
            video_downloader_factory=video_downloader_factory)
    return make_runner, TaskRequest(input_path=path, issue_id="1", output_dir=tmp_path/"out", no_videos=no_videos, use_socialdata_x=True), calls


def image_response(url, **kwargs):
    data = BytesIO()
    Image.new("RGB", (800,600), "cyan" if "good" in url else "yellow").save(data, "JPEG")
    return SimpleNamespace(url=url, status_code=200, headers={"content-type":"image/jpeg"}, content=data.getvalue())


def test_published_partial_task_recovers_only_failure_across_new_runner(tmp_path):
    downloads = []
    failing = True
    def transport(url, **kwargs):
        downloads.append(url)
        if "bad" in url and failing: raise httpx.ConnectTimeout("")
        return image_response(url)
    make_runner, request, calls = fixture(tmp_path, transport)
    first = make_runner().run(request)
    assert first.status == "completed"
    assert len(first.result.all_candidates) == 2
    checkpoint = load_checkpoint(first.checkpoint_path).model_dump(mode="json")
    assert len(checkpoint["retryable_candidate_ids"]) == 1
    failing = False
    downloads.clear()
    second = make_runner().run(replace(request, resume=True, task_dir=first.task_dir))
    assert second.status == "completed"
    assert len(downloads) == 1 and "bad" in downloads[0]
    assert len(calls) == 1  # No repeated post collection/query.
    assert all(value.download_status == "downloaded" for value in second.result.all_candidates)
    assert all(value.local_path for value in second.result.all_candidates)
    assert not load_checkpoint(second.checkpoint_path).model_dump(mode="json")["retryable_candidate_ids"]


def test_cancel_after_first_asset_commits_then_resume_only_pending(tmp_path):
    async def run():
        blocked = asyncio.Event()
        failing = True
        downloads = []
        async def transport(url, **kwargs):
            downloads.append(url)
            if "bad" in url and failing:
                blocked.set()
                await asyncio.sleep(30)
            return image_response(url)
        make_runner, request, calls = fixture(tmp_path, transport)
        token = CancellationToken()
        def sink(event):
            if event.kind == "x_media_stats" and event.payload.get("x_downloaded") == 1:
                token.cancel()
        first = await asyncio.wait_for(make_runner().run_async(request, event_sink=sink, cancellation_token=token), 5)
        assert first.status == "cancelled"
        assert len(first.candidates) == 2
        assert any(value.original_path for value in first.candidates)
        assert not list(first.task_dir.rglob("*.part"))
        failing = False
        downloads.clear()
        second = await make_runner().run_async(replace(request, resume=True, task_dir=first.task_dir))
        assert second.status == "completed"
        assert len(downloads) == 1 and "bad" in downloads[0]
        assert len(calls) == 1
    asyncio.run(run())


def test_http404_is_not_retried_on_resume(tmp_path):
    calls_download = []
    def transport(url, **kwargs):
        calls_download.append(url)
        result = image_response(url)
        if "bad" in url: result.status_code = 404
        return result
    make_runner, request, calls = fixture(tmp_path, transport)
    first = make_runner().run(request)
    count = len(calls_download)
    second = make_runner().run(replace(request, resume=True, task_dir=first.task_dir))
    assert second.status == "completed"
    assert len(calls_download) == count and len(calls) == 1


def test_completed_resume_recovers_retryable_videos_and_reuses_verified_assets(tmp_path):
    import hashlib

    video_downloads = []
    def video_factory(config, **kwargs):
        class Downloader:
            def __init__(self):
                self.config = config

            def download(self, candidates, output_dir):
                from pathlib import Path
                output_dir = Path(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                candidates = list(candidates)
                video_downloads.append((self.config.max_per_news, [Path(v.video_url).stem for v in candidates]))
                failures = []
                if len(video_downloads) == 1:
                    for candidate in candidates:
                        slug = Path(candidate.video_url).stem
                        if slug in {"success-a", "success-b"}:
                            path = output_dir / f"{slug}.mp4"
                            path.write_bytes(f"asset-{slug}".encode())
                            candidate.local_path = str(path)
                            candidate.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                            candidate.status = VideoStatus.DOWNLOADED
                        elif slug == "failed":
                            candidate.status = VideoStatus.FAILED
                            candidate.failure_reason = "network timeout"
                            failures.append(FailureRecord(stage=FailureStage.DOWNLOAD,
                                news_id=candidate.news_id, candidate_id=candidate.id,
                                code="timeout", message="network timeout", retryable=True))
                        elif slug in {"height", "filesize", "duration"}:
                            reason = {"height": "height_exceeded", "filesize": "file_size_exceeded",
                                      "duration": "duration_exceeded"}[slug]
                            candidate.status = VideoStatus.SKIPPED
                            candidate.failure_reason = reason
                            failures.append(FailureRecord(stage=FailureStage.DOWNLOAD,
                                news_id=candidate.news_id, candidate_id=candidate.id,
                                code=reason, message=reason, retryable=False))
                        elif slug == "missing":
                            candidate.status = VideoStatus.DOWNLOADED
                            candidate.local_path = str(output_dir / "missing.mp4")
                            candidate.sha256 = "a" * 64
                        else:
                            candidate.status = VideoStatus.DISCOVERED if slug == "discovered" else VideoStatus.DOWNLOADING
                    return candidates, failures

                accepted = 0
                for candidate in candidates:
                    if accepted >= self.config.max_per_news:
                        candidate.status = VideoStatus.SKIPPED
                        candidate.failure_reason = "per_news_limit"
                        candidate.review_reasons.append("per_news_limit")
                        failures.append(FailureRecord(stage=FailureStage.DOWNLOAD,
                            news_id=candidate.news_id, candidate_id=candidate.id,
                            code="per_news_limit", message="per news limit", retryable=False))
                        continue
                    path = output_dir / f"recovered-{accepted}.mp4"
                    path.write_bytes(b"recovered-video")
                    candidate.local_path = str(path)
                    candidate.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                    candidate.status = VideoStatus.DOWNLOADED
                    candidate.failure_reason = None
                    accepted += 1
                return candidates, failures
        return Downloader()

    urls = [f"https://video.twimg.com/{slug}.mp4" for slug in (
        "success-a", "success-b", "failed", "discovered", "downloading",
        "height", "filesize", "duration", "missing")]
    make_runner, request, calls = fixture(tmp_path, image_response, no_videos=False,
        video_downloader_factory=video_factory, video_urls=urls)
    first = make_runner().run(request)
    assert first.status == "completed"
    assert len(first.result.videos) == len(urls)

    second = make_runner().run(replace(request, resume=True, task_dir=first.task_dir))

    assert second.status == "completed"
    assert len(video_downloads) == 2
    cap, attempted = video_downloads[1]
    assert cap == 1  # Two verified successes occupy two of the three slots.
    assert set(attempted) == {"failed", "discovered", "downloading", "height", "filesize", "missing"}
    assert len([video for video in second.result.videos if video.status is VideoStatus.DOWNLOADED]) == 3
    assert not any(failure.candidate_id == next(v.id for v in second.result.videos if v.video_url.endswith("failed.mp4"))
                   for failure in second.result.failures)
    assert len(calls) == 1  # Reuse the saved X response/candidates; do not query again.


def test_cancel_after_paid_collection_uses_durable_public_cache(monkeypatch,tmp_path):
    import galgame_news.pipeline.network_runtime as runtime
    import galgame_news.discovery.x_api as api
    paid_calls=[]
    secret="mock-private-credential"
    payload={"id_str":"123","full_text":"Game news", "extended_entities":{"media":[{
        "type":"photo","media_url_https":"https://pbs.twimg.com/media/good.jpg",
        "original_info":{"width":800,"height":600}}]},"private_debug_header":secret}
    # Unknown response keys are excluded from persisted public-media records.
    payload.pop("private_debug_header")
    def handler(request):
        assert request.headers["authorization"]=="Bearer "+secret
        paid_calls.append(str(request.url))
        return httpx.Response(200,json=payload)
    monkeypatch.setattr(runtime,"AsyncSocialDataTweetTransport",lambda **kwargs:
        AsyncSocialDataTweetTransport(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(api,"CredentialStore",lambda:SimpleNamespace(get_socialdata_api_key=lambda:secret))
    make_runner,request,_=fixture(tmp_path,image_response)
    def actual_runner():
        value=make_runner()
        value.adapter_factory=None
        return value
    token=CancellationToken()
    def sink(event):
        if event.kind=="collect_completed":token.cancel()
    first=actual_runner().run(request,event_sink=sink,cancellation_token=token)
    assert first.status=="cancelled"
    assert len(paid_calls)==1
    second=actual_runner().run(replace(request,resume=True,task_dir=first.task_dir))
    assert second.status=="completed" and len(paid_calls)==1
    assert len(second.result.all_candidates)==1
    assert second.result.all_candidates[0].download_status=="downloaded"
    for path in first.task_dir.rglob("*.json"):
        assert secret not in path.read_text(encoding="utf-8")


def test_old_checkpoint_fields_can_recover_failure_without_journal(tmp_path):
    failing=True
    downloads=[]
    def transport(url,**kwargs):
        downloads.append(url)
        if "bad" in url and failing:raise httpx.ConnectTimeout("")
        return image_response(url)
    make_runner,request,calls=fixture(tmp_path,transport)
    first=make_runner().run(request)
    for path in (first.task_dir/"media_state").glob("*.json"):path.unlink()
    checkpoint=load_checkpoint(first.checkpoint_path).model_dump(mode="json")
    for field in ("retryable_candidate_ids","x_query_count","media_cache_version"):checkpoint.pop(field)
    for value in checkpoint["candidates"]:
        for field in ("download_status","download_error_code","downloaded_url","media_source_url","expected_width","expected_height"):
            value.pop(field)
    # This explicitly exercises a historical JSON task rather than interpreting
    # the new SQLite database bytes as JSON.
    first.checkpoint_path.unlink()
    (first.task_dir / "checkpoint.json").write_text(json.dumps(checkpoint),encoding="utf-8")
    failing=False
    downloads.clear()
    second=make_runner().run(replace(request,resume=True,task_dir=first.task_dir))
    assert second.status=="completed" and len(downloads)==1 and len(calls)==1


def test_cancel_before_any_api_dispatch_still_queries_once_on_resume(monkeypatch,tmp_path):
    import galgame_news.pipeline.network_runtime as runtime
    import galgame_news.discovery.x_api as api
    paid_calls=[]
    def handler(request):
        paid_calls.append(str(request.url))
        return httpx.Response(200,json={"extended_entities":{"media":[{
            "type":"photo","media_url_https":"https://pbs.twimg.com/media/good.jpg"}]}})
    monkeypatch.setattr(runtime,"AsyncSocialDataTweetTransport",lambda **kwargs:
        AsyncSocialDataTweetTransport(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(api,"CredentialStore",lambda:SimpleNamespace(get_socialdata_api_key=lambda:"fixture-key"))
    make_runner,request,_=fixture(tmp_path,image_response)
    def actual_runner():
        value=make_runner()
        value.adapter_factory=None
        return value
    token=CancellationToken()
    def sink(event):
        if event.kind=="resolve_completed":token.cancel()
    first=actual_runner().run(request,event_sink=sink,cancellation_token=token)
    assert first.status=="cancelled" and not paid_calls
    second=actual_runner().run(replace(request,resume=True,task_dir=first.task_dir))
    assert len(paid_calls)==1 and second.status=="completed"
    assert second.result.all_candidates[0].download_status=="downloaded"
