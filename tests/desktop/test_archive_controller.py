from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from galgame_news.archive.models import ArchiveAsset
from galgame_news.desktop.controller import DesktopController
from galgame_news.pipeline import TaskRequest
from galgame_news.pipeline.checkpoint import config_hash
from galgame_news.config import load_config


class _Runner:
    def __init__(self, task_dir: Path, requests: list[TaskRequest]):
        self.task_dir = task_dir
        self.requests = requests

    def run(self, request, event_sink=None, cancellation_token=None):
        self.requests.append(request)
        return SimpleNamespace(status="completed", task_dir=self.task_dir)


def test_archive_materials_flag_is_saved_and_restored_on_resume(qtbot, tmp_path):
    source = tmp_path / "issue.docx"
    source.write_bytes(b"docx")
    task_dir = tmp_path / "output" / "task"
    requests = []
    controller = None

    def runner_factory():
        return _Runner(task_dir, requests)

    controller = DesktopController(runner_factory=runner_factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)

    assert controller.start_task(input_path=source, issue_id="231", output_dir=task_dir.parent,
                                 use_archive_materials=True)
    qtbot.waitUntil(lambda: not controller.is_running, timeout=3000)
    record = controller.task_store.active_task
    assert record is not None
    assert controller.task_store.load_request_options(record)["use_archive_materials"] is True

    assert controller.resume_task(record)
    qtbot.waitUntil(lambda: not controller.is_running, timeout=3000)
    assert requests[-1].use_archive_materials is True
    assert controller.close()


def test_checkpoint_hash_only_changes_when_archive_materials_enabled():
    config = load_config()
    base = TaskRequest(input_path=Path("input.docx"), issue_id="231", output_dir=Path("out"))
    enabled = TaskRequest(input_path=Path("input.docx"), issue_id="231", output_dir=Path("out"),
                          use_archive_materials=True)

    assert config_hash(config, base) == config_hash(config, TaskRequest(
        input_path=Path("input.docx"), issue_id="231", output_dir=Path("out")))
    assert config_hash(config, base) != config_hash(config, enabled)


def test_archive_controller_off_does_not_query_index_or_network(qtbot, tmp_path):
    from galgame_news.desktop.archive_controller import ArchiveController

    class Index:
        def __init__(self):
            self.queries = 0

        def search(self, *args, **kwargs):
            self.queries += 1
            raise AssertionError("feature-off must not query the index")

    index = Index()
    app = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(app.review_page)
    archives = ArchiveController(app, index_factory=lambda _path: index)
    app.archive_controller = archives

    assert archives.query("news-1") is False
    assert index.queries == 0
    archives.close()
    app.close()


def test_archive_query_returns_all_assets_and_honors_no_videos(qtbot, tmp_path):
    from galgame_news.desktop.archive_controller import ArchiveController

    assets = [
        ArchiveAsset("i1", "local", "image", "230", "a", "a.png", ("Game",)),
        ArchiveAsset("i2", "local", "image", "230", "b", "b.png", ("Game",)),
        ArchiveAsset("v1", "local", "video", "230", "a", "a.mp4", ("Game",)),
    ]
    searches = []

    class Index:
        def __init__(self):
            self.args = None
            self.last_updated = "now"

        def list_assets(self):
            return assets

        def search(self, *args, **kwargs):
            self.args = (args, kwargs)
            searches.append(self.args)
            return assets

        def close(self):
            pass

    app = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(app.review_page)
    archives = ArchiveController(app, index_factory=lambda _path: Index())
    app.archive_controller = archives

    # A loaded, explicitly enabled review request drives this query.
    app.review_record = app.task_store.create_task("231", tmp_path / "issue.docx")
    app.task_store.save_request_options(app.review_record,
                                        {"use_archive_materials": True, "no_videos": True,
                                         "archive_range": "all"})
    stale_snapshot = archives._snapshot_path(app.review_record, "news-1")
    stale_snapshot.parent.mkdir(parents=True, exist_ok=True)
    stale_snapshot.write_text(json.dumps({
        "issue_id": "231", "terms": ["Game"], "no_videos": True,
        "days": archives._settings().range_days, "index_updated": "now",
        "assets": [assets[0].to_dict()],
    }), encoding="utf-8")
    archive_db = Path(app.task_store.root) / "archive" / "index.sqlite3"
    archive_db.parent.mkdir(parents=True)
    archive_db.touch()
    # The controller's index adapter is queried in a worker and the panel is updated on the GUI thread.
    assert archives.query("news-1", game_names=["Game"])
    qtbot.waitUntil(lambda: app.review_page.archive_panel.asset_count > 0, timeout=3000)
    assert app.review_page.archive_panel.asset_count == 2
    assert app.review_page.archive_panel.asset_kinds == ["image", "image"]
    assert searches[-1][1]["days"] is None
    snapshot = json.loads(stale_snapshot.read_text(encoding="utf-8"))
    assert snapshot["archive_range"] == "all"
    assert snapshot["result_version"] == 3
    archives.close()
    app.close()


def test_force_close_cancels_archive_result_and_retires_pending_work(qtbot, tmp_path):
    from galgame_news.desktop.archive_controller import ArchiveController

    app = DesktopController(app_data=tmp_path / "app")
    qtbot.addWidget(app.review_page)
    record = app.task_store.create_task("231", tmp_path / "issue.docx")
    app.review_record = record
    app.task_store.save_request_options(record, {"use_archive_materials": True})
    index_path = Path(app.task_store.root) / "archive" / "index.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.touch()

    class Index:
        def __init__(self, _path):
            pass

        last_updated = "current"

        def list_assets(self):
            return [ArchiveAsset("i", "local", "image", "230", "a", "a.png", ("Game",))]

        def search(self, *_args, **_kwargs):
            return self.list_assets()

        def close(self):
            pass

    app.review_page.session = SimpleNamespace(news_items=[], task_root=tmp_path / "task", retry_root=None)
    archives = ArchiveController(app, index_factory=Index)
    app.archive_controller = archives
    assert archives.query("news-1", game_names=["Game"])
    archives._future.result(timeout=3)
    app.begin_force_close()
    assert app.has_pending_work
    qtbot.waitUntil(lambda: not archives.busy, timeout=3000)
    assert not app.has_pending_work
    assert app.review_page.archive_panel.asset_count == 0
