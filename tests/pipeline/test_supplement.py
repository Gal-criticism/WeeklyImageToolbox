from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image
import pytest

from galgame_news.config import load_config
from galgame_news.discovery.http import UnsafeUrlError
from galgame_news.discovery.async_http import AsyncHttpClient
from galgame_news.domain import CollectionResult, ImageCandidate, Issue, NewsItem, SourceType
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest


SOURCE_URL = "https://93.184.216.34/gallery"
GOOD_IMAGE = "https://93.184.216.34/good.jpg"
BAD_IMAGE = "https://93.184.216.34/bad.jpg"


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (800, 600), "cyan").save(buffer, format="JPEG")
    return buffer.getvalue()


def _fixture(tmp_path, handler, *, source_url=SOURCE_URL, request_options=None, candidates=None, section="新作"):
    task_root = tmp_path / "task"
    task_root.mkdir()
    news = NewsItem(
        issue_id="261",
        sequence=2,
        section=section,
        title="银河边境 CG 更新",
        body="原始新闻正文",
        game_names=["银河边境"],
        source_urls=["https://news.example.invalid/original"],
    )
    other = NewsItem(
        issue_id="261", sequence=3, section="汉化", title="另一条新闻", body="不要处理",
    )
    checkpoint = {
        "issue_id": "261",
        "issue": {
            "issue_id": "261",
            "input_path": str(task_root / "weekly.docx"),
            "news_items": [news.model_dump(mode="json"), other.model_dump(mode="json")],
        },
    }
    checkpoint_path = task_root / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    before_checkpoint = checkpoint_path.read_bytes()

    calls = []

    class Adapter:
        def collect(self, item, source, context):
            calls.append((item.id, source.url, item.title, tuple(item.source_urls)))
            values = candidates or [GOOD_IMAGE, BAD_IMAGE]
            return CollectionResult(candidates=[
                ImageCandidate(
                    news_id=item.id,
                    image_url=url,
                    source_url=source.url,
                    source_type=SourceType.OFFICIAL_SITE,
                    nearby_text="银河边境 CG 更新",
                    fetched_at=datetime.now(timezone.utc),
                )
                for url in values
            ])

    mock_transport = httpx.MockTransport(handler)

    def image_transport(url, *, headers=None, **_kwargs):
        return mock_transport.handle_request(httpx.Request("GET", url, headers=headers))

    config = load_config()
    config.network.max_retries = 1
    config.filters.min_width = 100
    config.filters.min_height = 100
    runner = PipelineRunner(
        config=config,
        adapter_factory=lambda *_args: Adapter(),
        image_transport=image_transport,
    )
    options = {"no_videos": True}
    options.update(request_options or {})
    request = TaskRequest(
        input_path=task_root / "weekly.docx",
        issue_id="261",
        output_dir=task_root,
        **options,
    )
    return runner, task_root, news, request, calls, before_checkpoint


def _image_handler(request):
    return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"}, request=request)


def test_supplement_adds_images_to_pending_review_manifest_without_selecting_them(tmp_path):
    runner, task_root, news, request, calls, before = _fixture(
        tmp_path, _image_handler, candidates=[GOOD_IMAGE],
    )

    result = runner.supplement_news(
        task_root=task_root,
        issue_id="261",
        news_id=news.id,
        source_url=SOURCE_URL,
        request=request,
    )

    manifest = json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))
    assert result.status == "completed"
    assert result.success_count == 1 and result.failed_count == 0
    assert manifest["kind"] == "supplement"
    assert manifest["source_url"] == SOURCE_URL
    assert len(manifest["candidates"]) == 1
    assert manifest["candidates"][0]["selected"] is False
    assert manifest["candidates"][0]["curation_status"] == "unselected"
    assert manifest["candidates"][0]["download_status"] == "downloaded"
    assert calls[0][0:2] == (news.id, SOURCE_URL)
    assert (task_root / "checkpoint.json").read_bytes() == before


def test_second_supplement_attempt_retries_only_failed_download_and_reuses_success(tmp_path):
    fail_bad = True
    fetched = []

    def handler(request):
        nonlocal fail_bad
        fetched.append(str(request.url))
        if request.url.path == "/bad.jpg" and fail_bad:
            raise httpx.ConnectError("fixture failure", request=request)
        return _image_handler(request)

    runner, task_root, news, request, calls, _before = _fixture(tmp_path, handler)
    first = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url=SOURCE_URL, request=request,
    )
    assert first.status == "partial"
    assert first.success_count == 1
    assert first.failed_count == 1
    first_good = next(c for c in first.candidates if c.image_url == GOOD_IMAGE)
    first_good_path = first_good.original_path
    first_manifest = json.loads((first.path / "image_index.json").read_text(encoding="utf-8"))
    assert first_manifest["failures"]

    fetched.clear()
    fail_bad = False
    second = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url=SOURCE_URL, request=request,
    )

    assert second.status == "completed"
    assert second.success_count == 2 and second.failed_count == 0
    assert len(fetched) == 1 and fetched[0].endswith("/bad.jpg")
    second_good = next(c for c in second.candidates if c.image_url == GOOD_IMAGE)
    assert second_good.original_sha256 == first_good.original_sha256
    assert Path(first_good_path).is_file()
    assert calls == [(news.id, SOURCE_URL, news.title, (*news.source_urls, SOURCE_URL))]


def test_supplement_collects_only_requested_news_and_source_without_changing_checkpoint(tmp_path):
    runner, task_root, news, request, calls, before = _fixture(
        tmp_path, _image_handler, candidates=[GOOD_IMAGE],
    )

    runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url=SOURCE_URL, request=request,
    )

    assert calls == [(news.id, SOURCE_URL, news.title, (*news.source_urls, SOURCE_URL))]
    assert (task_root / "checkpoint.json").read_bytes() == before


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "file:///tmp/image.jpg", "https://user:pass@93.184.216.34/page"])
def test_supplement_rejects_invalid_or_private_source_urls_before_collection(tmp_path, url):
    runner, task_root, news, request, calls, _before = _fixture(tmp_path, _image_handler)

    with pytest.raises((UnsafeUrlError, ValueError)):
        runner.supplement_news(
            task_root=task_root, issue_id="261", news_id=news.id,
            source_url=url, request=request,
        )

    assert calls == []


def test_x_supplement_without_socialdata_fails_explicitly_without_paid_request(tmp_path):
    paid_requests = []

    def handler(request):
        paid_requests.append(str(request.url))
        return _image_handler(request)

    runner, task_root, news, request, calls, _before = _fixture(
        tmp_path, handler, request_options={"use_socialdata_x": False},
    )

    with pytest.raises(ValueError, match="SocialData|X"):
        runner.supplement_news(
            task_root=task_root, issue_id="261", news_id=news.id,
            source_url="https://x.com/studio/status/123", request=request,
        )

    assert paid_requests == []
    assert calls == []


def test_x_media_cache_is_shared_by_status_id_and_recovery_never_repeats_lookup(monkeypatch, tmp_path):
    import galgame_news.pipeline.network_runtime as network_runtime

    secret = "fixture-socialdata-secret"
    lookup_calls = []
    fail_media = True
    media_calls = []

    class FakeSocialDataTransport:
        request_count = 0

        def __init__(self, **_kwargs):
            pass

        async def lookup(self, status_url, *, token, timeout):
            assert token == secret
            lookup_calls.append(status_url)
            self.request_count += 1
            return {
                "id_str": "123",
                "full_text": "银河边境 CG 更新",
                "extended_entities": {"media": [{
                    "type": "photo",
                    "media_url_https": "https://pbs.twimg.com/media/supplement-photo.jpg?name=orig",
                    "original_info": {"width": 800, "height": 600},
                }]},
            }

        async def close(self):
            pass

    real_validate = AsyncHttpClient.validate_url
    real_async_validate = AsyncHttpClient._validate_url

    def validate_fixture_host(self, url):
        if (urlsplit(url).hostname or "").casefold() in {"x.com", "twitter.com", "pbs.twimg.com"}:
            return
        return real_validate(self, url)

    async def async_validate_fixture_host(self, url):
        if (urlsplit(url).hostname or "").casefold() in {"x.com", "twitter.com", "pbs.twimg.com"}:
            return
        return await real_async_validate(self, url)

    monkeypatch.setattr(network_runtime, "AsyncSocialDataTweetTransport", FakeSocialDataTransport)
    monkeypatch.setattr(AsyncHttpClient, "validate_url", validate_fixture_host)
    monkeypatch.setattr(AsyncHttpClient, "_validate_url", async_validate_fixture_host)

    def handler(request):
        nonlocal fail_media
        media_calls.append(str(request.url))
        if fail_media:
            raise httpx.ConnectError("fixture media timeout", request=request)
        return _image_handler(request)

    runner, task_root, news, request, _calls, _before = _fixture(
        tmp_path, handler, request_options={"use_socialdata_x": True},
    )
    runner.adapter_factory = None
    runner._supplement_credential_store = type(
        "MemoryCredentials", (), {"get_socialdata_api_key": lambda self: secret},
    )()

    first = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url="https://x.com/studio/status/123?utm_source=first", request=request,
    )
    assert first.status == "partial"
    assert len(lookup_calls) == 1
    assert first.failed_count == 1

    fail_media = False
    media_calls.clear()
    second = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url="https://twitter.com/another/status/123?utm_source=second", request=request,
    )

    assert second.status == "completed"
    assert len(lookup_calls) == 1
    assert lookup_calls[0] == "https://x.com/studio/status/123?utm_source=first"
    assert len(media_calls) == 1 and "supplement-photo.jpg" in media_calls[0]
    for artifact in task_root.rglob("*"):
        if artifact.is_file():
            assert secret.encode() not in artifact.read_bytes()


def test_official_source_follows_gallery_child_only_not_other_news_or_recommendations(tmp_path):
    source_url = "https://93.184.216.34/news"
    gallery_url = "https://93.184.216.34/game/gallery"
    page_calls = []
    pages = {
        source_url: """
            <html><head><title>银河边境 官方更新</title></head><body>
              <a href="/game/gallery">银河边境 CG图库</a>
              <a href="/news/other">另一条新闻</a>
              <a href="https://recommend.example/other/gallery">推荐作品图库</a>
            </body></html>
        """,
        gallery_url: """
            <html><head><title>银河边境 CG Gallery</title></head><body>
              <img src="/images/cg-01.jpg" alt="银河边境 事件 CG">
            </body></html>
        """,
    }

    def page_transport(url, **_kwargs):
        page_calls.append(url)
        return type("Response", (), {
            "status_code": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "url": url,
            "content": pages[url].encode("utf-8"),
        })()

    runner, task_root, news, request, _calls, _before = _fixture(
        tmp_path, _image_handler, source_url=source_url, candidates=[GOOD_IMAGE],
    )
    runner.adapter_factory = None
    runner.source_transport = page_transport

    result = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url=source_url, request=request,
    )

    manifest = json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))
    discovered_urls = {entry["url"] for entry in manifest["sources"]}
    assert discovered_urls == {source_url, gallery_url}
    assert set(page_calls) == {source_url, gallery_url}
    assert all("/news/other" not in url and "recommend.example" not in url for url in page_calls)
    assert manifest["news_items"][0]["title"] == news.title
    assert result.news_id == news.id


def test_imported_task_can_supplement_from_explicit_news_metadata_but_never_invents_a_title(tmp_path):
    runner, task_root, news, request, calls, _before = _fixture(
        tmp_path, _image_handler, candidates=[GOOD_IMAGE],
    )
    (task_root / "checkpoint.json").unlink()
    metadata = {
        "news_id": news.id,
        "sequence": news.sequence,
        "section": news.section,
        "title": news.title,
    }

    result = runner.supplement_news(
        task_root=task_root, issue_id="261", news_id=news.id,
        source_url=SOURCE_URL, news_item=metadata, request=request,
    )

    assert result.news_id == news.id
    assert calls[0][0:3] == (news.id, SOURCE_URL, news.title)

    with pytest.raises(ValueError, match="标题|title|字段|field"):
        runner.supplement_news(
            task_root=task_root, issue_id="261", news_id="unknown-news-id",
            source_url=SOURCE_URL,
            news_item={"news_id": "unknown-news-id", "sequence": 7, "section": "新作"},
            request=request,
        )


def test_cancelled_supplement_keeps_recoverable_manifest(tmp_path):
    token = CancellationToken()
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        token.cancel()
        return _image_handler(request)

    runner, task_root, news, request, _calls, _before = _fixture(
        tmp_path, handler, candidates=[GOOD_IMAGE],
    )

    result = runner.supplement_news(
        task_root=task_root,
        issue_id="261",
        news_id=news.id,
        source_url=SOURCE_URL,
        request=request,
        cancellation_token=token,
    )

    manifest_path = result.path / "image_index.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "cancelled"
    assert manifest["kind"] == "supplement"
    assert manifest["source_url"] == SOURCE_URL
    assert manifest["candidates"]
    assert fetched


@pytest.mark.parametrize("failure_mode", ["false", "raise"])
def test_review_asset_conversion_failure_isolated_per_candidate_and_keeps_manifest_links(
    monkeypatch, tmp_path, failure_mode,
):
    from galgame_news.delivery.output import OutputManager

    first_bytes, second_bytes = BytesIO(), BytesIO()
    Image.new("RGB", (800, 600), "cyan").save(first_bytes, format="JPEG")
    Image.new("RGB", (800, 600), "orange").save(second_bytes, format="JPEG")
    images = {GOOD_IMAGE: first_bytes.getvalue(), BAD_IMAGE: second_bytes.getvalue()}

    def handler(request):
        return httpx.Response(
            200,
            content=images[str(request.url)],
            headers={"content-type": "image/jpeg"},
            request=request,
        )

    save_review_image = OutputManager._save_review_image

    def fail_one_review_image(cls, candidate, source, root, destination):
        if candidate.image_url == BAD_IMAGE:
            if failure_mode == "raise":
                raise OSError("fixture review conversion failure")
            return False
        return save_review_image(candidate, source, root, destination)

    monkeypatch.setattr(OutputManager, "_save_review_image", classmethod(fail_one_review_image))
    runner, task_root, news, request, _calls, _before = _fixture(tmp_path, handler)

    result = runner.supplement_news(
        task_root=task_root,
        issue_id="261",
        news_id=news.id,
        source_url=SOURCE_URL,
        request=request,
    )

    manifest = json.loads((result.path / "image_index.json").read_text(encoding="utf-8"))
    candidates_by_url = {candidate["image_url"]: candidate for candidate in manifest["candidates"]}
    assert result.status == "partial"
    assert set(candidates_by_url) == {GOOD_IMAGE, BAD_IMAGE}
    assert candidates_by_url[GOOD_IMAGE]["source_url"] == SOURCE_URL
    assert Path(candidates_by_url[GOOD_IMAGE]["local_path"]).is_file()
    assert candidates_by_url[BAD_IMAGE]["source_url"] == SOURCE_URL
    assert candidates_by_url[BAD_IMAGE]["image_url"] == BAD_IMAGE
    assert Path(candidates_by_url[BAD_IMAGE]["original_path"]).is_file()
    failure = next(item for item in manifest["failures"] if item["code"] == "supplement_prepare_failed")
    assert failure["candidate_id"] == candidates_by_url[BAD_IMAGE]["id"]
    assert failure["news_id"] == news.id
    assert failure["source_url"] == BAD_IMAGE


@pytest.mark.parametrize("section", ["新作", "周边"])
@pytest.mark.parametrize(
    "source_url,expected_host",
    [
        ("https://vndb.org/v12345", "vndb.org"),
        ("https://store.steampowered.com/app/12345", "store.steampowered.com"),
    ],
)
def test_vndb_and_steam_supplements_use_localization_collection_in_x_and_z_sections(
    tmp_path, monkeypatch, section, source_url, expected_host,
):
    # The explicit supplement path is fully offline here: URL validation is
    # stubbed and the localization collector returns an empty result.
    runner, task_root, news, request, _calls, _before = _fixture(
        tmp_path, _image_handler, source_url=source_url, candidates=[], section=section,
    )
    runner.adapter_factory = None
    original_id = news.id
    observed = []

    monkeypatch.setattr(AsyncHttpClient, "validate_url", lambda self, url: None)
    monkeypatch.setattr(AsyncHttpClient, "_validate_url", lambda self, url: None)

    def collect(service, item, source, context):
        observed.append((urlsplit(source.url).hostname, item.id, item.section, item.title,
                         getattr(runner, "_supplement_mode", False)))
        return CollectionResult()

    monkeypatch.setattr("galgame_news.localization.service.LocalizationImageService.collect", collect)

    result = runner.supplement_news(
        task_root=task_root,
        issue_id="261",
        news_id=original_id,
        source_url=source_url,
        request=request,
    )

    assert result.news_id == original_id
    assert result.status == "completed"
    assert observed == [(expected_host, original_id, section, news.title, True)]
    assert news.id == original_id
    assert news.section == section
    assert news.title == "银河边境 CG 更新"
    assert runner._supplement_mode is False
