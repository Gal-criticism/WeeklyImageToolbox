"""Pooled asynchronous HTTP with cooperative cancellation and URL safety."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..concurrency import DaemonTaskPool
from .http import ResponseTooLarge, UnsafeUrlError, _public_url, _verified_www_fallback


@dataclass(frozen=True)
class AsyncHttpResponse:
    content: bytes
    status_code: int
    headers: Mapping[str, str]
    url: str
    encoding: str | None = None

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def text(self) -> str:
        match = re.search(r"charset\s*=\s*([^;\s]+)", self.headers.get("content-type", ""), re.I)
        encoding = self.encoding or (match.group(1).strip('"\'') if match else "utf-8")
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class AsyncHttpStats:
    network_requests: int
    cache_hits: int
    elapsed_seconds: float


class _NoCookieJar(httpx.Cookies):
    """Keep explicit request cookies out of the pooled client's state."""

    def extract_cookies(self, response: httpx.Response) -> None:
        return None


class AsyncHttpClient:
    _CANCELLATION_CLEANUP_SECONDS = 0.05

    def __init__(
        self,
        *,
        transport: Callable[..., Any] | None = None,
        timeout: float = 12.0,
        max_retries: int = 3,
        max_response_bytes: int = 5_242_880,
        user_agent: str = "WeeklyGalgameImagePrescan/2.0",
        resolver: Callable[[str], Iterable[str]] | None = None,
        concurrency: int = 4,
        per_host_concurrency: int = 2,
        cancellation: Any = None,
        trust_env: bool = True,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self.resolver = resolver
        self.cancellation = cancellation
        self.trust_env = trust_env
        self._global = asyncio.Semaphore(max(1, concurrency))
        self._host_limit = max(1, per_host_concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._client: httpx.AsyncClient | None = None
        self._executor = DaemonTaskPool(max_workers=max(1, concurrency), thread_name_prefix="galgame-http")
        self._cache: dict[tuple[Any, ...], AsyncHttpResponse] = {}
        self._flights: dict[tuple[Any, ...], asyncio.Task[AsyncHttpResponse]] = {}
        self._network_requests = 0
        self._cache_hits = 0
        self._started = time.monotonic()
        self._closed = False

    @property
    def stats(self) -> AsyncHttpStats:
        return AsyncHttpStats(self._network_requests, self._cache_hits, time.monotonic() - self._started)

    def validate_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise UnsafeUrlError(f"URL credentials forbidden: {url}")
        _public_url(url, self.resolver)

    async def _validate_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise UnsafeUrlError(f"URL credentials forbidden: {url}")
        loop = asyncio.get_running_loop()
        await self._wait(
            loop.run_in_executor(self._executor, _public_url, url, self.resolver),
            timeout=self.timeout,
        )

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = list(self._flights.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _check_cancelled(self) -> None:
        if self._closed or bool(getattr(self.cancellation, "is_cancelled", False)):
            raise asyncio.CancelledError("HTTP operation cancelled")

    async def _wait(self, awaitable: Any, *, timeout: float | None = None) -> Any:
        task = asyncio.ensure_future(awaitable)
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise httpx.TimeoutException("HTTP transport timed out")
                done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining) if remaining is not None else 0.05)
                if done:
                    return await task
        except BaseException:
            task.cancel()
            done, _ = await asyncio.wait(
                {task}, timeout=self._CANCELLATION_CLEANUP_SECONDS,
            )
            if done:
                await asyncio.gather(task, return_exceptions=True)
            else:
                # Cancellation-resistant transports may finish later. Retrieve
                # any eventual exception without keeping this request/close path
                # blocked while they perform cleanup.
                task.add_done_callback(self._consume_task_result)
            raise

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            pass

    async def _gate(self) -> None:
        while True:
            self._check_cancelled()
            if not bool(getattr(self.cancellation, "is_paused", False)):
                return
            await asyncio.sleep(0.05)

    async def _acquire(self, semaphore: asyncio.Semaphore) -> None:
        await self._wait(semaphore.acquire())

    @staticmethod
    def _cookies(cookies: Mapping[str, str] | None) -> dict[str, str]:
        result = dict(cookies or {})
        for name, value in result.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(name)) or re.search(r"[;\r\n]", str(value)):
                raise ValueError("invalid cookie name or value")
        return result

    async def get(self, url: str, *, cookies: dict[str, str] | None = None, cache: bool = True) -> AsyncHttpResponse:
        self._check_cancelled()
        await self._validate_url(url)
        explicit_cookies = self._cookies(cookies)
        parts = urlsplit(url)
        normalized = urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path or "/", parts.query, ""))
        key = (normalized, tuple(sorted(explicit_cookies.items())))
        if not cache:
            return await self._request(normalized, explicit_cookies, self.max_response_bytes, None)
        if key in self._cache:
            self._cache_hits += 1
            return self._cache[key]
        task = self._flights.get(key)
        if task is None:
            task = asyncio.create_task(self._request(normalized, explicit_cookies, self.max_response_bytes, None))
            self._flights[key] = task

            def finish(done: asyncio.Task[AsyncHttpResponse]) -> None:
                if self._flights.get(key) is done:
                    self._flights.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    response = done.result()
                    if 200 <= response.status_code < 300:
                        self._cache[key] = response

            task.add_done_callback(finish)
        else:
            self._cache_hits += 1
        return await asyncio.shield(task)

    async def download(self, url: str, target: Path, *, max_response_bytes: int | None = None, max_retries: int | None = None) -> AsyncHttpResponse:
        self._check_cancelled()
        await self._validate_url(url)
        target = Path(target)
        try:
            return await self._request(url, {}, max_response_bytes or self.max_response_bytes, target, max_retries=max_retries)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    async def post_json(self, url: str, payload: dict) -> AsyncHttpResponse:
        """Public VNDB queries use the same limits, proxy and cancellation gates.

        POST redirects are refused so query bodies never leave the API origin.
        Retries/rate limiting belong to the task's VNDB query coordinator.
        """
        if urlsplit(url).hostname != "api.vndb.org" or not url.startswith("https://api.vndb.org/kana/"):
            raise UnsafeUrlError("unsupported public JSON API endpoint")
        await self._validate_url(url)
        response = await self._fetch(url, {"user-agent": self.user_agent, "content-type": "application/json"},
                                     self.max_response_bytes, None, method="POST", payload=payload)
        if 300 <= response.status_code < 400:
            raise UnsafeUrlError("public JSON API redirect refused")
        return response

    async def _request(self, url: str, cookies: dict[str, str], cap: int, target: Path | None, *, max_retries: int | None = None) -> AsyncHttpResponse:
        current = url
        original_host = urlsplit(url).hostname
        attempts = 0
        limit = self.max_retries if max_retries is None else max(1, max_retries)
        while attempts < limit:
            attempts += 1
            try:
                response = await self._redirects(current, cookies, original_host, cap, target)
                if (response.status_code in {408, 429} or response.status_code >= 500) and attempts < limit:
                    if target is not None:
                        target.unlink(missing_ok=True)
                    await self._wait(asyncio.sleep(min(0.05 * 2 ** (attempts - 1), 0.2)))
                    continue
                return response
            except (UnsafeUrlError, ResponseTooLarge, asyncio.CancelledError):
                raise
            except Exception as exc:
                fallback = _verified_www_fallback(current, exc)
                if fallback is not None and (max_retries is None or attempts < limit):
                    current = fallback
                    if max_retries is None:
                        limit += 1
                    continue
                if attempts >= limit:
                    raise
                await self._wait(asyncio.sleep(min(0.05 * 2 ** (attempts - 1), 0.2)))
        raise RuntimeError("HTTP retry loop exhausted")

    async def _redirects(self, current: str, cookies: dict[str, str], original_host: str | None, cap: int, target: Path | None) -> AsyncHttpResponse:
        for _ in range(11):
            await self._validate_url(current)
            headers = {"user-agent": self.user_agent}
            if cookies and urlsplit(current).hostname == original_host:
                headers["cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
            response = await self._fetch(current, headers, cap, target)
            await self._validate_url(response.url)
            location = response.headers.get("location")
            if response.status_code in {301, 302, 303, 307, 308} and location:
                if target is not None:
                    target.unlink(missing_ok=True)
                current = urljoin(current, location)
                await self._validate_url(current)
                continue
            return response
        raise UnsafeUrlError("too many HTTP redirects")

    async def _fetch(self, url: str, headers: dict[str, str], cap: int, target: Path | None, *, method: str = "GET", payload: dict | None = None) -> AsyncHttpResponse:
        host = urlsplit(url).hostname or ""
        host_semaphore = self._hosts.setdefault(host, asyncio.Semaphore(self._host_limit))
        await self._acquire(host_semaphore)
        try:
            await self._acquire(self._global)
            try:
                await self._gate()
                self._network_requests += 1
                if self.transport is not None:
                    request_options = {"timeout": self.timeout, "headers": headers}
                    if method != "GET":
                        request_options.update(method=method, json=payload)
                    if inspect.iscoroutinefunction(self.transport) or inspect.iscoroutinefunction(getattr(self.transport, "__call__", None)):
                        raw = await self._wait(self.transport(url, **request_options), timeout=self.timeout)
                    else:
                        loop = asyncio.get_running_loop()
                        raw = await self._wait(loop.run_in_executor(self._executor, lambda: self.transport(url, **request_options)), timeout=self.timeout)
                    return await self._from_injected(raw, url, cap, target)
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=self.trust_env)
                    self._client._cookies = _NoCookieJar()
                context = self._client.stream(method, url, headers=headers, follow_redirects=False,
                                              **({"json": payload} if method != "GET" else {}))
                raw = await self._wait(context.__aenter__(), timeout=self.timeout)
                try:
                    return await self._from_stream(raw, url, cap, target)
                finally:
                    await context.__aexit__(None, None, None)
            finally:
                self._global.release()
        finally:
            host_semaphore.release()

    async def _from_injected(self, raw: Any, url: str, cap: int, target: Path | None) -> AsyncHttpResponse:
        headers = {str(k).lower(): str(v) for k, v in getattr(raw, "headers", {}).items()}
        final_url = str(getattr(raw, "url", url))
        await self._validate_url(final_url)
        self._check_length(headers, cap)
        content = getattr(raw, "content", b"") or b""
        if isinstance(content, str):
            content = content.encode()
        if not content and getattr(raw, "text", None):
            content = str(raw.text).encode()
        if len(content) > cap:
            raise ResponseTooLarge(f"response exceeds {cap} bytes")
        if target is not None:
            target.write_bytes(content)
            content = b""
        return AsyncHttpResponse(content, int(getattr(raw, "status_code", 200)), headers, final_url, getattr(raw, "encoding", None))

    async def _from_stream(self, raw: httpx.Response, url: str, cap: int, target: Path | None) -> AsyncHttpResponse:
        headers = {str(k).lower(): str(v) for k, v in raw.headers.items()}
        final_url = str(raw.url)
        await self._validate_url(final_url)
        self._check_length(headers, cap)
        size = 0
        chunks: list[bytes] = []
        output = target.open("wb") if target is not None else None
        try:
            iterator = raw.aiter_bytes()
            while True:
                try:
                    chunk = await self._wait(anext(iterator), timeout=self.timeout)
                except StopAsyncIteration:
                    break
                size += len(chunk)
                if size > cap:
                    raise ResponseTooLarge(f"response exceeds {cap} bytes")
                if output is not None:
                    output.write(chunk)
                else:
                    chunks.append(chunk)
        finally:
            if output is not None:
                output.close()
        return AsyncHttpResponse(b"" if target is not None else b"".join(chunks), raw.status_code, headers, final_url, raw.encoding)

    @staticmethod
    def _check_length(headers: Mapping[str, str], cap: int) -> None:
        length = headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                return
            if declared > cap:
                raise ResponseTooLarge(f"response exceeds {cap} bytes")
