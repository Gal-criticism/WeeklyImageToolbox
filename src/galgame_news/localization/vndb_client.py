"""Minimal public VNDB API client with serialized requests and a small cache."""

from __future__ import annotations

import threading
import time
import json
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

from .vendor.vndb_query import APIResponse, VNDBClient as SkillVNDBClient


class VNDBClient(SkillVNDBClient):
    """Safe transport adapter extending the vendored vndb-api skill client."""

    API = "https://api.vndb.org/kana"

    def __init__(self, http_client: Any, *, min_interval: float = 1.5):
        super().__init__(base_url=self.API, timeout=3)
        self.http = http_client
        self.min_interval = min_interval
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._last_request = 0.0
        self._news_counts: dict[str, int] = {}
        self._cooldown_until = 0.0
        self._response_headers = {}
        self._invalid_json = False

    def _query(self, endpoint: str, payload: dict[str, Any], key: str, news_id: str = "") -> dict[str, Any] | None:
        cache_key = (endpoint, key)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
            cooldown_remaining = self._cooldown_until - time.monotonic()
            if cooldown_remaining > 5:
                raise RuntimeError("VNDB request deferred by task-wide Retry-After cooldown")
            self._wait_until(max(self._cooldown_until, self._last_request + self.min_interval))
            if news_id and self._news_counts.get(news_id, 0) >= 16:
                raise RuntimeError("VNDB query budget exhausted for this news item")
            if news_id:
                self._news_counts[news_id] = self._news_counts.get(news_id, 0) + 1
            try:
                response = self.post(endpoint, payload)
            finally:
                self._last_request = time.monotonic()
            status = int(getattr(response, "status_code", 200))
            if status == 429:
                delay = self._retry_after(self._response_headers)
                if delay > 5:
                    self._cooldown_until = time.monotonic() + delay
                    raise RuntimeError("VNDB rate limited; request deferred until Retry-After cooldown expires")
                if news_id and self._news_counts.get(news_id, 0) >= 16:
                    self._cooldown_until = time.monotonic() + delay
                    raise RuntimeError("VNDB rate limited at the per-news query budget; retry deferred")
                self._wait_seconds(delay)
                self._wait_until(time.monotonic() + self.min_interval)
                if news_id:
                    self._news_counts[news_id] = self._news_counts.get(news_id, 0) + 1
                try:
                    response = self.post(endpoint, payload)
                finally:
                    self._last_request = time.monotonic()
                status = int(getattr(response, "status_code", 200))
                if status == 429:
                    delay = self._retry_after(self._response_headers)
                    self._cooldown_until = time.monotonic() + delay
                    raise RuntimeError("VNDB rate limit persisted after Retry-After retry")
            if not 200 <= status < 300:
                raise RuntimeError(f"VNDB API returned HTTP {status}")
            if self._invalid_json:
                raise RuntimeError("VNDB API returned malformed JSON")
            data = response.data
            rows = data.get("results", []) if isinstance(data, dict) else []
            if not isinstance(rows, list):
                raise RuntimeError("VNDB API returned malformed results")
            result = rows[0] if rows and isinstance(rows[0], dict) else None
            if result is not None and str(result.get("id", "")).casefold() != key.casefold():
                raise RuntimeError("VNDB API returned a conflicting work identifier")
            self._cache[cache_key] = result
            return result

    def release(self, release_id: str, news_id: str = "") -> dict[str, Any] | None:
        return self._query("release", {
            "filters": ["id", "=", release_id], "fields": "id,title,vns{id,title,alttitle,rtype}",
            "results": 1,
        }, release_id, news_id)

    def vn(self, vn_id: str, news_id: str = "") -> dict[str, Any] | None:
        return self._query("vn", {
            "filters": ["id", "=", vn_id],
            "fields": "id,title,alttitle,titles.title,titles.latin,rating,votecount,screenshots{id,url,dims,thumbnail,thumbnail_dims,release.id,sexual,violence,votecount},relations{id,title,alttitle,relation,relation_official}",
            "results": 1,
        }, vn_id, news_id)

    def _make_request(self, endpoint, method="GET", data=None, params=None):
        """Replace the skill's urllib transport with the task's safe HTTP bridge."""
        if method != "POST" or endpoint not in {"vn", "release"} or params:
            raise ValueError("localization adapter only permits VNDB POST /vn and /release")
        raw = self.http.post_json(f"{self.API}/{endpoint}", data or {})
        self._response_headers = getattr(raw, "headers", {}) or {}
        self._invalid_json = False
        status = int(getattr(raw, "status_code", 200))
        body = getattr(raw, "json", None)
        try:
            parsed = body() if callable(body) else json.loads(getattr(raw, "text", ""))
        except (ValueError, TypeError):
            self._invalid_json = 200 <= status < 300
            parsed = {}
        return APIResponse(data=parsed if isinstance(parsed, dict) else {}, status_code=status)

    def reserve_page(self, news_id: str) -> None:
        with self._lock:
            if self._news_counts.get(news_id, 0) >= 16:
                raise RuntimeError("VNDB lookup budget exhausted for this news item")
            self._news_counts[news_id] = self._news_counts.get(news_id, 0) + 1

    def remaining(self, news_id: str) -> int:
        with self._lock:
            return max(0, 16 - self._news_counts.get(news_id, 0))

    def _retry_after(self, headers) -> float:
        raw = next((value for key, value in headers.items() if str(key).casefold() == "retry-after"), "1")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(raw))
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 1.0

    def _wait_until(self, deadline: float) -> None:
        while time.monotonic() < deadline:
            self._wait_seconds(min(0.05, deadline - time.monotonic()))

    def _wait_seconds(self, delay: float) -> None:
        runtime = getattr(self.http, "runtime", None)
        token = getattr(runtime, "token", None)
        client = getattr(self.http, "client", None)
        token = token or getattr(client, "cancellation", None)
        end = time.monotonic() + max(0.0, delay)
        while time.monotonic() < end:
            if token is not None:
                wait_paused = getattr(token, "wait_if_paused", None)
                if callable(wait_paused):
                    wait_paused()
                if bool(getattr(token, "is_cancelled", False)):
                    from ..pipeline.contracts import CancellationRequested
                    raise CancellationRequested("VNDB request cancelled")
            time.sleep(min(0.05, end - time.monotonic()))
