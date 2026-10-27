from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from galgame_news.archive.models import ArchiveAsset
from galgame_news.archive.materialize import ArchiveMaterializer
from galgame_news.desktop.archive_controller import ArchiveController
from galgame_news.desktop.controller import DesktopController
from galgame_news.domain import ImageCandidate
from galgame_news.pipeline import TaskRequest
from galgame_news.review import ReviewSession, ReviewDecision
from galgame_news.tasks.repository import TaskRepository
from galgame_news.settings import ArchiveSettings
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend


def _enabled_desktop(tmp_path, qtbot, *, no_videos=False):
    app = DesktopController(app_data=tmp_path / "app",
        credential_store=CredentialStore(InMemoryCredentialBackend()))
    qtbot.addWidget(app.review_page)
    record = app.task_store.create_task("231", tmp_path / "issue.docx")
    app.review_record = record
    app.task_store.save_request_options(record, {
        "use_archive_materials": True,
        "no_videos": no_videos,
        "offline": False,
    })
    db = Path(app.task_store.root) / "archive" / "index.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    return app, record


def test_manual_index_scan_failure_keeps_existing_source(monkeypatch, qtbot, tmp_path):
    import galgame_news.desktop.archive_controller as module

    calls = []

    class Index:
        def __init__(self, _path):
            pass

        def replace_source(self, source_id, assets):
            calls.append((source_id, assets))

        def close(self):
            pass

    class BrokenLocalProvider:
        def __init__(self, _roots):
            pass

        def scan(self, **_kwargs):
            raise OSError("test scan failure")

    monkeypatch.setattr(module, "LocalArchiveProvider", BrokenLocalProvider)
    app = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(app.settings_page)
    controller = ArchiveController(app, index_factory=Index)
    settings = ArchiveSettings(local_roots=[str(tmp_path / "local")])

    assert controller.refresh_index(settings)
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == []
    assert "本地索引失败" in app.settings_page.archive_status.text()
    assert "未配置" in app.settings_page.archive_status.text()
    controller.close(wait=True)
    app.archive_controller.close(wait=True)
    app.task_store.close()


def test_manual_update_replaces_only_sources_whose_scan_succeeded(monkeypatch, qtbot, tmp_path):
    import galgame_news.desktop.archive_controller as module

    calls = []

    class Index:
        def __init__(self, _path):
            pass

        def replace_source(self, source_id, assets):
            calls.append((source_id, list(assets)))

        def close(self):
            pass

    class Local:
        def __init__(self, _roots):
            pass

        def scan(self, **_kwargs):
            return [ArchiveAsset("local-new", "local", "image", "230", "a", "a.png", ("Game",))]

    class FailedNextcloud:
        def scan(self, **_kwargs):
            raise OSError("remote metadata scan failed")

    monkeypatch.setattr(module, "LocalArchiveProvider", Local)
    app = DesktopController(app_data=tmp_path / "app",
        credential_store=CredentialStore(InMemoryCredentialBackend()))
    qtbot.addWidget(app.settings_page)
    app.credential_store.set("archive_app_password", "test-password")
    controller = ArchiveController(app, index_factory=Index,
        provider_factory=lambda *_args: FailedNextcloud())
    settings = ArchiveSettings(server_url="https://cloud.invalid", username="user",
                                root_path="/weekly", local_roots=[str(tmp_path / "local")])

    assert controller.refresh_index(settings)
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == [("local", [ArchiveAsset("local-new", "local", "image", "230", "a", "a.png", ("Game",))])]
    status = app.settings_page.archive_status.text()
    assert "本地索引更新 1 项" in status
    assert "Nextcloud 索引失败，保留上次索引" in status
    assert "test-password" not in status
    controller.close(wait=True)
    app.task_store.close()


def test_card_activation_uses_preview_api_without_fetching_original(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    asset = ArchiveAsset("remote-image", "nextcloud", "image", "230", "x.png", "x.png",
                         ("Game",), source_kind="nextcloud", preview_url="https://example.invalid/preview")
    calls = []
    preview_path = tmp_path / "preview.jpg"

    class Materializer:
        def get_preview(self, candidate, cancelled=None):
            calls.append(("preview", candidate.asset_id))
            return preview_path

        def get_original(self, *_args, **_kwargs):
            calls.append(("original",))
            raise AssertionError("preview must not fetch the original")

    controller = ArchiveController(app, materializer_factory=lambda *_args, **_kwargs: Materializer())
    app.archive_controller = controller
    app.review_page.archive_panel.current_news_id = "news-1"

    assert controller.activate(asset)
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == [("preview", "remote-image")]
    assert str(preview_path) in app.review_page.archive_panel.message_label.text()
    assert app.review_page._archive_preview_path == preview_path
    controller.close(wait=True)
    app.task_store.close()


def test_explicit_preview_action_fetches_original(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    asset = ArchiveAsset("remote-video", "nextcloud", "video", "230", "x.mp4", "x.mp4",
                         ("Game",), source_kind="nextcloud", original_url="https://example.invalid/x.mp4")
    original_path = tmp_path / "original.mp4"
    calls = []

    class Materializer:
        def get_original(self, candidate, cancelled=None):
            calls.append(("original", candidate.asset_id))
            return original_path

        def get_preview(self, *_args, **_kwargs):
            raise AssertionError("explicit preview uses the cached original")

    controller = ArchiveController(app, materializer_factory=lambda *_args, **_kwargs: Materializer())
    app.archive_controller = controller
    app.review_page.archive_panel.current_news_id = "news-1"

    assert controller.preview(asset)
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == [("original", "remote-video")]
    assert str(original_path) in app.review_page.archive_panel.message_label.text()
    assert app.review_page._archive_preview_path == original_path
    controller.close(wait=True)
    app.task_store.close()


def test_switching_news_discards_old_async_query_and_runs_latest(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    first = ArchiveAsset("one", "local", "image", "230", "one", "one.png", ("Game One",))
    second = ArchiveAsset("two", "local", "image", "230", "two", "two.png", ("Game Two",))

    class Index:
        def __init__(self, _path):
            pass

        def search(self, terms, **_kwargs):
            if terms == ["Game One"]:
                return [first]
            return [second]

        @property
        def last_updated(self):
            return "2026-10-27T00:00:00+00:00"

        def list_assets(self):
            return [first, second]

        def close(self):
            pass

    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)

    assert controller.query("news-1", game_names=["Game One"])
    assert controller.query("news-2", game_names=["Game Two"])
    first_future = controller._future
    first_future.result(timeout=3)
    controller._consume()
    qtbot.waitUntil(lambda: controller.busy, timeout=1000)
    controller._future.result(timeout=3)
    controller._consume()
    qtbot.waitUntil(lambda: not controller.busy and app.review_page.archive_panel.asset_count == 1,
                    timeout=4000)
    assert app.review_page.archive_panel.current_news_id == "news-2"
    item = app.review_page.archive_panel.list_widget.item(0)
    assert "two.png" in item.text()
    controller.close(wait=True)
    app.task_store.close()


def test_archive_queries_use_local_index_when_offline_and_do_not_construct_remote_provider(qtbot, tmp_path):
    app, record = _enabled_desktop(tmp_path, qtbot)
    app.task_store.save_request_options(record, {
        "use_archive_materials": True, "offline": True, "no_videos": False,
    })
    asset = ArchiveAsset("local-image", "local", "image", "230", "one", "one.png", ("Game",))
    calls = []

    class Index:
        def __init__(self, _path):
            pass

        def search(self, terms, **kwargs):
            calls.append((terms, kwargs))
            return [asset]

        @property
        def last_updated(self):
            return "2026-10-27T00:00:00+00:00"

        def list_assets(self):
            return [asset]

        def close(self):
            pass

    controller = ArchiveController(app, index_factory=Index,
                                   provider_factory=lambda *_args: (_ for _ in ()).throw(AssertionError("remote provider")))
    app.archive_controller = controller
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)

    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls and calls[0][0] == ["Game"]
    assert app.review_page.archive_panel.asset_kinds == ["image"]
    controller.close(wait=True)
    app.task_store.close()


def test_index_timestamp_change_invalidates_cached_news_snapshot(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    first = ArchiveAsset("first", "local", "image", "230", "first", "first.png", ("Game",))
    second = ArchiveAsset("second", "local", "image", "230", "second", "second.png", ("Game",))
    state = {"stamp": "index-one", "asset": first}
    calls = []

    class Index:
        def __init__(self, _path):
            pass

        @property
        def last_updated(self):
            return state["stamp"]

        def list_assets(self):
            return [state["asset"]]

        def search(self, *_args, **_kwargs):
            calls.append(state["stamp"])
            return [state["asset"]]

        def close(self):
            pass

    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == ["index-one"]
    state.update(stamp="index-two", asset=second)
    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert calls == ["index-one", "index-two"]
    assert "second.png" in app.review_page.archive_panel.list_widget.item(0).text()
    controller.close(wait=True)
    app.task_store.close()


def test_archive_range_change_invalidates_snapshot_even_when_index_is_unchanged(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    asset = ArchiveAsset("old", "local", "image", "230", "old", "old.png", ("Game",))
    observed_days = []

    class Index:
        def __init__(self, _path):
            pass

        @property
        def last_updated(self):
            return "unchanged-index"

        def list_assets(self):
            return [asset]

        def search(self, _terms, **kwargs):
            observed_days.append(kwargs["days"])
            return [asset]

        def close(self):
            pass

    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)

    options = app.task_store.load_request_options(_record)
    options["archive_range"] = "all"
    app.task_store.save_request_options(_record, options)
    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)

    assert observed_days == [365, None]
    controller.close(wait=True)
    app.task_store.close()


def test_same_news_keyword_change_ignores_previous_async_result(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    first = ArchiveAsset("first-keyword", "local", "image", "230", "first", "first.png", ("Game A",))
    second = ArchiveAsset("second-keyword", "local", "image", "230", "second", "second.png", ("Game B",))

    class Index:
        def __init__(self, _path):
            pass

        @property
        def last_updated(self):
            return "stable"

        def list_assets(self):
            return [first, second]

        def search(self, terms, **_kwargs):
            if terms == ["Game A"]:
                return [first]
            return [second]

        def close(self):
            pass

    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    assert controller.query("news-1", game_names=["Game A"])
    assert controller.query("news-1", game_names=["Game B"])
    assert app.review_page.archive_panel.asset_count == 0
    controller._future.result(timeout=3)
    controller._consume()
    qtbot.waitUntil(lambda: controller.busy, timeout=1000)
    controller._future.result(timeout=3)
    controller._consume()
    qtbot.waitUntil(lambda: not controller.busy and app.review_page.archive_panel.asset_count == 1,
                    timeout=4000)
    assert "second.png" in app.review_page.archive_panel.list_widget.item(0).text()
    controller.close(wait=True)
    app.task_store.close()


def test_matching_current_request_no_videos_overrides_stale_saved_default(qtbot, tmp_path):
    app, record = _enabled_desktop(tmp_path, qtbot)
    app.task_store.save_request_options(record, {
        "use_archive_materials": True, "no_videos": False, "offline": False,
    })
    app.current_request = TaskRequest(input_path=tmp_path / "issue.docx", issue_id="231",
        output_dir=record.root_path.parent, task_id=record.task_id, no_videos=True,
        use_archive_materials=True)
    image = ArchiveAsset("i", "local", "image", "230", "i", "i.png", ("Game",))
    video = ArchiveAsset("v", "local", "video", "230", "v", "v.mp4", ("Game",))
    observed = []

    class Index:
        def __init__(self, _path):
            pass

        @property
        def last_updated(self):
            return "stamp"

        def list_assets(self):
            return [image, video]

        def search(self, *_args, **kwargs):
            observed.append(kwargs["no_videos"])
            return [image, video]

        def close(self):
            pass

    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    assert controller.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: not controller.busy, timeout=3000)
    assert observed == [True]
    assert app.review_page.archive_panel.asset_kinds == ["image"]
    controller.close(wait=True)
    app.task_store.close()


def test_query_reports_unbuilt_index_without_creating_database(qtbot, tmp_path):
    app, _record = _enabled_desktop(tmp_path, qtbot)
    index_path = Path(app.task_store.root) / "archive" / "index.sqlite3"
    index_path.unlink()
    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    queried = []

    class Index:
        def __init__(self, _path):
            queried.append(True)
            raise AssertionError("unbuilt index must not be opened")

    controller = ArchiveController(app, index_factory=Index)
    app.archive_controller = controller
    assert controller.query("news-1", game_names=["Game"]) is False
    assert not index_path.exists()
    assert queried == []
    assert "尚未建立" in app.review_page.archive_panel.message_label.text()
    controller.close(wait=True)
    app.task_store.close()


def test_adopted_archive_asset_is_saved_as_retry_and_restores_accepted_after_reopen(qtbot, tmp_path):
    app, record = _enabled_desktop(tmp_path, qtbot)
    task_root = record.root_path
    raw = task_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    news = [{"news_id": "news-1", "sequence": 1, "section": "新作", "title": "《Game》发布新作"}]
    repository = TaskRepository(task_root)
    repository.activate()
    app.task_store.save_request_options(record, {
        "use_archive_materials": True, "no_videos": False, "offline": False,
    })
    app.current_request = TaskRequest(input_path=tmp_path / "issue.docx", issue_id="231",
        output_dir=task_root.parent, task_id=record.task_id, use_archive_materials=True)
    assert app.task_store.storage_root(record) == task_root
    assert TaskRepository.is_authoritative(task_root)
    session = ReviewSession(output_dir=raw, task_root=task_root, state_path=repository.path,
        images=[], videos=[], news_items=news, source_paths={}, raw_types={},
        review_ids=set(), failed_ids=set())
    session.retry_root = app.task_store.storage_root(record) / "retries"
    app.review_page.set_session(session)
    asset = ArchiveAsset("archive-1", "local", "image", "230", "asset.png", "asset.png", ("Game",),
                         source_kind="local")
    source = tmp_path / "source.png"
    Image.new("RGB", (480, 320), "navy").save(source)
    fetched = []

    class Provider:
        def fetch(self, _asset, target, max_bytes, cancelled=None):
            fetched.append(True)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return Path(target)

    provider = Provider()
    controller = ArchiveController(app, materializer_factory=lambda cache, _lookup, **kwargs:
        ArchiveMaterializer(cache, lambda _source_id: provider, **kwargs))
    app.archive_controller = controller
    app.review_page.archive_panel.current_news_id = "news-1"

    assert controller._enabled()
    assert app.review_page.session is session
    assert session.images == []
    assert session.pending_images("news-1") == []
    assert controller.adopt(asset)
    qtbot.waitUntil(lambda: not controller.busy, timeout=5000)
    qtbot.waitUntil(lambda: not app.review_page.state_writer.busy, timeout=5000)
    assert len(session.images) == 1, app.review_page.archive_panel.message_label.text()
    candidate = session.images[0]
    assert session.decision(candidate.id) is ReviewDecision.ACCEPTED
    assert len(fetched) == 1
    attempts = app.task_store.list_retry_attempts(record)
    assert len(attempts) == 1
    assert attempts[0].kind == "retry"

    assert controller.adopt(asset) is False
    assert len(session.images) == 1
    assert len(app.task_store.list_retry_attempts(record)) == 1
    assert len(fetched) == 1
    assert session.decision(candidate.id) is ReviewDecision.ACCEPTED

    reopened = ReviewSession(output_dir=raw, task_root=task_root, state_path=repository.path,
        images=[], videos=[], news_items=news, source_paths={}, raw_types={},
        review_ids=set(), failed_ids=set())
    reopened.retry_root = app.task_store.storage_root(record) / "retries"
    for attempt in attempts:
        reopened.merge_retry_attempt(attempt, persist=False)
    assert len(reopened.images) == 1
    assert reopened.decision(candidate.id) is ReviewDecision.ACCEPTED
    controller.close(wait=True)
    app.task_store.close()


@pytest.mark.parametrize("existing_decision", [ReviewDecision.REJECTED, ReviewDecision.PENDING])
def test_adopting_duplicate_source_does_not_fetch_or_override_existing_decision(
        qtbot, tmp_path, existing_decision):
    app, record = _enabled_desktop(tmp_path, qtbot)
    task_root = record.root_path
    raw = task_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    news = [{"news_id": "news-1", "sequence": 1, "section": "新作", "title": "《Game》发布新作"}]
    image_url = "https://archive.example/media.png"
    source_url = "https://archive.example/news"
    ordinary = ImageCandidate(news_id="news-1", image_url=image_url, source_url=source_url,
        fetched_at=datetime.now(timezone.utc), selected=False)
    (raw / "image_index.json").write_text(
        __import__("json").dumps({"issue_id": "231", "candidates": [ordinary.model_dump(mode="json")],
                                  "news_items": news}, ensure_ascii=False), encoding="utf-8")
    (raw / "video_index.json").write_text(
        __import__("json").dumps({"issue_id": "231", "videos": [], "news_items": news}, ensure_ascii=False),
        encoding="utf-8")
    session = ReviewSession.from_output(raw, task_root=task_root, state_path=record.review_state_path)
    session.retry_root = app.task_store.storage_root(record) / "retries"
    session.set_decisions([ordinary.id], existing_decision)
    app.review_page.set_session(session)
    asset = ArchiveAsset("archive-duplicate", "nextcloud", "image", "230", "/media.png", "media.png",
        ("Game",), source_kind="nextcloud", original_url=image_url, original_source_url=source_url)
    prepared = []

    class Materializer:
        def prepare(self, *_args, **_kwargs):
            prepared.append(True)
            raise AssertionError("duplicate archive source should be detected before fetching")

    controller = ArchiveController(app, materializer_factory=lambda *_args, **_kwargs: Materializer())
    app.archive_controller = controller
    app.review_page.archive_panel.current_news_id = "news-1"

    assert controller.adopt(asset) is False
    assert prepared == []
    assert len(session.images) == 1
    assert session.decision(ordinary.id) is existing_decision
    assert "本期已有同来源素材" in app.review_page.archive_panel.message_label.text()

    reopened = ReviewSession.from_output(raw, task_root=task_root, state_path=record.review_state_path)
    assert len(reopened.images) == 1
    assert reopened.decision(ordinary.id) is existing_decision
    controller.close(wait=True)
    app.task_store.close()
