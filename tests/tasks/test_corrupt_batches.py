from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path

from galgame_news.diagnostics import diagnose_error, format_error
from galgame_news.tasks.store import TaskStore


def _batch(root: Path, news_id: str, attempt_id: str, payload: object) -> Path:
    attempt = root / "retries" / news_id / attempt_id
    attempt.mkdir(parents=True)
    (attempt / "image_index.json").write_text(json.dumps(payload), encoding="utf-8")
    return attempt


def test_listing_skips_corrupt_batch_and_keeps_valid_one(tmp_path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"fixture")
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("1", source, task_root=tmp_path / "task")
    good = _batch(record.root_path, "n1", "good", {"schema_version": 1, "candidates": [{"id": "ok", "news_id": "n1"}]})
    bad = _batch(record.root_path, "n2", "bad", {"schema_version": 1, "candidates": "not-a-list"})
    store.register_retry_attempt(record, "n1", "good", path=good)
    store.register_retry_attempt(record, "n2", "bad", path=bad)

    attempts = store.list_retry_attempts(record)

    assert [(item.news_id, item.attempt_id) for item in attempts] == [("n1", "good")]
    assert len(store.retry_diagnostics) == 1
    assert store.retry_diagnostics[0]["code"] == "error"
    assert bad.is_dir()  # damaged data is retained for inspection


def test_lightweight_registration_confines_path_and_is_idempotent(tmp_path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"fixture")
    store = TaskStore(tmp_path / "app-data")
    record = store.create_task("1", source, task_root=tmp_path / "task")
    attempt = record.root_path / "retries" / "n1" / "a1"
    attempt.mkdir(parents=True)

    store.register_retry_attempt(record, "n1", "a1", "supplement", attempt)
    store.register_retry_attempt(record, "n1", "a1", "supplement", attempt)
    with sqlite3.connect(record.state_path) as connection:
        rows = connection.execute("SELECT news_id, attempt_id, kind FROM retry_attempts").fetchall()
    assert rows == [("n1", "a1", "supplement")]

    try:
        store.register_retry_attempt(record, "../outside", "a2")
    except ValueError:
        pass
    else:
        raise AssertionError("path component should have been rejected")


def test_diagnostics_classify_http_and_redact_secrets():
    error = urllib.error.HTTPError(
        "https://example.test/?api_key=secret&token=hidden", 429, "slow", {}, None
    )
    diagnostic = diagnose_error(error, stage="download", source="provider")
    assert diagnostic["code"] == "http_429"
    assert diagnostic["retryable"] is True
    assert diagnostic["http_status"] == 429
    assert "secret" not in str(diagnostic)
    assert "hidden" not in str(diagnostic)
    assert format_error(ValueError("Authorization: Bearer private key=topsecret"), "parse")


def test_orphan_retry_recovers_valid_rows_without_corrupt_sibling(tmp_path):
    source = tmp_path / "input.docx"
    source.write_bytes(b"fixture")
    store = TaskStore(tmp_path / "app-data")
    try:
        record = store.create_task("1", source, task_root=tmp_path / "task")
        _batch(record.root_path, "n1", "cancelled", {"kind": "retry", "status": "cancelled",
               "news_items": [{"news_id": "n1"}],
               "candidates": [{"id": "ok", "news_id": "n1"}, "broken", {"id": "foreign", "news_id": "n2"}]})
        recovered = store.list_retry_attempts(record)
        assert [attempt.attempt_id for attempt in recovered] == ["cancelled"]
        assert [row["id"] for row in recovered[0].images] == ["ok"]
        assert len(store.retry_diagnostics) == 2
        assert len(store.list_retry_attempts(record)) == 1
    finally:
        store.close()

