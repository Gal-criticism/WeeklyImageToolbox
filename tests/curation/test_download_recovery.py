import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import httpx
from PIL import Image

from galgame_news.config import load_config
from galgame_news.curation.download import ImageDownloader
from galgame_news.discovery.async_http import AsyncHttpClient
from galgame_news.domain import ImageCandidate, SourceType


def candidate(name):
    return ImageCandidate(news_id="news", image_url=f"https://pbs.twimg.com/media/{name}.jpg?name=orig",
                          source_url="https://x.com/studio/status/123", source_type=SourceType.OFFICIAL_X,
                          signals={"x_api_photo": True}, fetched_at=datetime.now(timezone.utc))


def response(url):
    image = BytesIO()
    Image.new("RGB", (800, 600), "purple").save(image, "JPEG")
    return SimpleNamespace(url=url, content=image.getvalue(), headers={"content-type": "image/jpeg"}, status_code=200)


def test_empty_exception_does_not_discard_success_and_reports_each_image(tmp_path):
    values = [candidate("good"), candidate("bad")]
    saved = []
    async def transport(url, **kwargs):
        if "bad" in url:
            raise httpx.ConnectTimeout("")
        return response(url)
    async def run():
        async with AsyncHttpClient(transport=transport, resolver=lambda _: ["93.184.216.34"], max_retries=1) as client:
            with ThreadPoolExecutor(max_workers=2) as executor:
                return await ImageDownloader(load_config()).download_async(values, tmp_path, client=client,
                    executor=executor, on_result=lambda value, error: saved.append(value.id))
    result, failures = asyncio.run(run())
    assert len(result) == 2 and len(saved) == 2
    assert values[0].download_status == "downloaded"
    assert values[1].download_status == "retryable_failed"
    assert failures[0].message.strip()
    assert values[1].image_url and values[1].source_url


def test_original_fallback_has_one_attempt_and_keeps_raw_link(tmp_path):
    value = candidate("photo")
    value.media_source_url = "https://pbs.twimg.com/media/photo.jpg"
    calls = []
    class Client:
        async def download(self, url, target, **kwargs):
            calls.append((url, kwargs))
            if "name=orig" in url:
                raise httpx.ConnectTimeout("")
            result = response(url)
            target.write_bytes(result.content)
            return result
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            return await ImageDownloader(load_config()).download_async([value], tmp_path, client=Client(), executor=executor)
    result, failures = asyncio.run(run())
    assert not failures and result[0].download_status == "downloaded"
    assert calls[1][1]["max_retries"] == 1
    assert value.downloaded_url == value.media_source_url
    assert value.signals["x_original_fallback"] is True


def test_resume_reuses_verified_source_and_clears_download_failure(tmp_path):
    value = candidate("photo")
    downloader = ImageDownloader(load_config(), transport=lambda url, **kwargs: response(url))
    downloader.client.resolver = lambda _: ["93.184.216.34"]
    downloader.download([value], tmp_path)
    value.signals["invalid_reason"] = "download_error"
    value.download_status = "retryable_failed"
    def unexpected(*args, **kwargs):
        raise AssertionError("must reuse the verified original")
    resumed = ImageDownloader(load_config(), transport=unexpected)
    resumed.client.resolver = lambda _: ["93.184.216.34"]
    values, failures = resumed.download([value], tmp_path)
    assert not failures and values[0].download_status == "downloaded"
    assert "invalid_reason" not in value.signals


def test_old_x_url_upgrades_for_request_without_changing_id(tmp_path):
    value=candidate("photo")
    value.image_url="https://pbs.twimg.com/media/photo.jpg"
    before=value.id
    urls=[]
    def transport(url,**kwargs):
        urls.append(url)
        return response(url)
    downloader = ImageDownloader(load_config(), transport=transport)
    downloader.client.resolver = lambda _: ["93.184.216.34"]
    downloader.download([value], tmp_path)
    assert value.id==before
    assert urls==["https://pbs.twimg.com/media/photo.jpg?name=orig"]
    assert value.downloaded_url==urls[0]
