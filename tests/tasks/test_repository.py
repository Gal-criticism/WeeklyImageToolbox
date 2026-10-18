import sqlite3
import hashlib
import time
from datetime import datetime, timezone

import pytest

from galgame_news.tasks.repository import TaskRepository
from galgame_news.pipeline.contracts import Checkpoint
from galgame_news.domain import NewsItem


def test_authority_check_keeps_transient_lock_recoverable(tmp_path):
    repository = TaskRepository(tmp_path)
    repository.activate()
    blocker = sqlite3.connect(repository.path)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            TaskRepository.is_authoritative(tmp_path)
    finally:
        blocker.rollback()
        blocker.close()
    assert TaskRepository.is_authoritative(tmp_path)


def test_authority_only_after_activation(tmp_path):
    repo = TaskRepository(tmp_path)
    assert not TaskRepository.is_authoritative(tmp_path)
    repo.activate()
    assert TaskRepository.is_authoritative(tmp_path)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } >= {"task_meta", "retry_attempts", "review_decisions"}


def test_refuses_newer_schema_without_mutating_it(tmp_path):
    path = tmp_path / "task_state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE task_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO task_meta VALUES('task_schema_version','99')")
    with pytest.raises(ValueError, match="newer"):
        TaskRepository(tmp_path)
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT value FROM task_meta WHERE key='task_schema_version'"
            ).fetchone()[0]
            == "99"
        )
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='candidates'"
            ).fetchone()
            is None
        )


def test_candidates_checkpoint_paths_and_review_state(tmp_path):
    repo = TaskRepository(tmp_path)
    (tmp_path / "raw").mkdir()
    image = tmp_path / "raw" / "a.png"
    image.write_bytes(b"image")
    repo.upsert_candidates(
        [{"id": "a", "news_id": "n1", "local_path": str(image), "section": "x"}]
    )
    assert repo.query_candidates("n1")[0]["local_path"] == str(image)
    repo.save_checkpoint(
        {
            "status": "running",
            "issue": {},
            "candidates": [{"id": "a", "news_id": "n1", "local_path": str(image)}],
            "videos": [],
        }
    )
    loaded = repo.load_checkpoint()
    assert loaded["candidates"][0]["local_path"] == str(image)
    with sqlite3.connect(repo.path) as db:
        asset = db.execute(
            "SELECT a.id,a.payload FROM candidate_assets c JOIN assets a ON a.id=c.asset_id WHERE c.candidate_id='a' AND c.role='local'"
        ).fetchone()
    assert asset is not None and asset[0] == hashlib.sha256(b"image").hexdigest()
    repo.save_review_state({"decisions": {"a": "accepted"}})
    assert repo.load_review_state() == {
        "decisions": {"a": "accepted"},
        "manual_decisions": [],
        "revision": 0,
    }


def test_rejects_external_asset_and_secret_options(tmp_path):
    repo = TaskRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.upsert_candidates(
            [{"id": "bad", "local_path": str(tmp_path.parent / "secret.png")}]
        )
    repo.save_options({"offline": True, "api_key": "secret"})
    assert repo.load_options() == {"offline": True}


def test_checkpoint_restores_news_sources_result_and_order(tmp_path):
    repo = TaskRepository(tmp_path)
    candidate_a = {"id": "z", "news_id": "n", "title": "first"}
    candidate_b = {"id": "a", "news_id": "n", "title": "second"}
    payload = {
        "schema_version": 1,
        "status": "completed",
        "task_id": "t",
        "issue_id": "i",
        "input_sha256": "h",
        "config_sha256": "c",
        "issue": {"issue_id": "i", "news_items": [{"id": "n", "title": "news"}]},
        "completed_news_ids": ["n"],
        "retryable_candidate_ids": [],
        "candidates": [candidate_a, candidate_b],
        "videos": [],
        "failures": [],
        "source_map": {"n": [{"id": "s"}]},
        "result": {
            "issue": {"issue_id": "i", "news_items": [{"id": "n", "title": "news"}]},
            "candidates": [candidate_a, candidate_b],
            "filtered_candidates": [],
            "review_required": [],
            "videos": [],
            "failures": [],
        },
    }
    repo.save_checkpoint(payload)
    restored = repo.load_checkpoint()
    assert [c["id"] for c in restored["candidates"]] == ["z", "a"]
    assert restored["issue"]["news_items"] == payload["issue"]["news_items"]
    assert restored["source_map"] == payload["source_map"]
    assert [c["id"] for c in restored["result"]["candidates"]] == ["z", "a"]


def test_checkpoint_roundtrips_overlapping_result_groups(tmp_path):
    repo = TaskRepository(tmp_path)
    shared = {"id": "same", "news_id": "n", "title": "candidate"}
    payload = {
        "issue": {"news_items": [{"id": "n"}]},
        "candidates": [shared],
        "videos": [],
        "failures": [],
        "source_map": {"n": [{"id": "source"}]},
        "result": {
            "issue": {"news_items": [{"id": "n"}]},
            "candidates": [shared],
            "filtered_candidates": [shared],
            "review_required": [shared],
            "videos": [],
            "failures": [],
        },
    }
    repo.save_checkpoint(payload)
    restored = repo.load_checkpoint()
    assert restored["result"]["candidates"] == [shared]
    assert restored["result"]["filtered_candidates"] == [shared]
    assert restored["result"]["review_required"] == [shared]
    assert restored["source_map"] == payload["source_map"]


def test_save_index_preserves_checkpoint_identity_and_exports_without_checkpoint(
    tmp_path,
):
    repo = TaskRepository(tmp_path)
    candidate = {"id": "img", "news_id": "n", "local_path": "raw/img.jpg"}
    payload = {
        "schema_version": 1,
        "status": "cancelled",
        "task_id": "task",
        "issue_id": "issue",
        "input_sha256": "input",
        "config_sha256": "config",
        "media_cache_version": 1,
        "issue": {"news_items": [{"id": "n", "title": "News", "sequence": 1}]},
        "candidates": [candidate],
        "videos": [],
        "failures": [],
        "result": {
            "issue": {"news_items": [{"id": "n", "title": "News", "sequence": 1}]},
            "candidates": [candidate],
            "filtered_candidates": [candidate],
            "review_required": [],
            "videos": [],
            "failures": [],
        },
    }
    repo.save_checkpoint(payload)
    repo.save_index(
        {
            "issue_id": "issue",
            "news_items": [
                {"news_id": "n", "title": "News", "sequence": 1, "section": "x"}
            ],
            "candidates": [{"id": "img", "news_id": "n", "selected": True}],
        },
        {"issue_id": "issue", "news_items": [], "videos": []},
        {"issue_id": "issue", "candidates": ["img"]},
    )
    restored = repo.load_checkpoint()
    assert (
        restored["status"],
        restored["task_id"],
        restored["input_sha256"],
        restored["media_cache_version"],
    ) == ("cancelled", "task", "input", 1)
    assert restored["candidates"][0]["local_path"] == str(tmp_path / "raw" / "img.jpg")
    assert restored["result"]["filtered_candidates"] == restored["candidates"]
    image, video, required, _ = repo.export_review_payloads()
    assert image["issue_id"] == "issue" and image["news_items"][0]["title"] == "News"
    assert [row["id"] for row in required] == ["img"]

    empty = TaskRepository(tmp_path / "index-only")
    empty.save_index(
        {
            "issue_id": "i",
            "news_items": [{"news_id": "n", "title": "N"}],
            "candidates": [],
        },
        {"issue_id": "i", "news_items": [], "videos": []},
        [],
    )
    assert empty.load_checkpoint() is None
    assert empty.export_review_payloads()[0]["news_items"][0]["title"] == "N"


def test_real_checkpoint_roundtrip_with_index_keeps_domain_news_and_section(tmp_path):
    repo = TaskRepository(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    news = NewsItem.model_validate(
        {
            "issue_id": "issue-real",
            "sequence": 1,
            "section": "汉化",
            "title": "发布",
            "body": "完整正文",
            "source_urls": ["https://example.test/news"],
            "author": "作者",
        }
    )
    checkpoint = Checkpoint.model_validate(
        {
            "status": "cancelled",
            "task_id": "task-real",
            "issue_id": "issue-real",
            "input_sha256": "input-real",
            "config_sha256": "config-real",
            "issue": {
                "issue_id": "issue-real",
                "input_path": "input.md",
                "news_items": [news.model_dump(mode="json")],
            },
            "candidates": [
                {
                    "news_id": news.id,
                    "image_url": "https://example.test/image.jpg",
                    "source_url": "https://example.test/news",
                    "fetched_at": now,
                }
            ],
        }
    )
    payload = checkpoint.model_dump(mode="json")
    repo.save_checkpoint(payload)
    repo.save_index(
        {
            "issue_id": "issue-real",
            "news_items": [
                {
                    "news_id": news.id,
                    "title": news.title,
                    "section": news.section,
                    "sequence": news.sequence,
                }
            ],
            "candidates": [{**payload["candidates"][0], "section": news.section}],
        },
        {"issue_id": "issue-real", "news_items": [], "videos": []},
        [],
    )
    restored = Checkpoint.model_validate(repo.load_checkpoint())
    assert restored.issue.news_items[0].body == "完整正文"
    assert restored.issue.news_items[0].source_urls == ["https://example.test/news"]
    assert (
        repo.query_candidates(news_id=news.id, section="汉化")[0]["id"]
        == payload["candidates"][0]["id"]
    )


def test_journal_identity_roundtrip_and_isolation(tmp_path):
    repo = TaskRepository(tmp_path)
    repo.save_journal("n1", [{"id": "one", "news_id": "n1"}], {"task": "t"})
    repo.save_journal("n2", [{"id": "two", "news_id": "n2"}], {"task": "other"})
    rows, collected = repo.load_journal({"task": "t"})
    assert [row["id"] for row in rows] == ["one"]
    assert collected == {"n1"}


def test_batch_paths_are_task_relative_and_journal_excludes_videos(tmp_path):
    repo = TaskRepository(tmp_path)
    retry = tmp_path / "retries" / "n" / "attempt-1"
    retry.mkdir(parents=True)
    repo.save_batch("n", "attempt-1", str(retry), "retry", "completed", [], [], [])
    assert repo.read_batch("n", "attempt-1")["path"] == str(retry.resolve())
    repo.save_journal(
        "n",
        [
            {"id": "im", "news_id": "n"},
            {"id": "vid", "news_id": "n", "video_url": "https://example.test/v.mp4"},
        ],
        "identity",
    )
    rows, _ = repo.load_journal("identity")
    assert [row["id"] for row in rows] == ["im"]


def test_review_revision_journal_posts_and_backup(tmp_path):
    repo = TaskRepository(tmp_path)
    repo.save_review_state(
        {
            "revision": 3,
            "decisions": {"manual": {"decision": "accepted", "manual": True}},
        }
    )
    with pytest.raises(ValueError):
        repo.save_review_state({"revision": 2, "decisions": {"manual": "rejected"}})
    assert repo.load_review_state()["revision"] == 3
    with pytest.raises(ValueError):
        repo.save_review_state({"revision": 3, "decisions": {"manual": "rejected"}})
    repo.save_journal("n", [{"id": "candidate", "news_id": "n"}], {"task": "t"})
    rows, collected = repo.load_journal({"task": "t"})
    assert [row["id"] for row in rows] == ["candidate"]
    assert collected == {"n"}
    repo.save_post("p", "seen", {"text": "ok"}, {"task": "t"})
    assert repo.load_posts({"task": "t"})["p"]["state"] == "seen"
    backup = repo.backup(tmp_path.parent / "repo-backup")
    assert (backup / "task_state.sqlite3").is_file()


def test_asset_intent_recovers_landed_original_and_keeps_missing_intent(tmp_path):
    import hashlib
    from PIL import Image

    repo = TaskRepository(tmp_path)
    candidate = {
        "id": "recover",
        "news_id": "n",
        "download_status": "retryable_failed",
        "signals": {"invalid_reason": "previous failure"},
    }
    repo.upsert_candidates([candidate])
    asset = tmp_path / "assets" / "originals" / "recover.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "red").save(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    repo.prepare_asset(
        "recover",
        {
            "original_path": str(asset),
            "original_sha256": digest,
            "original_mime_type": "image/png",
            "original_width": 2,
            "original_height": 2,
            "original_byte_size": asset.stat().st_size,
        },
    )
    repo.prepare_asset(
        "missing",
        {"original_path": str(tmp_path / "future.png"), "original_sha256": "a" * 64},
    )
    assert repo.recover_asset_intents() == 1
    rows = repo.query_candidates()
    recovered = next(row for row in rows if row["id"] == "recover")
    assert recovered["download_status"] == "downloaded"
    assert recovered["original_path"] == str(asset)
    assert "invalid_reason" not in recovered["signals"]
    with sqlite3.connect(repo.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM asset_intents WHERE candidate_id='missing'"
            ).fetchone()[0]
            == 1
        )


def test_backup_requires_empty_destination_and_journal_normalizes_identity(tmp_path):
    repo = TaskRepository(tmp_path / "task")
    repo.save_journal(
        "n", [{"id": "c", "news_id": "n"}], {"input_sha256": "a", "config_sha256": "b"}
    )
    rows, collected = repo.load_journal({"input_sha256": "a", "config_sha256": "b"})
    assert {row["id"] for row in rows} == {"c"}
    assert collected == {"n"}
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "stale").write_text("leave untouched")
    with pytest.raises(FileExistsError):
        repo.backup(destination)
    assert (destination / "stale").read_text() == "leave untouched"


def test_backup_preserves_referenced_work_assets_and_rejects_escape(tmp_path):
    root = tmp_path / "task"
    repo = TaskRepository(root)
    work = root / ".work" / "videos" / "done.mp4"
    work.parent.mkdir(parents=True)
    work.write_bytes(b"completed video")
    unused = root / ".work" / "temp.part"
    unused.write_bytes(b"unfinished")
    repo.upsert_candidates(
        [
            {
                "id": "video",
                "news_id": "n",
                "video_url": "https://example.invalid/v",
                "local_path": str(work),
            }
        ],
        kind="video",
    )
    backup = repo.backup(tmp_path / "backup")
    assert (backup / ".work" / "videos" / "done.mp4").read_bytes() == b"completed video"
    assert not (backup / ".work" / "temp.part").exists()

    with sqlite3.connect(repo.path) as db:
        db.execute(
            "UPDATE candidates SET payload=? WHERE id='video'",
            ('{"id":"video","local_path":"../outside.mp4"}',),
        )
    with pytest.raises(ValueError, match="escapes"):
        repo.backup(tmp_path / "escaped-backup")


def test_create_false_rejects_future_schema_without_mutation(tmp_path):
    path = tmp_path / "task_state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE task_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO task_meta VALUES('task_schema_version','99')")
    with pytest.raises(ValueError, match="newer"):
        TaskRepository(tmp_path, create=False)
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='candidates'"
            ).fetchone()
            is None
        )


def test_authority_uri_escapes_special_path_and_candidate_query_stays_bounded(tmp_path):
    root = tmp_path / "task #1 中文"
    repo = TaskRepository(root)
    repo.activate()
    assert TaskRepository.is_authoritative(root)
    image_index = {
        "issue_id": "1",
        "news_items": [{"news_id": "n", "sequence": 1, "section": "x", "title": "t"}],
        "candidates": [
            {"id": f"candidate-{i:040x}", "news_id": "n"} for i in range(120)
        ],
        "failures": [],
    }
    repo.save_index(image_index, {})
    rows = repo.query_candidates(section="x", limit=10000)
    assert len(rows) == 100
    assert all("section" not in row for row in rows)


def test_rejects_synthetic_socialdata_key_without_echoing_value(tmp_path):
    repo = TaskRepository(tmp_path)
    synthetic = "123456|" + "A9" * 24
    with pytest.raises(ValueError) as exc:
        repo.set_meta("synthetic", synthetic)
    assert synthetic not in str(exc.value)
    long_id = "a" * 64
    repo.set_meta(
        "_membership",
        {"candidates": [long_id], "filtered_candidates": [str(int("9" * 48))]},
    )
    assert repo.get_meta("_membership") == {
        "candidates": [long_id],
        "filtered_candidates": [str(int("9" * 48))],
    }


def test_asset_recovery_and_integrity_force_full_image_decode(tmp_path):
    import hashlib
    from io import BytesIO
    from PIL import Image

    repo = TaskRepository(tmp_path)
    stream = BytesIO()
    Image.new("RGB", (16, 16), "green").save(stream, format="JPEG")
    truncated = stream.getvalue()[:-40]
    asset = tmp_path / "assets" / "originals" / "truncated.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(truncated)
    repo.upsert_candidates(
        [
            {
                "id": "truncated",
                "news_id": "n",
                "download_status": "pending",
                "original_path": str(asset),
                "original_sha256": hashlib.sha256(truncated).hexdigest(),
            }
        ]
    )
    repo.prepare_asset(
        "truncated",
        {
            "original_path": str(asset),
            "original_sha256": hashlib.sha256(truncated).hexdigest(),
        },
    )
    assert repo.recover_asset_intents() == 0
    report = repo.integrity_check()
    assert report["assets"][0]["decodable"] is False
    with sqlite3.connect(repo.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM asset_intents WHERE candidate_id='truncated'"
            ).fetchone()[0]
            == 1
        )


def test_save_batch_rejects_cross_news_candidate_atomically(tmp_path):
    repo = TaskRepository(tmp_path)
    with pytest.raises(ValueError, match="news id mismatch"):
        repo.save_batch(
            "expected",
            "attempt",
            "retries/attempt",
            "retry",
            "completed",
            [{"id": "candidate", "news_id": "other"}],
            [],
            [],
        )
    assert repo.read_batch("expected", "attempt") is None
    assert repo.query_candidates(news_id="other") == []


def test_writer_lock_times_out_then_preserves_and_saves_review_state(tmp_path):
    repo = TaskRepository(tmp_path)
    repo.save_review_state({"revision": 1, "decisions": {"m": "accepted"}})
    external = sqlite3.connect(repo.path, timeout=0)
    try:
        external.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            repo.save_review_state({"revision": 2, "decisions": {"m": "rejected"}})
        elapsed = time.monotonic() - started
        assert 4.5 <= elapsed < 8
    finally:
        external.rollback()
        external.close()
    assert repo.load_review_state()["decisions"] == {"m": "accepted"}
    repo.save_review_state({"revision": 2, "decisions": {"m": "rejected"}})
    assert repo.load_review_state()["decisions"] == {"m": "rejected"}


def test_candidate_upsert_rolls_back_the_entire_batch_on_invalid_second_row(tmp_path):
    repo = TaskRepository(tmp_path)
    outside = tmp_path.parent / "external.png"
    with pytest.raises(ValueError, match="escapes"):
        repo.upsert_candidates(
            [
                {"id": "first", "news_id": "n"},
                {"id": "invalid", "news_id": "n", "local_path": str(outside)},
            ]
        )
    assert repo.query_candidates(news_id="n") == []


def test_failed_asset_landing_keeps_existing_asset_and_download_state(tmp_path):
    repo = TaskRepository(tmp_path)
    old_asset = tmp_path / "assets" / "old.png"
    old_asset.parent.mkdir(parents=True)
    old_asset.write_bytes(b"old verified asset")
    repo.upsert_candidates(
        [
            {
                "id": "candidate",
                "news_id": "n",
                "download_status": "downloaded",
                "local_path": str(old_asset),
                "original_path": str(old_asset),
                "signals": {},
            }
        ]
    )
    missing_new_asset = tmp_path / "assets" / "new.png"
    repo.prepare_asset(
        "candidate",
        {"original_path": str(missing_new_asset), "original_sha256": "a" * 64},
    )
    assert repo.recover_asset_intents() == 0
    candidate = repo.query_candidates(news_id="n")[0]
    assert candidate["download_status"] == "downloaded"
    assert candidate["local_path"] == str(old_asset)
    assert candidate["original_path"] == str(old_asset)
    assert not missing_new_asset.exists()


def test_reopening_after_uncommitted_connection_disconnect_rolls_back(tmp_path):
    repo = TaskRepository(tmp_path)
    repo.set_meta("stable", {"value": "committed"})
    external = sqlite3.connect(repo.path, timeout=1)
    external.execute("BEGIN IMMEDIATE")
    external.execute(
        "INSERT INTO candidates(id,news_id,section,kind,payload,ordinal) "
        "VALUES('uncommitted','n',NULL,'image','{}',0)"
    )
    external.close()
    reopened = TaskRepository(tmp_path, create=False)
    assert reopened.get_meta("stable") == {"value": "committed"}
    assert reopened.query_candidates(news_id="n") == []
