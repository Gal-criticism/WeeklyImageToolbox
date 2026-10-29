import asyncio
from dataclasses import dataclass, field

import httpx
import pytest

from galgame_news.discovery.async_http import AsyncHttpClient, _NoCookieJar
from galgame_news.discovery.http import ResponseTooLarge, UnsafeUrlError
from galgame_news.pipeline.contracts import CancellationToken


PUBLIC = lambda _host: ["93.184.216.34"]


@dataclass
class Response:
    url: str
    content: bytes = b"ok"
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def run(coro):
    return asyncio.run(coro)


def test_dns_workers_cannot_hold_process_exit_after_request_timeout():
    import threading

    async def scenario():
        release = threading.Event()

        def blocked(_host):
            release.wait(3)
            return ["93.184.216.34"]

        client = AsyncHttpClient(timeout=0.02, resolver=blocked, concurrency=1)
        try:
            with pytest.raises(httpx.TimeoutException):
                await client.get("https://public.example/")
            await asyncio.wait_for(client.close(), 0.2)
            assert all(thread.daemon for thread in client._executor._threads)
        finally:
            release.set()

    run(scenario())


def test_response_body_read_obeys_timeout(tmp_path):
    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b"never"

    async def scenario():
        client = AsyncHttpClient(resolver=PUBLIC, timeout=0.02, max_retries=1)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=StalledBody())))
        try:
            with pytest.raises(httpx.TimeoutException):
                await asyncio.wait_for(client.download(
                    "https://public.example/image", tmp_path / "body.part"), 0.3)
        finally:
            await client.close()

    run(scenario())


def test_exhausted_http_error_is_not_cached_and_later_request_recovers():
    async def scenario():
        calls = []
        async def transport(url, **kwargs):
            calls.append(url)
            return Response(url, status_code=429 if len(calls) == 1 else 200)
        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, max_retries=1) as client:
            assert (await client.get("https://public.example/")).status_code == 429
            assert (await client.get("https://PUBLIC.example")).status_code == 200
            assert (await client.get("https://public.example/#gallery")).status_code == 200
            assert len(calls) == 2
    run(scenario())


def test_singleflight_cache_normalizes_fragment_and_cookie_state():
    async def scenario():
        calls = []

        async def transport(url, **kwargs):
            calls.append((url, kwargs["headers"].get("cookie")))
            await asyncio.sleep(0.02)
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC) as client:
            a, b = await asyncio.gather(
                client.get("https://public.example/a#one", cookies={"age": "yes"}),
                client.get("https://public.example/a#two", cookies={"age": "yes"}),
            )
            assert a is b
            await client.get("https://public.example/a", cookies={"age": "no"})
            await client.get("https://public.example/a", cookies={"age": "yes"})
            assert calls == [
                ("https://public.example/a", "age=yes"),
                ("https://public.example/a", "age=no"),
            ]
            assert client.stats.network_requests == 2
            assert client.stats.cache_hits == 2

    run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_request():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def transport(url, **_):
            started.set()
            await release.wait()
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC) as client:
            one = asyncio.create_task(client.get("https://public.example/a"))
            await started.wait()
            two = asyncio.create_task(client.get("https://public.example/a"))
            await asyncio.sleep(0)
            one.cancel()
            with pytest.raises(asyncio.CancelledError):
                await one
            release.set()
            assert (await two).content == b"ok"
            assert client.stats.network_requests == 1

    run(scenario())


def test_redirect_checks_private_and_does_not_forward_cookies():
    async def scenario():
        calls = []

        async def transport(url, **kwargs):
            calls.append((url, kwargs["headers"].get("cookie")))
            if url.endswith("/start"):
                return Response(url, b"", 302, {"location": "https://other.example/final"})
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC) as client:
            result = await client.get("https://public.example/start", cookies={"age": "yes"})
            assert result.url == "https://other.example/final"
            assert calls == [
                ("https://public.example/start", "age=yes"),
                ("https://other.example/final", None),
            ]

        async def private_redirect(url, **_):
            return Response(url, b"", 302, {"location": "http://127.0.0.1/secret"})

        async with AsyncHttpClient(transport=private_redirect, resolver=PUBLIC) as client:
            with pytest.raises(UnsafeUrlError):
                await client.get("https://public.example/start")

    run(scenario())


def test_rejects_credentials_and_injected_final_private_url():
    async def scenario():
        async with AsyncHttpClient(transport=lambda url, **_: Response("http://127.0.0.1/secret"), resolver=PUBLIC) as client:
            with pytest.raises(UnsafeUrlError):
                await client.get("https://user:pass@public.example/a")
            with pytest.raises(UnsafeUrlError):
                await client.get("https://public.example/a")

    run(scenario())


def test_retry_and_download_cleanup(tmp_path):
    async def scenario():
        calls = 0

        async def transport(url, **_):
            nonlocal calls
            calls += 1
            return Response(url, b"image", 429 if calls == 1 else 200)

        target = tmp_path / "image.tmp"
        async with AsyncHttpClient(transport=transport, resolver=PUBLIC) as client:
            response = await client.download("https://public.example/image", target)
            assert response.status_code == 200
            assert response.content == b""
            assert target.read_bytes() == b"image"
            assert calls == 2

        async with AsyncHttpClient(transport=lambda url, **_: Response(url, b"too big"), resolver=PUBLIC) as client:
            with pytest.raises(ResponseTooLarge):
                await client.download("https://public.example/image", target, max_response_bytes=3)
            assert not target.exists()

    run(scenario())


def test_real_stream_limit_and_timeout_with_mock_transport(tmp_path):
    async def scenario():
        target = tmp_path / "image.tmp"

        async def handler(request):
            return httpx.Response(200, content=b"12345")

        async with AsyncHttpClient(resolver=PUBLIC, max_retries=1) as client:
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
            with pytest.raises(ResponseTooLarge):
                await client.download("https://public.example/image", target, max_response_bytes=4)
            assert not target.exists()

    run(scenario())


def test_pause_blocks_dispatch_then_cancel_aborts():
    async def scenario():
        token = CancellationToken()
        token.pause()
        calls = []

        async def transport(url, **_):
            calls.append(url)
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, cancellation=token) as client:
            pending = asyncio.create_task(client.get("https://public.example/a"))
            await asyncio.sleep(0.1)
            assert calls == []
            token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert calls == []

    run(scenario())


def test_per_host_concurrency():
    async def scenario():
        active = 0
        maximum = 0

        async def transport(url, **_):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            active -= 1
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, concurrency=4, per_host_concurrency=2) as client:
            await asyncio.gather(*(client.get(f"https://public.example/{i}") for i in range(6)))
            assert maximum == 2

    run(scenario())


def test_injected_transport_timeout():
    async def scenario():
        async def transport(url, **_):
            await asyncio.sleep(1)
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, timeout=0.02, max_retries=1) as client:
            with pytest.raises(httpx.TimeoutException):
                await client.get("https://public.example/slow")

    run(scenario())


def test_pooled_client_does_not_retain_response_cookies():
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(request.headers.get("cookie"))
            return httpx.Response(200, content=b"ok", headers={"set-cookie": "session=secret; Path=/"})

        async with AsyncHttpClient(resolver=PUBLIC) as client:
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
            client._client._cookies = _NoCookieJar()
            await client.get("https://public.example/one")
            await client.get("https://public.example/two")
            assert seen == [None, None]

    run(scenario())


def test_declared_length_rejected_before_small_body(tmp_path):
    async def scenario():
        target = tmp_path / "image.tmp"
        async with AsyncHttpClient(
            transport=lambda url, **_: Response(url, b"x", headers={"content-length": "999"}),
            resolver=PUBLIC,
            max_retries=1,
        ) as client:
            with pytest.raises(ResponseTooLarge):
                await client.download("https://public.example/image", target, max_response_bytes=4)
            assert not target.exists()

    run(scenario())


def test_dns_resolution_does_not_block_event_loop():
    async def scenario():
        heartbeat = asyncio.Event()

        def slow_resolver(_host):
            import time
            time.sleep(0.1)
            return ["93.184.216.34"]

        async with AsyncHttpClient(transport=lambda url, **_: Response(url), resolver=slow_resolver) as client:
            task = asyncio.create_task(client.get("https://public.example/a"))
            asyncio.get_running_loop().call_later(0.02, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.06)
            await task

    run(scenario())


def test_dns_validation_obeys_network_timeout_and_releases_blocked_resolver():
    import threading
    import time

    async def scenario():
        entered = threading.Event()
        release = threading.Event()

        def blocked_resolver(_host):
            entered.set()
            release.wait()
            return ["93.184.216.34"]

        client = AsyncHttpClient(timeout=0.02, resolver=blocked_resolver)
        task = asyncio.create_task(client._validate_url("https://public.example/"))
        try:
            assert await asyncio.to_thread(entered.wait, 0.2)
            started = time.monotonic()
            with pytest.raises(httpx.TimeoutException):
                await asyncio.wait_for(task, timeout=0.3)
            assert time.monotonic() - started < 0.2
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await client.close()

    run(scenario())


def test_transport_timeout_does_not_wait_for_cancellation_cleanup_forever():
    import time

    async def scenario():
        client = AsyncHttpClient(timeout=0.02, resolver=PUBLIC)
        cancellation_seen = asyncio.Event()
        cleanup_finished = asyncio.Event()
        release_cleanup = asyncio.Event()
        task_holder = []
        loop_errors = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def cancellation_resistant():
            task_holder.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                try:
                    await release_cleanup.wait()
                finally:
                    cleanup_finished.set()
                raise RuntimeError("late cleanup failure")

        started = time.monotonic()
        try:
            with pytest.raises(httpx.TimeoutException):
                await asyncio.wait_for(client._wait(cancellation_resistant(), timeout=0.02), timeout=0.12)
            assert time.monotonic() - started < 0.11
            assert cancellation_seen.is_set()
            assert task_holder and not task_holder[0].done()
            release_cleanup.set()
            await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)
            await asyncio.sleep(0)
            assert task_holder[0].done()
        finally:
            release_cleanup.set()
            loop.set_exception_handler(old_handler)
            await client.close()
        assert not loop_errors

    run(scenario())


def test_failed_singleflight_allows_later_recovery():
    async def scenario():
        calls = 0

        async def transport(url, **_):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            if calls == 1:
                raise httpx.TimeoutException("temporary timeout")
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, max_retries=1) as client:
            results = await asyncio.gather(
                client.get("https://public.example/a"),
                client.get("https://public.example/a#fragment"),
                return_exceptions=True,
            )
            assert all(isinstance(value, httpx.TimeoutException) for value in results)
            assert (await client.get("https://public.example/a")).content == b"ok"
            assert calls == 2

    run(scenario())


def test_busy_host_does_not_block_other_host():
    async def scenario():
        first_started = asyncio.Event()
        release = asyncio.Event()
        other_started = asyncio.Event()

        async def transport(url, **_):
            if "host-a" in url and url.endswith("/1"):
                first_started.set()
                await release.wait()
            if "host-b" in url:
                other_started.set()
            return Response(url)

        async with AsyncHttpClient(transport=transport, resolver=PUBLIC, concurrency=2, per_host_concurrency=1) as client:
            first = asyncio.create_task(client.get("https://host-a.example/1"))
            await first_started.wait()
            waiting = asyncio.create_task(client.get("https://host-a.example/2"))
            other = asyncio.create_task(client.get("https://host-b.example/1"))
            await asyncio.wait_for(other_started.wait(), 0.5)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            release.set()
            await asyncio.gather(first, other)

    run(scenario())


def test_cancel_interrupts_native_header_wait():
    async def scenario():
        token = CancellationToken()
        started = asyncio.Event()

        async def handler(_request):
            started.set()
            await asyncio.sleep(1)
            return httpx.Response(200, content=b"ok")

        async with AsyncHttpClient(resolver=PUBLIC, cancellation=token, max_retries=1) as client:
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
            request = asyncio.create_task(client.get("https://public.example/a"))
            await started.wait()
            token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 0.5)

    run(scenario())
