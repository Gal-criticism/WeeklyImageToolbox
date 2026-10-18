#!/usr/bin/env python3
"""Replay z-section goods collection against a read-only baseline task.

Network access is opt-in. When enabled, only bounded public GET requests are
made while freezing relevant pages and candidate images. The subsequent formal
pipeline run uses a closed-world transport backed only by those frozen files.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import closing
import hashlib
import html
import json
import mimetypes
import re
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from galgame_news.config import load_config
from galgame_news.discovery.goods_context import cached_product_urls, is_goods_news, unwrap_goods_url
from galgame_news.discovery.goods_pages import goods_site
from galgame_news.discovery.goods_service import GoodsSourceService, goods_page_text
from galgame_news.discovery.http import SafeHttpClient
from galgame_news.discovery.product_navigation import product_links
from galgame_news.discovery.resolver import DefaultSourceResolver
from galgame_news.domain import (SourceRef, CollectionContext, CollectionResult, DiscoveryMethod,
                                 ImageCandidate, Issue, NewsItem,
                                 SourceType)
from galgame_news.pipeline.contracts import TaskRequest
from galgame_news.pipeline.runner import PipelineRunner
from galgame_news.ingestion import DocxDocumentParser

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PAGES_PER_NEWS = 16
MAX_DEPTH = 3
EEO_SAMPLE_WORK = "あえて無視するキミとの未来"
EEO_SAMPLE_URL = "https://eeo.today/store/101/title/8161"
IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "AVIF": ".avif"}


class HttpResponse:
    def __init__(self, url, status=200, content=b"", headers=None):
        self.url, self.status_code, self.content = str(url), int(status), bytes(content)
        self.headers = dict(headers or {})
        self.text = self.content.decode("utf-8", "replace")
        self.text = goods_page_text(self)


def normalized_url(url: str) -> str:
    p = urlsplit(unwrap_goods_url(str(url)))
    return p._replace(fragment="").geturl()


def is_x_url(url: str) -> bool:
    return (urlsplit(str(url)).hostname or "").casefold().removeprefix("www.") in {"x.com", "twitter.com"}


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_baseline(task_root: Path):
    db_path = task_root / "task_state.sqlite3"
    if not db_path.is_file():
        raise FileNotFoundError(f"baseline task database not found: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"news", "candidates", "post_cache"} <= tables:
            raise ValueError("baseline database is missing required task-state tables")
        checkpoint_row = db.execute("SELECT value FROM checkpoint WHERE key='payload'").fetchone() if "checkpoint" in tables else None
        checkpoint = json.loads(checkpoint_row[0]) if checkpoint_row else {}
        news_rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM news ORDER BY rowid")]
        candidate_rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM candidates WHERE kind='image' ORDER BY ordinal,id")]
        posts = {}
        for post_id, state, raw_payload in db.execute("SELECT post_id,state,payload FROM post_cache"):
            payload = json.loads(raw_payload) if state == "success" else None
            if isinstance(payload, dict) and payload.get("state") in {"success", "failed"} and "payload" in payload:
                payload = payload["payload"]
            posts[post_id] = payload
        input_hash = checkpoint.get("input_sha256", "")
        issue_id = checkpoint.get("issue_id") or checkpoint.get("issue", {}).get("issue_id")
        if not issue_id:
            row = db.execute("SELECT value FROM task_meta WHERE key='issue_id'").fetchone() if "task_meta" in tables else None
            issue_id = json.loads(row[0]) if row else ""
    ignored_news_fields = sorted({key for row in news_rows for key in row if key not in NewsItem.model_fields})
    news = []
    for row in news_rows:
        filtered = {key: value for key, value in row.items() if key in NewsItem.model_fields}
        item = NewsItem.model_validate(filtered)
        if row.get("news_id"):
            object.__setattr__(item, "id", row["news_id"])
        news.append(item)
    image_candidates = [ImageCandidate.model_validate(row) for row in candidate_rows]
    return str(issue_id or ""), input_hash, news, image_candidates, posts, ignored_news_fields


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def freeze_baseline_originals(candidates, baseline_root: Path, output_root: Path, failures: list[dict]):
    target = output_root / "frozen" / "baseline_assets"
    target.mkdir(parents=True, exist_ok=True)
    copies = {}
    for candidate in candidates:
        if not (candidate.signals.get("x_api_photo") or candidate.signals.get("socialdata_photo")
                or candidate.source_type is SourceType.OFFICIAL_X):
            continue
        raw = candidate.original_path or candidate.local_path
        if not raw:
            continue
        source = Path(raw)
        if not source.is_absolute():
            source = baseline_root / source
        if not within(source, baseline_root) or not source.is_file():
            failures.append({"news_id": candidate.news_id, "url": candidate.image_url,
                             "stage": "baseline_asset", "status": "missing_or_out_of_scope"})
            continue
        expected = candidate.original_sha256 or candidate.sha256
        try:
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if not expected or digest != expected:
                raise ValueError("sha256_mismatch")
            with Image.open(source) as image:
                image.load()
                image_format = image.format or ""
                width, height = image.size
            suffix = source.suffix.lower()
            expected_suffix = IMAGE_EXTENSIONS.get(image_format, "")
            valid_suffixes = {".jpg", ".jpeg"} if image_format == "JPEG" else ({expected_suffix} if expected_suffix else set())
            if valid_suffixes and suffix not in valid_suffixes:
                raise ValueError("extension_format_mismatch")
            copied = target / f"{digest}{suffix or expected_suffix or '.img'}"
            if not copied.exists():
                copied.write_bytes(data)
            candidate.original_path = str(copied)
            candidate.local_path = str(copied)
            candidate.original_sha256 = digest
            candidate.sha256 = candidate.sha256 or digest
            candidate.original_width = candidate.original_width or width
            candidate.original_height = candidate.original_height or height
            candidate.download_status = "downloaded"
            copies[candidate.id] = copied
        except Exception as exc:
            failures.append({"news_id": candidate.news_id, "url": candidate.image_url,
                             "stage": "baseline_asset", "status": "invalid",
                             "reason": str(exc) if str(exc) in {"sha256_mismatch", "extension_format_mismatch"} else type(exc).__name__})
    return copies


class FrozenStore:
    def __init__(self, output_root: Path, *, fetch_enabled: bool = False, live_replay: bool = False):
        self.live_replay = live_replay
        self.root = output_root / "fixtures"
        self.fetch_enabled = fetch_enabled
        self.pages: dict[str, HttpResponse] = {}
        self.images: dict[str, HttpResponse] = {}
        self.statuses: dict[str, dict] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "pages").mkdir(exist_ok=True)
        (self.root / "images").mkdir(exist_ok=True)

    def add_page(self, requested_url, response):
        content = bytes(getattr(response, "content", b"") or str(getattr(response, "text", "")).encode())
        final_url = str(getattr(response, "url", requested_url))
        status = int(getattr(response, "status_code", 200))
        key = hashlib.sha256(normalized_url(requested_url).encode()).hexdigest()
        path = self.root / "pages" / f"{key}.html"
        path.write_bytes(content)
        frozen = HttpResponse(final_url, status, content, getattr(response, "headers", {}))
        self.pages[normalized_url(requested_url)] = frozen
        self.pages[normalized_url(final_url)] = frozen
        self.statuses[normalized_url(requested_url)] = {"kind": "page", "status": status, "file": path.relative_to(self.root.parent).as_posix()}
        self.statuses[normalized_url(requested_url)].update(final_url=final_url, headers=dict(frozen.headers))
        return frozen

    def add_image(self, url, response):
        content = bytes(getattr(response, "content", b"") or b"")
        digest = hashlib.sha256(content).hexdigest()
        content_type = str(getattr(response, "headers", {}).get("content-type", "")).split(";", 1)[0].strip()
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
            suffix = mimetypes.guess_extension(content_type) or ".img"
        path = self.root / "images" / f"{digest}{suffix}"
        path.write_bytes(content)
        frozen = HttpResponse(str(getattr(response, "url", url)), int(getattr(response, "status_code", 200)), content, getattr(response, "headers", {}))
        self.images[normalized_url(url)] = frozen
        self.images[normalized_url(frozen.url)] = frozen
        self.statuses[normalized_url(url)] = {"kind": "image", "status": frozen.status_code, "file": path.relative_to(self.root.parent).as_posix(), "sha256": digest}
        self.statuses[normalized_url(url)].update(final_url=frozen.url, headers=dict(frozen.headers))

    def load(self, previous):
        previous = Path(previous).resolve()
        for url, record in json.loads((previous / "fixture_manifest.json").read_text("utf-8")).items():
            relative = record.get("file")
            if not relative:
                continue
            path = previous / relative
            if not within(path, previous) or not path.is_file():
                continue
            response = HttpResponse(record.get("final_url", url), record.get("status", 599), path.read_bytes(), record.get("headers", {}))
            if record["kind"] == "page":
                self.add_page(url, response)
            else:
                self.add_image(url, response)

    def source_transport(self, url, **_kwargs):
        key = normalized_url(url)
        if key not in self.pages and self.live_replay:
            if is_x_url(url):
                raise ValueError("live X requests are forbidden in cached validation")
            response = _fetch(SafeHttpClient(timeout=10, max_retries=0, max_response_bytes=MAX_PAGE_BYTES),
                              url, max_bytes=MAX_PAGE_BYTES, store=self, kind="page")
            if response is not None:
                return response
            self.pages[key] = HttpResponse(url, 599, b"fetch_failed")
        if key not in self.pages:
            self.statuses.setdefault(key, {"kind": "page", "status": "fixture_missing", "reason": "closed_world_replay"})
            return HttpResponse(url, 599, b"fixture_missing")
        return self.pages[key]

    def image_transport(self, url, **_kwargs):
        key = normalized_url(url)
        if key not in self.images and self.live_replay:
            response = _fetch(SafeHttpClient(timeout=10, max_retries=0, max_response_bytes=MAX_IMAGE_BYTES),
                              url, max_bytes=MAX_IMAGE_BYTES, store=self, kind="image")
            if response is not None:
                return response
            self.images[key] = HttpResponse(url, 599, b"fetch_failed")
        if key not in self.images:
            self.statuses.setdefault(key, {"kind": "image", "status": "fixture_missing", "reason": "closed_world_replay"})
            return HttpResponse(url, 599, b"fixture_missing")
        return self.images[key]


def _source(url, root, parent=None, depth=0, kind="goods"):
    source = DefaultSourceResolver._document_source(url)
    source.root_url, source.parent_url = root, parent
    source.navigation_kind, source.navigation_depth = kind, depth
    source.discovered_via = DiscoveryMethod.SAME_DOMAIN
    return source


def _fetch(client, url, *, max_bytes, store, kind):
    cached = (store.pages if kind == "page" else store.images).get(normalized_url(url))
    if cached is not None:
        return cached
    if not store.fetch_enabled:
        store.statuses[normalized_url(url)] = {"kind": kind, "status": "fixture_missing", "reason": "closed_world_replay"}
        return None
    try:
        response = client.get(url, max_retries=1)
        data = bytes(getattr(response, "content", b"") or str(getattr(response, "text", "")).encode())
        if len(data) > max_bytes:
            raise ValueError("response_too_large")
        return store.add_page(url, response) if kind == "page" else (store.add_image(url, response) or response)
    except Exception as exc:
        store.statuses[normalized_url(url)] = {"kind": kind, "status": "fetch_failed", "reason": type(exc).__name__}
        return None


def freeze_goods_web(news_items, post_cache, store: FrozenStore, fetch: bool, failures: list[dict]):
    """Freeze bounded page trees. Link discovery uses the production parsers."""
    if not fetch and not store.pages:
        for item in news_items:
            failures.append({"news_id": item.id, "stage": "page_fetch", "status": "not_fetched", "reason": "--fetch not supplied"})
        return [(EEO_SAMPLE_URL, "", "approved_explicit_seed")], []
    client = SafeHttpClient(timeout=10, max_retries=1, max_response_bytes=MAX_PAGE_BYTES)
    sample_seeds: list[tuple[str, str, str]] = [(EEO_SAMPLE_URL, "", "approved_explicit_seed")]
    image_urls: set[str] = set()
    sample_candidates: list[ImageCandidate] = []
    for item in news_items:
        if not is_goods_news(item):
            continue
        roots = [(url, url, None, "goods") for url in item.source_urls if not is_x_url(url)]
        roots.extend((unwrap_goods_url(url), post_url, post_url, "goods")
                     for url, post_url in cached_product_urls(post_cache, item)
                     if goods_site(unwrap_goods_url(url)))
        queue = deque((unwrap_goods_url(url), 0, root, parent, kind) for url, root, parent, kind in roots
                      if urlsplit(url).scheme in {"http", "https"} and not is_x_url(url))
        visited: set[str] = set()
        page_count = 0
        while queue and page_count < MAX_PAGES_PER_NEWS:
            url, depth, root, parent, kind = queue.popleft()
            key = normalized_url(url)
            if key in visited or depth > MAX_DEPTH:
                continue
            visited.add(key)
            page_count += 1
            if key not in store.pages:
                response = _fetch(client, url, max_bytes=MAX_PAGE_BYTES, store=store, kind="page")
            else:
                response = store.pages[key]
            if response is None or not 200 <= response.status_code < 300:
                failures.append({"news_id": item.id, "url": url, "stage": "page_fetch",
                                 "status": store.statuses.get(key, {}).get("status", getattr(response, "status_code", "failed")),
                                 "reason": store.statuses.get(key, {}).get("reason", "non_success_http")})
                continue
            source = _source(str(response.url), root, parent, depth, kind)
            page_url = str(response.url)
            if goods_site(page_url):
                links = GoodsSourceService.links(item, source, response.text, page_url)
                parsed = GoodsSourceService(None).parse(item, source, CollectionContext(timeout_seconds=10, max_candidates=300, now=datetime.now(timezone.utc)), response.text, page_url=page_url)
                image_urls.update(c.image_url for c in parsed.candidates)
            else:
                links = product_links(item, source, page_url, BeautifulSoup(response.text, "html.parser"))
            # Find the separate EEO sample only if a public page actually links it.
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True)
                target = anchor.get("href")
                absolute = urljoin(page_url, target)
                if EEO_SAMPLE_WORK in label and goods_site(absolute) == "eeo":
                    sample_seeds.append((absolute, page_url, "public_link"))
            if depth < MAX_DEPTH:
                for target, label, link_kind in links:
                    if goods_site(target) and normalized_url(target) not in visited:
                        queue.append((target, depth + 1, root, page_url, link_kind))
        if queue:
            failures.append({"news_id": item.id, "stage": "page_discovery", "status": "budget_reached",
                             "reason": f"bounded at {MAX_PAGES_PER_NEWS} pages and depth {MAX_DEPTH}"})
    if sample_seeds:
        sample_news = NewsItem(issue_id="eeo-sample", sequence=1, section="周边", title=f"{EEO_SAMPLE_WORK} 商品验证样例",
            body="独立商品页解析样例；不是本期新闻。", game_names=[EEO_SAMPLE_WORK],
            source_urls=[url for url, _, _ in sample_seeds])
        queue = deque((url, 0, url, parent, "goods") for url, parent, _ in sample_seeds)
        visited: set[str] = set()
        page_count = 0
        while queue and page_count < MAX_PAGES_PER_NEWS:
            url, depth, root, parent, kind = queue.popleft()
            key = normalized_url(url)
            if key in visited or depth > MAX_DEPTH:
                continue
            visited.add(key)
            page_count += 1
            response = store.pages.get(key)
            if response is None:
                response = _fetch(client, url, max_bytes=MAX_PAGE_BYTES, store=store, kind="page")
            if response is None or not 200 <= response.status_code < 300:
                failures.append({"news_id": sample_news.id, "url": url, "stage": "eeo_sample_page",
                                 "status": store.statuses.get(key, {}).get("status", getattr(response, "status_code", "failed")),
                                 "reason": store.statuses.get(key, {}).get("reason", "non_success_http")})
                continue
            source = _source(str(response.url), root, parent, depth, kind)
            source.officiality = 0.86
            parsed = GoodsSourceService(None).parse(sample_news, source,
                CollectionContext(timeout_seconds=10, max_candidates=300, now=datetime.now(timezone.utc)),
                response.text, page_url=str(response.url))
            sample_candidates.extend(parsed.candidates)
            image_urls.update(c.image_url for c in parsed.candidates)
            for target, _label, link_kind in GoodsSourceService.links(sample_news, source, response.text, str(response.url)):
                if goods_site(target) == "eeo" and normalized_url(target) not in visited and depth < MAX_DEPTH:
                    queue.append((target, depth + 1, root, str(response.url), link_kind))
    image_client = SafeHttpClient(timeout=10, max_retries=1, max_response_bytes=MAX_IMAGE_BYTES)
    for url in sorted(image_urls):
        response = _fetch(image_client, url, max_bytes=MAX_IMAGE_BYTES, store=store, kind="image")
        if response is None or int(getattr(response, "status_code", 599)) >= 400:
            failures.append({"url": url, "stage": "image_fetch", "status": store.statuses.get(normalized_url(url), {}).get("status", "failed"),
                             "reason": store.statuses.get(normalized_url(url), {}).get("reason", "non_success_http")})
    for candidate in sample_candidates:
        fixture = store.images.get(normalized_url(candidate.image_url))
        status = store.statuses.get(normalized_url(candidate.image_url), {})
        if fixture is None or not 200 <= fixture.status_code < 300:
            continue
        image_path = store.root.parent / status.get("file", "")
        try:
            with Image.open(image_path) as image:
                image.load()
                candidate.width, candidate.height = image.size
                candidate.original_width, candidate.original_height = image.size
            digest = sha_file(image_path)
            candidate.original_path = candidate.local_path = str(image_path)
            candidate.original_sha256 = candidate.sha256 = digest
            candidate.download_status = "downloaded"
        except Exception as exc:
            failures.append({"news_id": "eeo-sample", "url": candidate.image_url,
                             "stage": "eeo_sample_image", "status": "invalid", "reason": type(exc).__name__})
    return sample_seeds, sample_candidates


class FrozenAnalyzer:
    def __init__(self, issue: Issue, input_path: Path):
        self.issue = issue.model_copy(update={"input_path": str(input_path)})

    def analyze(self, _draft):
        return self.issue.model_copy(deep=True)


class ReplayRunner(PipelineRunner):
    def __init__(self, *, issue, input_path, x_candidates, posts, store, config, search_provider=None):
        super().__init__(analyzer=FrozenAnalyzer(issue, input_path),
                         resolver=DefaultSourceResolver(search_provider=search_provider or (lambda news: [])), config=config,
                         source_transport=store.source_transport, image_transport=store.image_transport,
                         adapter_factory=None)
        self.frozen_x_candidates = x_candidates
        self.frozen_posts = posts

    async def _resolve_async(self, news, request):
        self._socialdata_response_cache = copy.deepcopy(self.frozen_posts)
        sources = []
        for original in news.source_urls:
            effective = unwrap_goods_url(original)
            source = DefaultSourceResolver._document_source(effective)
            if effective != original:
                source.root_url = original
                source.parent_url = original
                source.navigation_kind = "goods"
            sources.append(source)
        return sources

    def _collect(self, news, source, *, request=None):
        if source.source_type is SourceType.OFFICIAL_X:
            rows = copy.deepcopy(self.frozen_x_candidates.get(news.id, []))
            def post_id(url):
                match = re.search(r"/status/(\d+)", url or "")
                return match[1] if match else ""
            rows = [c for c in rows if post_id(c.source_url) == post_id(source.url)]
            for candidate in rows:
                candidate.local_path = candidate.original_path = None
                candidate.download_status = "pending"
                candidate.downloaded_url = None
                candidate.downloadable = True
            return CollectionResult(candidates=rows)
        return super()._collect(news, source, request=request)


def _relative_file(path, root):
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def build_reports(output_root, issue, baseline_candidates, result, failures, store, sample_seeds,
                  sample_result=None, ignored_news_fields=None):
    by_news = {item.id: item for item in issue.news_items}
    old_counts, old_selected = defaultdict(int), defaultdict(int)
    for candidate in baseline_candidates:
        if candidate.news_id in by_news:
            old_counts[candidate.news_id] += 1
            old_selected[candidate.news_id] += bool(candidate.selected)
    new_candidates = list(result.pipeline_result.all_candidates if result and result.pipeline_result else [])
    new_counts, new_selected = defaultdict(int), defaultdict(int)
    rows = []
    for candidate in new_candidates:
        item = by_news.get(candidate.news_id)
        if item is None:
            continue
        new_counts[item.id] += 1
        new_selected[item.id] += bool(candidate.selected)
        evidence = [{"page_url": e.page_url, "item_id": e.item_id, "role": e.role, "goods_role": e.image_role,
                     "text": e.text, "method": e.method} for e in candidate.evidence if e.role == "goods"]
        rows.append({"news_id": item.id, "news_title": item.title, "image_url": candidate.image_url,
                     "source_url": candidate.source_url,
                     "image_type": candidate.image_type.value, "selected": candidate.selected,
                     "goods_association": candidate.signals.get("goods_association"),
                     "goods_role": candidate.signals.get("goods_role"),
                     "goods_site": candidate.signals.get("goods_site"), "goods_original": candidate.signals.get("goods_original"),
                     "download_status": candidate.download_status,
                     "review_reasons": [reason.value for reason in candidate.review_reasons],
                     "selection_reasons": list(candidate.selection_reasons), "evidence": evidence,
                     "original_path": _relative_file(candidate.original_path, output_root),
                     "local_path": _relative_file(candidate.local_path, output_root)})
    sample_curated = list(sample_result.candidates if sample_result else [])
    for candidate in sample_curated:
        evidence = [{"page_url": e.page_url, "item_id": e.item_id, "role": e.role,
                     "goods_role": e.image_role, "text": e.text, "method": e.method}
                    for e in candidate.evidence if e.role == "goods"]
        rows.append({"validation_case": "eeo_sample", "news_id": candidate.news_id,
                     "source_url": candidate.source_url,
                     "news_title": EEO_SAMPLE_WORK + " (separate sample; not actual news)",
                     "image_url": candidate.image_url, "image_type": candidate.image_type.value,
                     "selected": candidate.selected, "goods_association": candidate.signals.get("goods_association"),
                     "goods_role": candidate.signals.get("goods_role"), "goods_site": candidate.signals.get("goods_site"),
                     "goods_original": candidate.signals.get("goods_original"),
                     "download_status": candidate.download_status,
                     "review_reasons": [reason.value for reason in candidate.review_reasons],
                     "selection_reasons": list(candidate.selection_reasons), "evidence": evidence,
                     "original_path": _relative_file(candidate.original_path, output_root),
                     "local_path": _relative_file(candidate.local_path, output_root)})
    summary = {"issue_id": issue.issue_id, "input_sha256": sha_file(Path(issue.input_path)),
               "ignored_baseline_news_fields": ignored_news_fields or [], "fetch_enabled": store.fetch_enabled,
               "news": [], "api_calls": 0, "extra_eeo_sample": {"work": EEO_SAMPLE_WORK,
               "status": "verified" if sample_result and sample_curated else ("unverified" if sample_seeds and store.fetch_enabled else "not_fetched"),
               "source_urls": [url for url, _, _ in sample_seeds], "provenance": [kind for _, _, kind in sample_seeds], "is_actual_news": False,
               "new_candidate_count": len(sample_curated),
               "selected_count": sum(bool(c.selected) for c in sample_curated)},
               "fixtures": len(store.statuses), "failed_source_count": len(failures)}
    expected_support = ("getchu", "fantia", "dlsite")
    seen_sites = {str(row.get("goods_site") or "") for row in rows}
    summary["support_site_samples"] = {
        site: {"status": "observed" if site in seen_sites else "unverified", "candidate_count": sum(row.get("goods_site") == site for row in rows)}
        for site in expected_support
    }
    for item in issue.news_items:
        summary["news"].append({"news_id": item.id, "title": item.title,
            "old_candidate_count": old_counts[item.id], "old_selected_count": old_selected[item.id],
            "new_candidate_count": new_counts[item.id], "new_selected_count": new_selected[item.id],
            "new_downloaded_count": sum(c.download_status == "downloaded" for c in new_candidates if c.news_id == item.id),
            "new_unselected_count": sum(c.curation_status.value == "unselected" for c in new_candidates if c.news_id == item.id),
            "new_invalid_count": sum(c.curation_status.value == "invalid" for c in new_candidates if c.news_id == item.id)})
    summary["extra_eeo_sample"]["downloaded_count"] = sum(c.download_status == "downloaded" for c in sample_curated)
    summary["extra_eeo_sample"]["failed_count"] = sum(c.download_status != "downloaded" for c in sample_curated)
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "failed_sources.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "fixture_manifest.json").write_text(json.dumps(store.statuses, ensure_ascii=False, indent=2), encoding="utf-8")
    cells = []
    for row in rows:
        evidence = "<br>".join(f'<a href="{html.escape(e["page_url"], quote=True)}">{html.escape(e["role"])} · {html.escape(e["text"][:140])}</a>' for e in row["evidence"])
        files = "<br>".join(f'<a href="{html.escape(p, quote=True)}">{html.escape(Path(p).name)}</a>' for p in (row["original_path"], row["local_path"]) if p)
        local = row["local_path"]
        thumb = (f'<a href="{html.escape(local, quote=True)}"><img src="{html.escape(local, quote=True)}" '
                 'loading="lazy" style="max-width:220px;max-height:160px"></a>' if local else "无本地预览")
        download_state = "已选中" if row["selected"] else ("下载失败，保留链接待恢复" if row["download_status"] != "downloaded" else "待人工复核")
        source_links = f'<a href="{html.escape(row["source_url"], quote=True)}">来源／原帖</a><br><a href="{html.escape(row["image_url"], quote=True)}">原图链接</a>'
        cells.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            row["news_title"], row["image_type"], row["goods_site"], row["goods_role"], row["goods_association"],
            download_state, ", ".join(row["review_reasons"]))) + f"<td>{thumb}</td><td>{evidence}</td><td>{source_links}</td><td>{files}</td></tr>")
    compare = "".join(f"<tr><td>{html.escape(v['title'])}</td><td>{v['old_candidate_count']}</td><td>{v['old_selected_count']}</td><td>{v['new_candidate_count']}</td><td>{v['new_selected_count']}</td></tr>" for v in summary["news"])
    page = """<!doctype html><meta charset=utf-8><title>Goods collection validation</title>
<style>body{font:15px system-ui;margin:2rem;color:#222}table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{border:1px solid #bbb;padding:.45rem;vertical-align:top}th{background:#eee}a{overflow-wrap:anywhere}</style>
<h1>周边商品采集验证</h1><p>使用冻结页面回放；API 调用：0。额外 EEO 样例单独统计，不属于本期新闻。</p>
<h2>前后候选对比</h2><table><thead><tr><th>新闻</th><th>旧候选数</th><th>旧选中数</th><th>新候选数（含未选中）</th><th>新选中数</th></tr></thead><tbody>""" + compare + """</tbody></table>
<h2>商品图片、证据与复核状态</h2><table><thead><tr><th>新闻</th><th>类型</th><th>站点</th><th>角色</th><th>关联</th><th>状态</th><th>复核原因</th><th>缩略图</th><th>商品证据</th><th>来源页</th><th>原图及本地文件</th></tr></thead><tbody>""" + "".join(cells) + """</tbody></table>
<h2>独立 EEO 样例</h2><p>""" + ("批准的样例入口及发现来源：" + ", ".join(html.escape(u) for u, _, _ in sample_seeds) if sample_seeds else "尚无样例入口。") + """</p></html>"""
    (output_root / "report.html").write_text(page, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-task", type=Path, required=True, help="Existing task root containing task_state.sqlite3")
    parser.add_argument("--input", type=Path, required=True, help="Original DOCX used by the baseline task")
    parser.add_argument("--output", type=Path, required=True, help="New, empty validation output root")
    parser.add_argument("--fetch", action="store_true", help="Fetch bounded public HTML/images, then replay from frozen fixtures")
    parser.add_argument("--search", action="store_true", help="Validate conditional public goods search; use --fetch or frozen --reuse-fixtures; no paid API")
    parser.add_argument("--reuse-fixtures", type=Path, help="Reuse an earlier validation's public fixtures before fetching missing resources")
    args = parser.parse_args(argv)
    if args.search and not (args.fetch or args.reuse_fixtures):
        parser.error("--search requires --fetch or --reuse-fixtures")
    baseline = args.baseline_task.expanduser().resolve()
    docx = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not docx.is_file():
        parser.error(f"input DOCX not found: {docx}")
    if output.exists():
        parser.error(f"output root must be new and nonexistent: {output}")
    issue_id, baseline_input_hash, all_news, baseline_candidates, posts, ignored_news_fields = read_baseline(baseline)
    input_hash = sha_file(docx)
    if baseline_input_hash and input_hash != baseline_input_hash:
        parser.error("input DOCX SHA-256 does not match the read-only baseline task")
    goods_news = [item for item in all_news if is_goods_news(item)]
    fresh_links = {entry.sequence: entry.source_urls for entry in DocxDocumentParser().parse(docx, issue_id).entries}
    for item in goods_news:
        identity = item.id
        if item.sequence in fresh_links:
            item.source_urls = fresh_links[item.sequence]
            object.__setattr__(item, "id", identity)
    if not goods_news:
        parser.error("baseline contains no z-section GOODS news items")
    output.mkdir(parents=True)
    failures: list[dict] = []
    freeze_baseline_originals(baseline_candidates, baseline, output, failures)
    frozen_news = [item for item in all_news if item in goods_news]
    frozen_issue = Issue(issue_id=issue_id or str(goods_news[0].issue_id), input_path=str(docx), news_items=frozen_news)
    store = FrozenStore(output, fetch_enabled=args.fetch, live_replay=args.search and args.fetch)
    if args.reuse_fixtures:
        store.load(args.reuse_fixtures)
    for candidate in baseline_candidates:
        if candidate.original_path and within(Path(candidate.original_path), output) and Path(candidate.original_path).is_file():
            with Image.open(candidate.original_path) as decoded:
                mime = Image.MIME.get(decoded.format, "application/octet-stream")
            for url in {candidate.image_url, candidate.media_source_url, candidate.downloaded_url} - {None, ""}:
                store.add_image(url, HttpResponse(url, content=Path(candidate.original_path).read_bytes(), headers={"content-type": mime}))
    sample_seeds, sample_candidates = freeze_goods_web(frozen_news, posts, store, args.fetch, failures)
    x_candidates = defaultdict(list)
    for candidate in baseline_candidates:
        if candidate.news_id in {item.id for item in frozen_news} and (
            candidate.source_type is SourceType.OFFICIAL_X or candidate.signals.get("x_api_photo") or candidate.signals.get("socialdata_photo")
        ):
            if candidate.original_path and within(Path(candidate.original_path), output):
                candidate.download_status = "downloaded"
            x_candidates[candidate.news_id].append(candidate)
    config = load_config()
    config.browser.enabled = False
    config.visual_analysis.enabled = False
    config.video.enabled = False
    config.network.max_retries = 1
    config.network.timeout_seconds = 10
    config.filters.max_response_bytes = MAX_PAGE_BYTES
    config.filters.max_image_bytes = MAX_IMAGE_BYTES
    searches = {}
    cached_searches = {}
    if args.reuse_fixtures and (args.reuse_fixtures / "search_results.json").is_file():
        cached_searches = json.loads((args.reuse_fixtures / "search_results.json").read_text("utf-8"))
    def search_provider(news):
        from galgame_news.discovery.search import DDGSSearchProvider, _search_queries
        rows = ([SourceRef.model_validate(r) for r in cached_searches[news.id]["results"]]
                if news.id in cached_searches else (DDGSSearchProvider(max_results=5, timeout=8).search(news) if args.fetch else []))
        searches[news.id] = {"title": news.title, "queries": list(_search_queries(news)),
                             "results": [r.model_dump(mode="json") for r in rows], "reused": news.id in cached_searches}
        (output / "search_results.json").write_text(json.dumps(searches, ensure_ascii=False, indent=2), encoding="utf-8")
        return rows
    runner = ReplayRunner(issue=frozen_issue, input_path=docx, x_candidates=x_candidates,
                          posts=posts, store=store, config=config,
                          search_provider=search_provider if args.search else None)
    request = TaskRequest(input_path=docx, issue_id=frozen_issue.issue_id, output_dir=output / "replay",
                          no_videos=True, use_socialdata_x=False, selected_sections=["z"])
    task_result = runner.run(request)
    pipeline_result = task_result.pipeline_result
    sample_result = None
    if sample_candidates:
        sample_news = NewsItem(issue_id="eeo-sample", sequence=1, section="周边", title=f"{EEO_SAMPLE_WORK} 商品验证样例",
            body="独立商品页解析样例；不是本期新闻。", game_names=[EEO_SAMPLE_WORK],
            source_urls=[url for url, _, _ in sample_seeds])
        sample_runner = ReplayRunner(issue=Issue(issue_id="eeo-sample", input_path=str(docx), news_items=[sample_news]),
            input_path=docx, x_candidates={}, posts={}, store=store, config=config)
        sample_task = sample_runner.run(TaskRequest(input_path=docx, issue_id="eeo-sample", output_dir=output / "replay_eeo",
            no_videos=True, use_socialdata_x=False, selected_sections=["z"]))
        sample_result = sample_task.pipeline_result
    # Fixture misses from the closed-world replay are visible and traceable.
    for url, status in store.statuses.items():
        if status.get("status") in {"fetch_failed", "fixture_missing"}:
            failures.append({"url": url, "stage": "closed_world_replay", "status": status.get("status"), "reason": status.get("reason", "")})
    if pipeline_result:
        failures.extend({"stage": "pipeline", "news_id": f.news_id, "url": f.source_url,
                         "status": f.code, "reason": f.message} for f in pipeline_result.failures)
    build_reports(output, frozen_issue, baseline_candidates, task_result, failures, store, sample_seeds,
                  sample_result, ignored_news_fields)
    metrics = {"runner": runner.network_metrics, "public_html_fetches": sum(v.get("kind") == "page" for v in store.statuses.values()),
               "public_image_fetches": sum(v.get("kind") == "image" for v in store.statuses.values()), "api_calls": 0,
               "task_status": task_result.status, "task_id": task_result.task_id,
               "goods_search_news_count": len(searches)}
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Validation artifacts written to {output}")
    print(f"Task status: {task_result.status}; API calls: 0; report: {output / 'report.html'}")
    return 0 if task_result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
