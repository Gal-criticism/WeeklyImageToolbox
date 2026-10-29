from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from PIL import Image

from galgame_news.concurrency import DaemonTaskPool
from galgame_news.pipeline import CancellationToken
from galgame_news.pipeline.network_runtime import TaskNetworkRuntime
from galgame_news.config import load_config
from galgame_news.curation.download import ImageDownloader
from galgame_news.domain import ImageCandidate, SourceType


def _runtime(*, workers=1, limit=0.05):
    runtime = TaskNetworkRuntime.__new__(TaskNetworkRuntime)
    runtime.loop = asyncio.get_running_loop()
    runtime.runner = SimpleNamespace(config=SimpleNamespace(network=SimpleNamespace(
        news_timeout_seconds=limit, image_processing_threads=workers,
    )))
    runtime.token = CancellationToken()
    runtime._compat_slots = asyncio.Semaphore(workers)
    runtime.compat = DaemonTaskPool(max_workers=workers, thread_name_prefix="test-compat")
    runtime.processing = DaemonTaskPool(max_workers=workers, thread_name_prefix="test-processing")
    runtime._active = set()
    return runtime


def test_call_sync_bounds_blocking_work_and_slot_queue():
    async def scenario():
        runtime = _runtime(workers=1)
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(2)
            return "done"

        first = asyncio.create_task(runtime.call_sync(blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await runtime.call_sync(lambda: "queued")
        assert time.monotonic() - started < 0.5
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        runtime.compat.shutdown(wait=False, cancel_futures=True)
        runtime.processing.shutdown(wait=False, cancel_futures=True)
        release.set()

    asyncio.run(scenario())


def test_call_sync_timeout_includes_pause_wait_and_honors_cancel():
    async def scenario():
        runtime = _runtime()
        runtime.token.pause()
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await runtime.call_sync(lambda: "never")
        assert time.monotonic() - started < 0.5
        runtime.token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runtime.call_sync(lambda: "never")
        runtime.compat.shutdown(wait=False, cancel_futures=True)
        runtime.processing.shutdown(wait=False, cancel_futures=True)

    asyncio.run(scenario())


def test_submit_times_out_and_cancels_coroutine():
    async def scenario():
        runtime = _runtime()
        started, cancelled = threading.Event(), threading.Event()

        async def blocked():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def bridge():
            with pytest.raises(TimeoutError):
                runtime.submit(blocked(), timeout=0.05)

        await asyncio.to_thread(bridge)
        assert started.wait(1)
        assert cancelled.wait(1)
        runtime.compat.shutdown(wait=False, cancel_futures=True)
        runtime.processing.shutdown(wait=False, cancel_futures=True)

    asyncio.run(scenario())


def test_daemon_pool_close_does_not_wait_for_running_worker():
    pool = DaemonTaskPool(max_workers=1, thread_name_prefix="test-close")
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(2)

    pool.submit(blocked)
    assert entered.wait(1)
    started = time.monotonic()
    pool.shutdown(wait=False, cancel_futures=True)
    assert time.monotonic() - started < 0.5
    release.set()


def test_daemon_pool_submit_racing_shutdown_cannot_queue_after_sentinel():
    pool = DaemonTaskPool(max_workers=1, thread_name_prefix="test-submit-close-race")
    entered_put, release_put = threading.Event(), threading.Event()

    # Force the submit/shutdown interleaving at the queue insertion boundary
    # without replacing the queue the already-running worker is consuming.
    queue = pool._queue
    original_put = queue.put

    def delayed_put(item, *args, **kwargs):
        if item is not None:
            entered_put.set()
            assert release_put.wait(1)
        return original_put(item, *args, **kwargs)

    queue.put = delayed_put
    submitted = []
    submitter = threading.Thread(target=lambda: submitted.append(pool.submit(lambda: "done")))
    submitter.start()
    assert entered_put.wait(1)
    closer = threading.Thread(target=lambda: pool.shutdown(wait=False, cancel_futures=False))
    closer.start()
    release_put.set()
    submitter.join(1)
    closer.join(1)
    assert not submitter.is_alive()
    assert not closer.is_alive()
    assert submitted[0].result(timeout=1) == "done"


def _image_candidate():
    return ImageCandidate(news_id="n1", image_url="https://img.example/cg.jpg",
        source_url="https://site.example/gallery", source_type=SourceType.OFFICIAL_SITE,
        fetched_at=datetime.now(timezone.utc))


def test_prepare_timeout_uses_isolated_candidates(tmp_path, monkeypatch):
    config = load_config()
    config.network.news_timeout_seconds = 0.05
    downloader = ImageDownloader(config)
    entered, release = threading.Event(), threading.Event()
    original_prepare = downloader._prepare

    def blocked_prepare(candidates, directory):
        entered.set()
        release.wait(2)
        return original_prepare(candidates, directory)

    monkeypatch.setattr(downloader, "_prepare", blocked_prepare)

    class Client:
        async def download(self, *_args, **_kwargs):
            raise AssertionError("download should not start")

    async def scenario():
        candidate = _image_candidate()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(TimeoutError, match="preparation"):
                await downloader.download_async([candidate], tmp_path, client=Client(), executor=executor)
            assert await asyncio.to_thread(entered.wait, 1)
            assert candidate.download_status == "pending"
            release.set()
            await asyncio.sleep(0.05)
            assert candidate.download_status == "pending"

    asyncio.run(scenario())


def test_processing_timeout_cannot_publish_or_mutate_original_late(tmp_path, monkeypatch):
    config = load_config()
    config.network.news_timeout_seconds = 0.05
    downloader = ImageDownloader(config)
    entered, release = threading.Event(), threading.Event()
    original_validate = downloader.validator.validate

    def blocked_validate(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(downloader.validator, "validate", blocked_validate)
    image = BytesIO()
    Image.new("RGB", (800, 600), "blue").save(image, "JPEG")

    class Client:
        async def download(self, url, target, **_kwargs):
            target.write_bytes(image.getvalue())
            return type("Response", (), {"url": url, "status_code": 200,
                "headers": {"content-type": "image/jpeg"}})()

    async def scenario():
        candidate = _image_candidate()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result, failures = await downloader.download_async([candidate], tmp_path,
                client=Client(), executor=executor)
            assert await asyncio.to_thread(entered.wait, 1)
            assert failures and failures[0].code == "processing_timeout"
            assert candidate.downloadable is False
            assert not list(tmp_path.glob("*.jpg"))
            release.set()
            await asyncio.sleep(0.1)
            assert candidate.downloadable is False
            assert candidate.local_path is None
            assert not list(tmp_path.glob("*.jpg"))
            assert not list(tmp_path.glob("*.part"))
            assert result[0] is candidate

    asyncio.run(scenario())
