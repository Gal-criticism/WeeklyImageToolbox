from __future__ import annotations

from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

from PIL import Image
import pytest

from galgame_news.domain import (
    CollectionResult, EventType, ImageCandidate, ImageNeed, Issue, IssueDraft, NewsDraft, NewsItem,
    SourceRef, SourceType,
)
from galgame_news.pipeline import PipelineRunner, TaskRequest


class _Parser:
    def __init__(self, source_urls=None):
        self.source_urls = source_urls or ["https://www.getchu.com/feature/"]

    def parse(self, path, issue_id):
        return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[
            NewsDraft(sequence=1, section="周边", title="School Days 商品", body="官方周边",
                      source_urls=self.source_urls)
        ])


class _Analyzer:
    def analyze(self, draft):
        entry = draft.entries[0]
        return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[NewsItem(
            issue_id=draft.issue_id, sequence=entry.sequence, section=entry.section,
            title=entry.title, body=entry.body, source_urls=entry.source_urls,
            game_names=["School Days"], event_type=EventType.GOODS, image_need=ImageNeed.GENERIC_EDITORIAL,
        )])


class _Resolver:
    def __init__(self, urls):
        self.urls = list(urls)

    def resolve(self, _news):
        return [SourceRef(url=url, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE,
                          root_url=url) for url in self.urls]


def _image_bytes(image_format="JPEG", size=(1200, 900)):
    buffer = BytesIO()
    Image.new("RGB", size, "navy").save(buffer, format=image_format)
    return buffer.getvalue()


def _runner(tmp_path, pages, image_urls=(), source_urls=None, *, parser=None, resolver=None,
            image_size=(1200, 900), image_sizes=None, failed_images=()):
    requests = []

    def source_transport(url, **_kwargs):
        requests.append(url)
        status, content = pages.get(url, (404, ""))
        return type("Response", (), {"text": content, "content": content.encode(),
            "status_code": status, "url": url, "headers": {"content-type": "text/html"}})()

    def image_transport(url, **_kwargs):
        assert url in image_urls, f"unexpected image request: {url}"
        if url in failed_images:
            raise OSError("fixture download failure")
        image_format = "PNG" if url.lower().endswith(".png") else "JPEG"
        content_type = "image/png" if image_format == "PNG" else "image/jpeg"
        size = (image_sizes or {}).get(url, image_size)
        return type("Response", (), {"content": _image_bytes(image_format, size), "headers": {"content-type": content_type},
            "status_code": 200, "url": url})()

    urls = list(source_urls if source_urls is not None else pages)
    runner = PipelineRunner(parser=parser or _Parser(), analyzer=_Analyzer(), resolver=resolver or _Resolver(urls),
                            source_transport=source_transport, image_transport=image_transport)
    return runner, requests


@pytest.mark.parametrize("image_mode", ["undersized", "download_failed"])
def test_unqualified_initial_goods_image_triggers_injected_search_and_indexes_search_result(tmp_path, image_mode):
    from galgame_news.discovery.resolver import DefaultSourceResolver

    listing = "https://www.getchu.com/feature/"
    detail = "https://www.getchu.com/soft.phtml?id=42"
    searched = "https://www.getchu.com/soft.phtml?id=77"
    initial_image = "https://www.getchu.com/images/preview.jpg"
    searched_image = "https://www.getchu.com/images/full.jpg"
    pages = {
        listing: (200, '<div class="ec-productRole__listItem"><a href="/soft.phtml?id=42">School Days acrylic figure</a></div>'),
        detail: (200, '<h1>School Days acrylic figure</h1><div class="gallery"><a href="/images/preview.jpg">original</a><img src="/images/preview.jpg" alt="School Days acrylic figure"></div>'),
        searched: (200, '<h1>School Days acrylic figure</h1><div class="gallery"><a href="/images/full.jpg"><img src="/images/full.jpg" alt="School Days acrylic figure"></a></div>'),
    }
    search_calls = []
    resolver = DefaultSourceResolver(search_provider=lambda _news: search_calls.append(True) or [
        SourceRef(url=searched, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE)])
    if image_mode == "undersized":
        image_size, failed = (1200, 900), ()
        image_sizes = {initial_image: (400, 300), searched_image: (1200, 900)}
    else:
        image_size, failed = (1200, 900), (initial_image,)
        image_sizes = None
    runner, requested = _runner(tmp_path, pages, [initial_image, searched_image], [listing], resolver=resolver,
                                 image_size=image_size, image_sizes=image_sizes, failed_images=failed)
    result = _run(tmp_path, runner, image_mode)

    assert result.status == "completed"
    assert search_calls == [True]
    assert searched in requested
    searched_candidates = [c for c in result.pipeline_result.candidates if c.source_url == searched]
    assert any(c.original_path and Path(c.original_path).is_file()
               and (c.original_width or 0) >= 800 and (c.original_height or 0) >= 600
               for c in searched_candidates)
    assert any(path.is_file() for path in (tmp_path / "out").rglob("final/images/**/*.jpg"))


def test_x_photo_candidate_does_not_suppress_goods_search(tmp_path):
    from galgame_news.discovery.resolver import DefaultSourceResolver

    listing = "https://www.getchu.com/feature/"
    searched = "https://www.getchu.com/soft.phtml?id=77"
    image = "https://www.getchu.com/images/full.jpg"
    x_photo = "https://www.getchu.com/images/x-photo.jpg"
    x_status = "https://x.com/studio/status/123"
    pages = {listing: (200, "<p>no product listing</p>"),
             searched: (200, '<h1>School Days acrylic figure</h1><div class="gallery"><img src="/images/full.jpg" alt="School Days acrylic figure"></div>')}
    search_calls = []
    x_candidates = []

    class XCandidateRunner(PipelineRunner):
        async def _resolve_async(self, news, request):
            return [SourceRef(url=listing, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE),
                    SourceRef(url=x_status, domain="x.com", source_type=SourceType.OFFICIAL_X)]

        def _collect(self, news, source, *, request=None):
            if source.url == x_status:
                candidate = ImageCandidate(
                    news_id=news.id, image_url=x_photo, source_url=x_status,
                    source_type=SourceType.OFFICIAL_X, downloadable=True,
                    fetched_at=datetime.now(timezone.utc), signals={"x_api_photo": True},
                )
                x_candidates.append(candidate)
                return CollectionResult(candidates=[candidate])
            return super()._collect(news, source, request=request)

    resolver = DefaultSourceResolver(search_provider=lambda _news: search_calls.append(True) or [
        SourceRef(url=searched, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE)])
    runner, requested = _runner(tmp_path, pages, [image, x_photo], [listing], parser=_Parser([x_status]), resolver=resolver)
    runner.__class__ = XCandidateRunner
    result = _run(tmp_path, runner, "x-photo")

    assert result.status == "completed"
    assert search_calls == [True]
    assert searched in requested
    assert any(c.source_url == searched and c.original_path for c in result.pipeline_result.candidates)
    assert len(x_candidates) == 1
    assert x_candidates[0].signals.get("x_api_photo") is True
    assert x_candidates[0].download_status == "downloaded"
    assert x_candidates[0].original_path and Path(x_candidates[0].original_path).is_file()


def test_initial_goods_pages_exhaust_shared_budget_before_search_result_page(tmp_path):
    from galgame_news.discovery.resolver import DefaultSourceResolver

    urls = [f"https://www.getchu.com/feature/{index}" for index in range(20)]
    pages = {url: (200, "<p>No related product content</p>") for url in urls}
    search_calls = []
    searched = "https://www.getchu.com/soft.phtml?id=77"
    resolver = DefaultSourceResolver(search_provider=lambda _news: search_calls.append(True) or [
        SourceRef(url=searched, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE)])
    runner, requested = _runner(tmp_path, pages, source_urls=urls, parser=_Parser(urls), resolver=resolver)
    result = _run(tmp_path, runner, "shared-budget")

    assert result.status == "completed"
    assert search_calls == []
    assert searched not in requested
    assert any(failure.code == "goods_search_budget_exhausted" for failure in result.pipeline_result.failures)


def _run(tmp_path, runner, issue="goods"):
    source = tmp_path / "fixture.docx"
    source.write_bytes(b"offline fixture")
    return runner.run(TaskRequest(input_path=source, issue_id=issue, output_dir=tmp_path / "out",
                                  no_videos=True, max_images=10))


@pytest.mark.parametrize(("extension", "expected_format"), [("jpg", "JPEG"), ("png", "PNG")])
def test_runner_expands_related_getchu_detail_and_saves_decodable_original_image_with_chain(tmp_path, extension, expected_format):
    listing = "https://www.getchu.com/feature/"
    detail = "https://www.getchu.com/soft.phtml?id=42"
    image = f"https://www.getchu.com/images/school-days-large.{extension}"
    pages = {
        listing: (200, '<div class="ec-productRole__listItem"><a href="/soft.phtml?id=42">School Days acrylic figure</a></div>'
                        '<div class="ec-productRole__listItem"><a href="/soft.phtml?id=99">Other Work figure</a></div>'
                        '<div class="ranking"><a href="/soft.phtml?id=100">School Days ranking</a><img src="/images/ranking.jpg"></div>'),
        detail: (200, f'<h1>School Days acrylic figure</h1><div class="gallery"><a href="/images/school-days-large.{extension}"><img src="/images/school-days-large.{extension}" alt="School Days acrylic figure"></a></div>'),
    }
    runner, requested = _runner(tmp_path, pages, [image], [listing])
    result = _run(tmp_path, runner)

    assert result.status == "completed"
    assert requested == [listing, detail]
    candidates = result.pipeline_result.candidates
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_url == detail
    assert candidate.parent_source_url == detail
    assert candidate.original_path and Path(candidate.original_path).is_file()
    with Image.open(candidate.original_path) as saved:
        assert saved.format == expected_format and saved.size == (1200, 900)
    evidence = candidate.evidence[0]
    assert evidence.source_chain == [listing, detail]


def test_goods_page_budget_caps_unique_pages_at_sixteen(tmp_path):
    urls = [f"https://www.getchu.com/feature/{index}" for index in range(20)]
    pages = {url: (200, "<p>No related product content</p>") for url in urls}
    runner, requested = _runner(tmp_path, pages, source_urls=urls)
    result = _run(tmp_path, runner, "budget")

    assert result.status == "completed"
    assert len(set(requested)) <= 16
    assert any(failure.code == "goods_page_budget" for failure in result.pipeline_result.failures)
    assert result.pipeline_result.candidates == []


def test_goods_navigation_stops_at_three_child_levels(tmp_path):
    pages = {
        "https://www.getchu.com/feature/": (200, '<div class="ec-productRole__listItem"><a href="/soft.phtml?id=42">School Days goods</a></div>'),
    }
    for index in range(42, 46):
        url = f"https://www.getchu.com/soft.phtml?id={index}"
        next_link = f'<a href="/soft.phtml?id={index + 1}">School Days related goods</a>' if index < 46 else ""
        pages[url] = (200, f"<h1>School Days goods {index}</h1>{next_link}")
    runner, requested = _runner(tmp_path, pages, source_urls=["https://www.getchu.com/feature/"])
    result = _run(tmp_path, runner, "depth")

    assert result.status == "completed"
    assert len(set(requested)) <= 4
    assert any(failure.code == "goods_depth_budget" for failure in result.pipeline_result.failures)


def test_matching_goods_result_skips_conditional_broad_search_fallback(tmp_path):
    from galgame_news.discovery.resolver import DefaultSourceResolver

    listing = "https://www.getchu.com/feature/"
    detail = "https://www.getchu.com/soft.phtml?id=42"
    image = "https://www.getchu.com/images/school-days-large.jpg"
    pages = {
        listing: (200, '<div class="ec-productRole__listItem"><a href="/soft.phtml?id=42">School Days acrylic figure</a></div>'),
        detail: (200, '<h1>School Days acrylic figure</h1><div class="gallery"><a href="/images/school-days-large.jpg"><img src="/images/school-days-large.jpg"></a></div>'),
    }
    resolver = DefaultSourceResolver()
    fallback_calls = []
    resolver.resolve = lambda _news: [SourceRef(url=listing, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE)]
    resolver.search_provider = lambda _news: fallback_calls.append(True) or [
        SourceRef(url="https://example.org/work", domain="example.org", source_type=SourceType.OFFICIAL_SITE),
        SourceRef(url="https://x.com/other/status/1", domain="x.com", source_type=SourceType.OFFICIAL_X),
    ]
    runner, _ = _runner(tmp_path, pages, [image], [listing], resolver=resolver)
    result = _run(tmp_path, runner, "fallback")

    assert result.status == "completed"
    assert fallback_calls == [], [(candidate.signals.get("goods_association"), candidate.signals.get("goods_original")) for candidate in result.pipeline_result.candidates]
    assert len(result.pipeline_result.candidates) == 1


def test_saved_x_entities_expand_goods_url_without_socialdata_lookup(tmp_path):
    listing = "https://www.getchu.com/feature/"
    detail = "https://www.getchu.com/soft.phtml?id=42"
    image = "https://www.getchu.com/images/school-days-large.jpg"
    x_status = "https://x.com/studio/status/123456"
    pages = {
        listing: (200, "<p>no listing links</p>"),
        detail: (200, '<h1>School Days acrylic figure</h1><div class="gallery"><img src="/images/school-days-large.jpg"></div>'),
    }

    class CacheRunner(PipelineRunner):
        async def _resolve_async(self, news, request):
            # The runner clears its cache at task start; model a response already
            # saved for this news before collection begins.
            self._socialdata_response_cache["123456"] = {"entities": {"urls": [
                {"expanded_url": detail}
            ]}}
            return [SourceRef(url=listing, domain="getchu.com", source_type=SourceType.OFFICIAL_SITE)]

    runner = CacheRunner(parser=_Parser([x_status]), analyzer=_Analyzer(), resolver=_Resolver([listing]),
                         source_transport=lambda url, **_: type("Response", (), {
            "text": pages[url][1], "content": pages[url][1].encode(), "status_code": pages[url][0],
                             "url": url, "headers": {"content-type": "text/html"}})(),
                         image_transport=lambda url, **_: type("Response", (), {
                             "content": _image_bytes(), "headers": {"content-type": "image/jpeg"}, "status_code": 200, "url": url})())
    result = _run(tmp_path, runner, "cache")

    assert result.status == "completed"
    assert len(result.pipeline_result.candidates) == 1
    assert result.pipeline_result.candidates[0].parent_source_url == detail


def test_runner_continues_after_forbidden_service_and_region_restriction_without_fake_images(tmp_path):
    forbidden = "https://www.getchu.com/feature/403"
    regional = "https://www.getchu.com/feature/region"
    unavailable = "https://www.getchu.com/feature/503"
    good = "https://www.getchu.com/soft.phtml?id=42"
    image = "https://www.getchu.com/images/good.jpg"
    pages = {
        forbidden: (403, "forbidden"),
        regional: (200, "<p>Not available in your region</p>"),
        unavailable: (503, "service unavailable"),
        good: (200, '<h1>School Days figure</h1><div class="gallery"><img src="/images/good.jpg" alt="School Days figure"></div>'),
    }
    runner, requested = _runner(tmp_path, pages, [image], [forbidden, regional, unavailable, good])
    result = _run(tmp_path, runner, "continued")

    assert result.status == "completed"
    assert set(requested) == {forbidden, regional, unavailable, good}
    assert all(requested.count(url) >= 1 for url in (forbidden, regional, unavailable, good))
    assert [candidate.source_url for candidate in result.pipeline_result.candidates] == [good]
    codes = {failure.code for failure in result.pipeline_result.failures}
    assert "goods_access_denied" in codes
    assert "goods_region_restricted" in codes
    assert "goods_http_error" in codes
