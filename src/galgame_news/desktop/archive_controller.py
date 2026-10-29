"""Async coordinator for offline archive lookup and explicit material actions."""

from __future__ import annotations

import json
import hashlib
import re
from concurrent.futures import CancelledError, Future, TimeoutError
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ..archive import ArchiveAsset, ArchiveIndex, LocalArchiveProvider, NextcloudArchiveProvider
from ..archive.materialize import ArchiveMaterializer
from ..tasks.repository import TaskRepository
from ..concurrency import DaemonTaskPool


class ArchiveController(QObject):
    """Keep archive I/O off the GUI thread and discard stale panel results."""

    OPERATION_TIMEOUT_SECONDS = 60.0
    INDEX_REFRESH_TIMEOUT_SECONDS = 600.0

    busy_changed = Signal(bool)

    def __init__(self, desktop, *, index_factory: Callable[[Path], Any] | None = None,
                 provider_factory: Callable[[Any, str], Any] | None = None,
                 materializer_factory: Callable[..., Any] | None = None):
        super().__init__(desktop)
        self.desktop = desktop
        self.index_factory = index_factory or ArchiveIndex
        self.provider_factory = provider_factory
        self.materializer_factory = materializer_factory or ArchiveMaterializer
        self._executor = DaemonTaskPool(max_workers=1, thread_name_prefix="archive-operation")
        self._future: Future | None = None
        self._cancelled: Event | None = None
        self._identity: tuple | None = None
        self._completion: Callable[[Any, BaseException | None], None] | None = None
        self._pending_query: tuple[str, list[str] | None] | None = None
        self._deadline_at: float | None = None
        self._operation_timeout_seconds: float | None = None
        self._query_generation = 0
        self._closed = False
        self._poll = QTimer(self)
        self._poll.setInterval(10)
        self._poll.timeout.connect(self._consume)

    @property
    def busy(self) -> bool:
        return self._future is not None

    def _settings(self):
        return self.desktop.settings_store.load().archive

    def _record(self):
        return self.desktop.review_record or self.desktop.current_record

    def _enabled(self) -> bool:
        record = self._record()
        if record is None:
            return bool(getattr(self.desktop.current_request, "use_archive_materials", False))
        options = self.desktop.task_store.load_request_options(record)
        if "use_archive_materials" in options:
            return bool(options.get("use_archive_materials"))
        request = self.desktop.current_request
        return bool(request is not None and request.task_id == record.task_id
                    and request.use_archive_materials)

    def _issue_id(self) -> str:
        record = self._record()
        request = self.desktop.current_request
        if record is not None and record.issue_id:
            return str(record.issue_id)
        return str(request.issue_id) if request is not None else ""

    def _index_path(self) -> Path:
        return Path(self.desktop.task_store.root) / "archive" / "index.sqlite3"

    @staticmethod
    def _safe_news_id(news_id: str) -> str:
        label = re.sub(r"[^0-9A-Za-z._-]+", "_", str(news_id)).strip("._") or "news"
        digest = hashlib.sha256(str(news_id).encode("utf-8")).hexdigest()[:12]
        return f"{label[:80]}_{digest}"

    def _snapshot_path(self, record, news_id: str) -> Path:
        root = self.desktop.task_store.storage_root(record)
        return root / "archive" / f"{self._safe_news_id(news_id)}.json"

    def _request_offline(self, record) -> bool:
        if record is not None:
            options = self.desktop.task_store.load_request_options(record)
            if "offline" in options:
                return bool(options["offline"])
        request = self.desktop.current_request
        return bool(request.offline) if request is not None else False

    def _no_videos(self, record) -> bool:
        request = self.desktop.current_request
        if request is not None and record is not None and request.task_id == record.task_id:
            return bool(request.no_videos)
        options = self.desktop.task_store.load_request_options(record) if record is not None else {}
        return bool(options.get("no_videos", False))

    def _archive_range(self, record) -> str:
        request = self.desktop.current_request
        if request is not None and record is not None and request.task_id == record.task_id:
            value = getattr(request, "archive_range", "year")
        else:
            options = self.desktop.task_store.load_request_options(record) if record is not None else {}
            value = options.get("archive_range", "year")
        return value if value in {"year", "all"} else "year"

    def _clear_preview(self) -> None:
        page = self.desktop.review_page
        clear = getattr(page, "clear_archive_preview", None)
        if callable(clear):
            clear()
        else:
            page._archive_preview_asset = None
            page._archive_preview_path = None
            page._clear_details()

    def _local_roots(self, settings) -> list[Path]:
        roots = [Path(value).expanduser() for value in settings.local_roots if str(value).strip()]
        for record in self.desktop.task_store.list_tasks():
            roots.append(Path(record.task_root))
            if record.source_output_path is not None:
                roots.append(record.source_output_path)
        unique = {}
        for root in roots:
            try:
                unique[str(root.resolve())] = root.resolve()
            except OSError:
                continue
        return list(unique.values())

    def _nextcloud(self, settings):
        password = self.desktop.credential_store.get("archive_app_password")
        if not (settings.server_url and settings.username and settings.root_path and password):
            return None
        if self.provider_factory is not None:
            return self.provider_factory(settings, password)
        return NextcloudArchiveProvider(settings.server_url, settings.username, password,
                                        settings.root_path,
                                        cache_dir=Path(self.desktop.task_store.root) / "archive" / "webdav-cache")

    @staticmethod
    def _close_provider(provider) -> None:
        transport = getattr(provider, "transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def _providers(self, settings, *, offline: bool):
        providers = {"local": LocalArchiveProvider(self._local_roots(settings))}
        if not offline:
            provider = self._nextcloud(settings)
            if provider is not None:
                providers["nextcloud"] = provider
        return providers

    def _submit(self, identity, function, completion, *, timeout_seconds=None) -> bool:
        if self._closed or self.busy:
            return False
        timeout_seconds = (self.OPERATION_TIMEOUT_SECONDS if timeout_seconds is None
                           else max(0.001, float(timeout_seconds)))
        cancelled = Event()
        self._identity = identity
        self._cancelled = cancelled
        self._completion = completion
        self._future = self._executor.submit(function, cancelled)
        self._deadline_at = time.monotonic() + timeout_seconds
        self._operation_timeout_seconds = timeout_seconds
        self._poll.start()
        self.busy_changed.emit(True)
        return True

    def _consume(self):
        future = self._future
        if future is None:
            return
        identity, completion = self._identity, self._completion
        if not future.done():
            if self._deadline_at is None or time.monotonic() < self._deadline_at:
                return
            if self._cancelled is not None:
                self._cancelled.set()
            future.cancel()
            timeout = self._operation_timeout_seconds or self.OPERATION_TIMEOUT_SECONDS
            self._retire_running_future(future)
            result, error = None, TimeoutError(
                f"archive operation exceeded its {timeout:g} second deadline")
        else:
            try:
                result, error = future.result(), None
            except BaseException as exc:
                result, error = None, exc
        self._future = self._cancelled = self._completion = self._identity = None
        self._deadline_at = None
        self._operation_timeout_seconds = None
        self._poll.stop()
        if completion is not None and not self._closed and not self._is_stale(identity):
            completion(result, error)
        self.busy_changed.emit(False)
        pending, self._pending_query = self._pending_query, None
        if pending is not None and not self._closed:
            QTimer.singleShot(0, self, lambda: self.query(pending[0], game_names=pending[1]))

    def _is_stale(self, identity) -> bool:
        if not identity:
            return False
        record = self._record()
        return (identity[0] != (record.task_id if record else None)
                or identity[1] != id(self.desktop.review_page.session)
                or identity[2] != self.desktop.review_page.archive_panel.current_news_id
                or (len(identity) > 3 and identity[3] != self._query_generation))

    def cancel(self) -> None:
        self._pending_query = None
        if self._cancelled is not None:
            self._cancelled.set()
        if self._future is not None:
            future = self._future
            if not future.cancel() and not future.done():
                self._retire_running_future(future)
                self.busy_changed.emit(False)
            else:
                # A queued or just-completed operation must not deliver a late
                # success after explicit cancellation.
                future.add_done_callback(self._consume_detached_result)
                self._future = self._cancelled = self._completion = self._identity = None
                self._deadline_at = self._operation_timeout_seconds = None
                self._poll.stop()
                self.busy_changed.emit(False)

    def _retire_running_future(self, future: Future) -> None:
        # Python cannot stop a running filesystem/provider call. Retire its
        # daemon worker so it cannot block later work or process exit.
        future.add_done_callback(self._consume_detached_result)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._future = self._cancelled = self._completion = self._identity = None
        self._deadline_at = None
        self._operation_timeout_seconds = None
        self._poll.stop()
        if not self._closed:
            self._executor = DaemonTaskPool(max_workers=1, thread_name_prefix="archive-operation")

    @staticmethod
    def _consume_detached_result(future: Future) -> None:
        try:
            future.exception()
        except BaseException:
            pass

    @staticmethod
    def _check_cancelled(cancelled: Event) -> None:
        if cancelled.is_set():
            raise CancelledError()

    def close(self, *, wait: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        if self.busy and not wait:
            # Keep only the GUI-side future poll alive so has_pending_work can
            # settle after cooperative cancellation; no result reaches widgets.
            self._poll.start()
        else:
            self._poll.stop()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def query(self, news_id: str, *, game_names=None) -> bool:
        panel = self.desktop.review_page.archive_panel
        self._query_generation += 1
        generation = self._query_generation
        self._clear_preview()
        panel.set_assets(str(news_id), [], "正在准备历史素材查询…")
        if self.busy:
            self._pending_query = (str(news_id), list(game_names) if game_names is not None else None)
            panel.current_news_id = str(news_id)
            panel.set_status("正在完成上一条历史查询；随后读取当前新闻…")
            return True
        record = self._record()
        if not self._enabled():
            panel.set_assets(str(news_id), [], "此任务未启用往期周报素材检索")
            return False
        session = self.desktop.review_page.session
        news = next((item for item in (session.news_items if session else [])
                     if str(item.get("news_id")) == str(news_id)), {})
        values = game_names or news.get("game_names") or news.get("aliases") or []
        if isinstance(values, str):
            values = [values]
        terms = [str(value).strip() for value in values if str(value).strip()]
        if not terms:
            title = str(news.get("title") or "")
            terms = re.findall(r"《([^》]+)》", title)
        if not terms:
            panel.set_assets(str(news_id), [], "缺少作品名，未查询历史索引")
            return False
        if record is None:
            panel.set_assets(str(news_id), [], "无法确定本期期号")
            return False
        issue = self._issue_id()
        if not issue:
            panel.set_assets(str(news_id), [], "无法确定本期期号")
            return False
        panel.current_news_id = str(news_id)
        panel.set_status("正在查询本地历史索引…")
        identity = (record.task_id, id(session), str(news_id), generation)
        settings = self._settings()
        no_videos = self._no_videos(record)
        archive_range = self._archive_range(record)
        days = 365 if archive_range == "year" else None
        snapshot_path = self._snapshot_path(record, str(news_id))

        index_path = self._index_path()
        if not index_path.is_file():
            has_nextcloud = bool(settings.server_url and settings.username and settings.root_path
                                 and self.desktop.credential_store.get("archive_app_password"))
            if not settings.local_roots and not has_nextcloud:
                panel.set_assets(str(news_id), [], "历史索引尚未建立；本地历史目录和 Nextcloud 均未配置。已登记任务输出仍可在手动更新时索引")
            else:
                panel.set_assets(str(news_id), [], "历史索引尚未建立，请在设置中手动更新索引")
            return False

        def work(cancelled):
            saved = {}
            if snapshot_path.is_file():
                try:
                    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    pass
            index = self.index_factory(self._index_path())
            try:
                index_stamp = index.last_updated
                indexed_assets = index.list_assets()
                if not indexed_assets:
                    return [], "历史索引已建立，但当前没有可用的历史素材；请检查目录或索引范围"
                if (saved.get("issue_id") == issue and saved.get("terms") == terms
                        and saved.get("no_videos") == no_videos
                        and saved.get("archive_range", "year") == archive_range
                        and saved.get("result_version") == 4
                        and saved.get("index_updated") == index_stamp):
                    return [ArchiveAsset.from_dict(value) for value in saved.get("assets", [])], "已载入任务内缓存的历史结果"
                assets = index.search(terms, current_issue=issue, no_videos=no_videos,
                                      days=days)
            finally:
                index.close()
            selected = []
            for asset in assets:
                if no_videos and asset.kind == "video":
                    continue
                if asset.kind not in {"image", "video"}:
                    continue
                selected.append(asset)
            self._check_cancelled(cancelled)
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self._check_cancelled(cancelled)
            snapshot_path.write_text(json.dumps({"issue_id": issue, "terms": terms,
                "no_videos": no_videos, "archive_range": archive_range,
                "result_version": 4,
                "index_updated": index_stamp,
                "assets": [asset.to_dict() for asset in selected]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            return selected, "找到匹配的历史素材" if selected else "历史索引中没有符合本期号、作品名和时间范围的素材"

        return self._submit(identity, work, lambda value, error: self._query_done(news_id, value, error))

    def _query_done(self, news_id, value, error):
        panel = self.desktop.review_page.archive_panel
        if error:
            panel.set_assets(news_id, [], f"历史索引查询失败：{error}")
            return
        assets, message = value
        panel.set_assets(news_id, assets, message)

    def refresh_index(self, settings=None) -> bool:
        settings = settings or self._settings()
        local_roots = self._local_roots(settings)

        def work(cancelled):
            reports = []
            local = LocalArchiveProvider(local_roots)
            try:
                records = local.scan(days=settings.range_days, cancelled=cancelled)
                self._check_cancelled(cancelled)
                index = self.index_factory(self._index_path())
                try:
                    self._check_cancelled(cancelled)
                    index.replace_source("local", records)
                finally:
                    index.close()
                reports.append(f"本地索引更新 {len(records)} 项")
            except CancelledError:
                raise
            except BaseException as exc:
                reports.append(f"本地索引失败：{exc}")
            try:
                self._check_cancelled(cancelled)
                provider = self._nextcloud(settings)
                if provider is not None:
                    records = provider.scan(days=settings.range_days, cancelled=cancelled)
                    self._check_cancelled(cancelled)
                    index = self.index_factory(self._index_path())
                    try:
                        self._check_cancelled(cancelled)
                        index.replace_source("nextcloud", records)
                    finally:
                        index.close()
                    reports.append(f"Nextcloud 索引更新 {len(records)} 项")
                    self._close_provider(provider)
                else:
                    reports.append("Nextcloud 未配置，已保留可用的本地索引")
            except CancelledError:
                self._close_provider(locals().get("provider"))
                raise
            except BaseException as exc:
                self._close_provider(locals().get("provider"))
                reports.append(f"Nextcloud 索引失败，保留上次索引：{exc}")
            return "；".join(reports)

        def done(value, error):
            self.desktop.settings_page.set_archive_status(f"索引更新失败：{error}" if error else value)
        return self._submit(None, work, done,
                            timeout_seconds=self.INDEX_REFRESH_TIMEOUT_SECONDS)

    def test_connection(self, settings=None) -> bool:
        settings = settings or self._settings()
        def work(cancelled):
            provider = self._nextcloud(settings)
            if provider is None:
                local = LocalArchiveProvider(self._local_roots(settings))
                return ("本地历史目录可用" if local.test_connection() else "未配置可用的本地或 Nextcloud 历史库")
            try:
                return "Nextcloud 连接成功" if provider.test_connection() else "Nextcloud 连接失败"
            finally:
                self._close_provider(provider)
        return self._submit(None, work,
                            lambda value, error: self.desktop.settings_page.set_archive_status(
                                f"连接测试失败：{error}" if error else value))

    def activate(self, asset: ArchiveAsset) -> bool:
        """Load only an indexed thumbnail when the user activates a card."""
        if not self._enabled():
            return False
        record = self._record()
        session = self.desktop.review_page.session
        news_id = self.desktop.review_page.archive_panel.current_news_id or ""
        if record is None or session is None:
            return False
        panel = self.desktop.review_page.archive_panel
        panel.set_status("正在读取历史预览…")
        identity = (record.task_id, id(session), news_id, self._query_generation)
        offline = self._request_offline(record)
        def work(cancelled):
            providers = []
            materializer = self._materializer(offline=offline, providers=providers)
            try:
                return materializer.get_preview(asset, cancelled=cancelled)
            finally:
                for provider in providers:
                    self._close_provider(provider)
        return self._submit(identity, work, lambda path, error: self._preview_done(
            panel, asset, path, error, explicit=False))

    def preview(self, asset: ArchiveAsset) -> bool:
        """Explicit preview action may fetch the full asset into the cache."""
        if not self._enabled():
            return False
        record = self._record()
        session = self.desktop.review_page.session
        news_id = self.desktop.review_page.archive_panel.current_news_id or ""
        if record is None or session is None:
            return False
        panel = self.desktop.review_page.archive_panel
        panel.set_status("正在读取历史素材…")
        identity = (record.task_id, id(session), news_id, self._query_generation)
        offline = self._request_offline(record)
        def work(cancelled):
            providers = []
            materializer = self._materializer(offline=offline, providers=providers)
            try:
                return materializer.get_original(asset, cancelled=cancelled)
            finally:
                for provider in providers:
                    self._close_provider(provider)
        return self._submit(identity, work, lambda path, error: self._preview_done(
            panel, asset, path, error, explicit=True))

    @staticmethod
    def _preview_done(panel, asset, path, error, *, explicit):
        page = panel.parent()
        while page is not None and not callable(getattr(page, "show_archive_preview", None)):
            page = page.parent()
        show = getattr(page, "show_archive_preview", None)
        if error:
            panel.set_status(f"预览失败：{error}")
            if callable(show):
                show(asset, None, original=explicit)
        elif path is None:
            panel.set_status("该素材没有可用预览")
            if callable(show):
                show(asset, None, original=explicit)
        else:
            panel.set_status(f"历史素材已缓存：{path}" if explicit else f"预览已缓存：{path}")
            if callable(show):
                show(asset, path, original=explicit)
                if explicit and asset.kind == "video":
                    page.play_video()

    def adopt(self, asset: ArchiveAsset) -> bool:
        if not self._enabled():
            return False
        page = self.desktop.review_page
        if (page.exporter.busy or page._export_waiting_session is not None
                or self.desktop.is_running or self.desktop.is_supplement_running
                or self.desktop._migration_active):
            page.archive_panel.set_status("已有导出、抓取或迁移操作正在运行，请结束后再采用历史素材")
            return False
        record = self._record()
        session = page.session
        news_id = page.archive_panel.current_news_id or ""
        if record is None or session is None or not news_id:
            return False
        panel = page.archive_panel
        duplicate = self._existing_candidate(session, asset, news_id)
        if duplicate is not None:
            panel.set_status("本期已有同来源素材，请在原候选中选择")
            return False
        panel.set_status("正在验证并采用历史素材…")
        identity = (record.task_id, id(session), news_id, self._query_generation)
        snapshot = session.query_snapshot()
        offline = self._request_offline(record)
        storage_root = self.desktop.task_store.storage_root(record)
        task_root = Path(session.retry_root).parent if session.retry_root is not None else Path(session.task_root)
        app_data = Path(self.desktop.task_store.root)

        def work(cancelled):
            providers = []
            materializer = self._materializer(app_data, offline=offline, providers=providers)
            try:
                candidate = materializer.prepare(asset, news_id=news_id, task_root=task_root, cancelled=cancelled)
            finally:
                for provider in providers:
                    self._close_provider(provider)
            attempt_id = candidate.archive_provenance["attempt_id"]
            attempt_path = task_root / "retries" / news_id / attempt_id
            image_payload = json.loads((attempt_path / "image_index.json").read_text(encoding="utf-8"))
            video_payload = json.loads((attempt_path / "video_index.json").read_text(encoding="utf-8"))
            images = image_payload.get("candidates", [])
            videos = video_payload.get("videos", [])
            if TaskRepository.is_authoritative(storage_root):
                self._check_cancelled(cancelled)
                TaskRepository(storage_root, create=False).save_batch(
                    news_id, attempt_id, str(attempt_path), "retry", "completed", images, videos, [],
                    source_url=candidate.source_url)
            from ..tasks.store import TaskStore
            self._check_cancelled(cancelled)
            store = TaskStore(app_data, recover_on_startup=False)
            try:
                self._check_cancelled(cancelled)
                store.register_retry_attempt(record, news_id, attempt_id, kind="retry", path=attempt_path)
            finally:
                store.close()
            delta = snapshot.prepare_retry_delta(news_id, attempt_id)
            return candidate, delta

        return self._submit(identity, work, lambda value, error: self._adopt_done(
            identity, session, value, error))

    @staticmethod
    def _existing_candidate(session, asset: ArchiveAsset, news_id: str):
        source_url = (asset.original_source_url or asset.original_url
                      or f"archive://{asset.source_id}/{asset.asset_id}")
        media_url = asset.original_url or source_url
        values = session.images if asset.kind == "image" else session.videos
        url_field = "image_url" if asset.kind == "image" else "video_url"
        return next((candidate for candidate in values
                     if str(candidate.news_id) == str(news_id)
                     and str(getattr(candidate, url_field, "")) == str(media_url)), None)

    def _adopt_done(self, identity, session, value, error):
        panel = self.desktop.review_page.archive_panel
        if error:
            panel.set_status(f"采用失败：{error}")
            return
        candidate, delta = value
        if self.desktop.review_page.session is not session or self._is_stale(identity):
            return
        try:
            session.apply_retry_delta(delta, persist=False)
            available = ({str(item.id) for item in session.images}
                         | {str(item.id) for item in session.videos})
            candidate_id = str(candidate.id) if candidate.id is not None else ""
            if not candidate_id or candidate_id not in available:
                panel.set_status("本期已有同来源素材，请在原候选中选择")
                return
            ids = [candidate_id]
            session.set_decisions(ids, "accepted", persist=False)
            self.desktop.review_page.state_writer.enqueue(session)
            self.desktop.review_page.refresh(preserve_position=True)
            panel.set_status("历史素材已采用并保存到当前新闻审核")
        except BaseException as exc:
            panel.set_status(f"采用后合并失败，批次已保留供恢复：{exc}")

    def _materializer(self, app_data=None, *, offline=False, providers=None):
        from ..config import load_config
        data = Path(app_data or self.desktop.task_store.root)
        providers = providers if providers is not None else []
        def lookup(source_id):
            provider = self._provider_by_id(source_id, offline=offline)
            providers.append(provider)
            return provider
        return self.materializer_factory(data / "archive" / "cache",
            lookup, config=load_config())

    def _provider_by_id(self, source_id, *, offline=False):
        settings = self._settings()
        if source_id == "local":
            return LocalArchiveProvider(self._local_roots(settings))
        if offline:
            raise RuntimeError("离线模式不能从 Nextcloud 获取素材；可使用已缓存文件")
        provider = self._nextcloud(settings)
        if provider is None:
            raise RuntimeError("Nextcloud 历史库未配置")
        return provider
