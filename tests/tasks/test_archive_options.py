from galgame_news.tasks.repository import TaskRepository
from galgame_news.tasks.store import TaskStore
import sqlite3


def test_sqlite_archive_option_survives_reopen_without_persisting_credentials(tmp_path):
    repository = TaskRepository(tmp_path)
    repository.activate()
    repository.save_options({"use_archive_materials": True, "archive_range": "all", "archive_app_password": "must-not-save"})
    reopened = TaskRepository(tmp_path, create=False)
    assert reopened.load_options().get("use_archive_materials") is True
    assert reopened.load_options()["archive_range"] == "all"
    assert "archive_app_password" not in reopened.load_options()


def test_sqlite_archive_batch_upgrades_legacy_retry_columns_without_losing_rows(tmp_path):
    store = TaskStore(tmp_path / "app")
    record = store.create_task("231", tmp_path / "231.docx")
    with sqlite3.connect(record.root_path / "task_state.sqlite3") as db:
        db.execute("INSERT INTO retry_attempts VALUES(?,?,?,?)", ("old", "old-attempt", "old-path", "2026-01-01"))
    repository = TaskRepository(record.root_path)
    repository.activate()
    repository.save_batch("n1", "archive_1", record.root_path / "retries/n1/archive_1", "retry", "completed", [], [], [])
    assert repository.read_batch("n1", "archive_1")["kind"] == "retry"
    with sqlite3.connect(repository.path) as db:
        assert db.execute("SELECT path,created_at,kind FROM retry_attempts WHERE news_id='old'").fetchone() == ("old-path", "2026-01-01", "retry")
    store.close()
