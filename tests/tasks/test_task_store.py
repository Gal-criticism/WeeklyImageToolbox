from __future__ import annotations

import json
from pathlib import Path

import pytest

from galgame_news.tasks.store import TaskStore


def test_create_task_materializes_contract_and_round_trips_history(tmp_path):
    source = tmp_path / "259.docx"
    source.write_bytes(b"docx fixture")
    store = TaskStore(tmp_path / "app-data")

    record = store.create_task("259", source, task_root=tmp_path / "task-259")

    assert record.issue_id == "259"
    assert record.task_root == str(tmp_path / "task-259")
    for name in ("raw", "retries", "final", "logs", ".work"):
        assert (tmp_path / "task-259" / name).is_dir()
    assert (tmp_path / "task-259" / "task_state.sqlite3").is_file()

    reopened = TaskStore(tmp_path / "app-data")
    loaded = reopened.get_task(record.task_id)
    assert loaded is not None
    assert loaded.issue_id == "259"
    assert loaded.input_sha256 == record.input_sha256


def test_import_existing_output_does_not_write_to_source(tmp_path):
    output = tmp_path / "legacy"
    output.mkdir()
    (output / "image_index.json").write_text(json.dumps({"schema_version": 1, "issue_id": "1", "news_items": [], "candidates": []}), encoding="utf-8")
    before = sorted(path.relative_to(output).as_posix() for path in output.rglob("*"))

    store = TaskStore(tmp_path / "app-data")
    record = store.import_output(output)

    assert record.imported is True
    assert Path(record.source_output).resolve() == output.resolve()
    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*")) == before
    assert Path(record.state_path).parent != output


def test_remove_from_history_keeps_task_files(tmp_path):
    source = tmp_path / "259.docx"
    source.write_bytes(b"docx fixture")
    task_root = tmp_path / "task-259"
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("259", source, task_root=task_root)

    assert store.remove_from_history(record.task_id) is True
    assert store.get_task(record.task_id) is None
    assert task_root.is_dir()
    assert (task_root / "task_state.sqlite3").is_file()


def test_retry_attempt_is_read_as_data_and_keeps_existing_candidates(tmp_path):
    source = tmp_path / "259.docx"
    source.write_bytes(b"docx fixture")
    task_root = tmp_path / "task-259"
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("259", source, task_root=task_root)
    retry = task_root / "retries" / "n1" / "attempt-1"
    retry.mkdir(parents=True)
    (retry / "image_index.json").write_text(
        json.dumps({"candidates": [{"id": "old"}, {"id": "new"}]}), encoding="utf-8"
    )
    (retry / "video_index.json").write_text(json.dumps({"videos": [{"id": "v1"}]}), encoding="utf-8")

    merged = store.merge_retry_attempt(record.task_id, "n1", "attempt-1")

    assert [item["id"] for item in merged.images] == ["old", "new"]
    assert [item["id"] for item in merged.videos] == ["v1"]
    assert (task_root / "retries" / "n1" / "attempt-1" / "image_index.json").is_file()


def test_retry_attempt_repairs_legacy_state_schema_and_list_is_stable(tmp_path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"fixture")
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("1", source, task_root=tmp_path / "task")
    state = Path(record.state_path)
    import sqlite3
    with sqlite3.connect(state) as connection:
        connection.execute("DROP TABLE retry_attempts")
        connection.commit()
    attempt = Path(record.task_root) / "retries" / "n1" / "a1"
    attempt.mkdir(parents=True)
    (attempt / "image_index.json").write_text(json.dumps({"kind": "supplement", "candidates": [], "failures": [{"id": "broken"}]}), encoding="utf-8")

    loaded = store.merge_retry_attempt(record, "n1", "a1")
    before = store.list_retry_attempts(record)
    reread = store.list_retry_attempts(record)

    assert loaded.kind == "supplement"
    assert len(before) == len(reread) == 1
    assert before[0].failures == [{"id": "broken"}]
    with sqlite3.connect(state) as connection:
        created_1 = connection.execute("SELECT created_at FROM retry_attempts").fetchone()[0]
    store.list_retry_attempts(record)
    with sqlite3.connect(state) as connection:
        created_2 = connection.execute("SELECT created_at FROM retry_attempts").fetchone()[0]
    assert created_1 == created_2


def test_list_retry_attempts_recovers_confined_orphan_supplement_manifest(tmp_path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"fixture")
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("1", source, task_root=tmp_path / "task")
    attempt = Path(record.task_root) / "retries" / "n1" / "batch-1"
    attempt.mkdir(parents=True)
    (attempt / "image_index.json").write_text(json.dumps({
        "kind": "supplement",
        "news_items": [{"news_id": "n1"}],
        "candidates": [{"id": "img-1", "news_id": "n1", "image_url": "https://example.test/1"}],
    }), encoding="utf-8")
    # A malformed neighboring manifest must not be registered.
    invalid = Path(record.task_root) / "retries" / "n2" / "batch-2"
    invalid.mkdir(parents=True)
    (invalid / "image_index.json").write_text(json.dumps({
        "kind": "supplement", "news_items": [{"news_id": "n2"}],
        "candidates": [{"id": "cross", "news_id": "n1"}],
    }), encoding="utf-8")

    attempts = store.list_retry_attempts(record)

    assert [(item.news_id, item.attempt_id, item.kind) for item in attempts] == [("n1", "batch-1", "supplement")]
    assert attempts[0].images[0]["id"] == "img-1"


@pytest.mark.parametrize("task_root_name", ["legacy", "legacy/managed"])
def test_import_rejects_task_root_inside_legacy_source_without_writing(tmp_path, task_root_name):
    output = tmp_path / "legacy"
    output.mkdir()
    index = output / "image_index.json"
    index.write_bytes(json.dumps({"schema_version": 1, "issue_id": "1", "news_items": [], "candidates": []}).encode())
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    store = TaskStore(tmp_path / "app-data")

    with pytest.raises(ValueError, match="task_root"):
        store.import_output(output, task_root=tmp_path / task_root_name)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
