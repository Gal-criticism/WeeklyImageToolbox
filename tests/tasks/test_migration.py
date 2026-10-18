import json
import hashlib
from io import BytesIO
from datetime import datetime, timezone

import pytest
from PIL import Image

from galgame_news.tasks.migration import migrate_task
from galgame_news.tasks.repository import TaskRepository
from galgame_news.domain import ImageCandidate, Issue, NewsItem
from galgame_news.pipeline.contracts import Checkpoint


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_migration_activates_after_copy_and_is_idempotent(tmp_path):
    root = tmp_path / "task"
    candidate = {
        "id": "im1",
        "news_id": "n1",
        "image_url": "https://example.test/a.jpg",
        "local_path": "n1/a.jpg",
        "download_status": "downloaded",
    }
    (root / "raw" / "n1").mkdir(parents=True)
    (root / "raw" / "n1" / "a.jpg").write_bytes(b"asset")
    _write(
        root / "raw" / "image_index.json",
        {"candidates": [candidate], "news_items": [{"news_id": "n1"}]},
    )
    _write(root / "raw" / "review_required.json", [candidate])
    state = {
        "schema_version": 1,
        "revision": 7,
        "decisions": {"im1": "accepted"},
        "manual_decisions": ["im1"],
    }
    _write(root / "review_state.json", state)
    _write(root / "options.json", {"offline": True, "ignored": "legacy"})
    _write(root / "request_options.json", {"max_images": 4, "offline": False})
    final_manifest = root / "final" / "manifest.json"
    _write(final_manifest, {"images": ["keep.jpg"]})
    final_bytes = final_manifest.read_bytes()
    (root / "final" / "keep.jpg").write_bytes(b"do not touch")
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")
    }

    report = migrate_task(root)
    assert report["migrated"] is True
    assert TaskRepository.is_authoritative(root)
    repository = TaskRepository(root, create=False)
    assert repository.query_candidates(kind="image")[0]["id"] == "im1"
    assert repository.load_review_state() == state
    assert repository.load_options() == {"offline": False, "max_images": 4}
    assert {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")
    } == before
    assert final_manifest.read_bytes() == final_bytes
    assert (root / "final" / "keep.jpg").read_bytes() == b"do not touch"
    again = migrate_task(root)
    assert again["already_authoritative"] is True


def test_review_state_override_takes_precedence_and_is_repeatable(tmp_path):
    root = tmp_path / "review-state-override"
    legacy = {
        "schema_version": 1,
        "revision": 2,
        "decisions": {"media-1": "rejected"},
        "manual_decisions": ["media-1"],
    }
    override = {
        "schema_version": 1,
        "revision": 9,
        "decisions": {"media-1": "accepted"},
        "manual_decisions": ["media-1"],
    }
    _write(root / "review_state.json", legacy)

    first = migrate_task(root, review_state_override=override)
    repository = TaskRepository(root, create=False)
    assert first["migrated"]
    assert repository.load_review_state() == override
    assert json.loads((root / "review_state.json").read_text(encoding="utf-8")) == legacy

    # Once the DB is authoritative, a later import stub cannot replace its state.
    replacement = {**override, "revision": 10, "decisions": {"media-1": "rejected"}}
    second = migrate_task(root, review_state_override=replacement)
    assert second["already_authoritative"]
    assert repository.load_review_state() == override


def test_invalid_review_state_override_fails_before_database_creation(tmp_path):
    root = tmp_path / "invalid-review-state-override"
    legacy = {"schema_version": 1, "revision": 3, "decisions": {"m": "pending"}}
    _write(root / "review_state.json", legacy)

    with pytest.raises(ValueError, match="review_state_override"):
        migrate_task(
            root,
            review_state_override={
                "revision": -1,
                "decisions": {"m": "not-a-decision"},
                "manual_decisions": [],
            },
        )

    assert not (root / "task_state.sqlite3").exists()
    assert json.loads((root / "review_state.json").read_text(encoding="utf-8")) == legacy


def test_private_retry_import_copies_assets_and_survives_task_move(tmp_path):
    from galgame_news.review.session import ReviewSession

    source_root = tmp_path / "source-task"
    private_root = tmp_path / "private-task"
    _write(
        source_root / "raw" / "image_index.json",
        {"news_items": [{"news_id": "n1", "title": "News", "section": "Games"}], "candidates": []},
    )
    batch = private_root / "retries" / "n1" / "attempt-1"
    original = batch / "images" / "original.png"
    review = batch / "review_assets" / "review.png"
    original.parent.mkdir(parents=True)
    review.parent.mkdir(parents=True)
    original_bytes = BytesIO()
    Image.new("RGB", (8, 6), "red").save(original_bytes, format="PNG")
    original.write_bytes(original_bytes.getvalue())
    review_bytes = BytesIO()
    Image.new("RGB", (4, 3), "blue").save(review_bytes, format="PNG")
    review.write_bytes(review_bytes.getvalue())
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    review_hash = hashlib.sha256(review.read_bytes()).hexdigest()
    candidate = {
        "id": "private-image",
        "news_id": "n1",
        "image_url": "https://example.test/private.png",
        "source_url": "https://example.test/page",
        "local_path": str(review.relative_to(private_root)),
        "original_path": str(original.relative_to(private_root)),
        "download_status": "downloaded",
        "sha256": original_hash,
        "original_sha256": original_hash,
        "output_sha256": review_hash,
        "original_byte_size": original.stat().st_size,
        "output_byte_size": review.stat().st_size,
    }
    index = {
        "kind": "supplement",
        "status": "completed",
        "source_url": "https://example.test/page",
        "sources": [],
        "candidates": [candidate],
    }
    index_path = batch / "image_index.json"
    _write(index_path, index)
    _write(
        batch / "failed_items.json",
        [{"news_id": "n1", "stage": "download", "code": "timeout", "message": "retry"}],
    )
    private_options = {"offline": True, "max_images": 2}
    _write(private_root / "request_options.json", private_options)
    original_index_bytes = index_path.read_bytes()
    original_options_bytes = (private_root / "request_options.json").read_bytes()
    override = {
        "schema_version": 1,
        "revision": 6,
        "decisions": {"private-image": "accepted"},
        "manual_decisions": ["private-image"],
    }

    report = migrate_task(
        source_root,
        private_task_root=private_root,
        review_state_override=override,
    )
    repository = TaskRepository(source_root, create=False)
    imported = repository.read_batch("n1", "attempt-1")
    assert report["counts"]["private_retry_batches"] == 1
    assert imported["images"][0]["local_path"].startswith(str(source_root))
    assert imported["images"][0]["original_path"].startswith(str(source_root))
    assert len(imported["failures"]) == 1
    assert repository.load_options() == private_options
    assert repository.load_review_state()["revision"] == 6
    assert repository.load_review_state()["manual_decisions"] == ["private-image"]
    assert index_path.read_bytes() == original_index_bytes
    assert (private_root / "request_options.json").read_bytes() == original_options_bytes

    moved_root = tmp_path / "moved-source-task"
    source_root.rename(moved_root)
    session = ReviewSession.from_output(moved_root / "raw", task_root=moved_root)
    loaded = next(item for item in session.images if str(item.id) == "private-image")
    assert session.decision("private-image").value == "accepted"
    assert session._source_paths["private-image"].is_file()
    assert str(session._source_paths["private-image"]).startswith(str(moved_root))
    assert loaded.original_path.startswith(str(moved_root))


def test_private_retry_options_only_fill_missing_source_options(tmp_path):
    source_root = tmp_path / "source-with-options"
    private_root = tmp_path / "private-with-options"
    _write(
        source_root / "raw" / "image_index.json",
        {"news_items": [{"news_id": "n1", "title": "News", "section": "Games"}], "candidates": []},
    )
    _write(source_root / "request_options.json", {"offline": False, "max_images": 4})
    _write(private_root / "request_options.json", {"offline": True, "max_images": 1})

    migrate_task(source_root, private_task_root=private_root)

    assert TaskRepository(source_root, create=False).load_options() == {
        "offline": False,
        "max_images": 4,
    }


def test_private_retry_asset_escape_isolated_and_preserves_candidate(tmp_path):
    source_root = tmp_path / "source-escape"
    private_root = tmp_path / "private-escape"
    _write(
        source_root / "raw" / "image_index.json",
        {"news_items": [{"news_id": "n1", "title": "News", "section": "Games"}], "candidates": []},
    )
    outside = tmp_path / "outside.png"
    output = BytesIO()
    Image.new("RGB", (3, 2), "green").save(output, format="PNG")
    outside.write_bytes(output.getvalue())
    batch = private_root / "retries" / "n1" / "attempt-escape"
    candidate = {
        "id": "escaped-image",
        "news_id": "n1",
        "image_url": "https://example.test/escaped.png",
        "source_url": "https://example.test/page",
        "local_path": str(outside),
        "download_status": "downloaded",
        "output_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    _write(batch / "image_index.json", {"candidates": [candidate], "failures": []})

    report = migrate_task(source_root, private_task_root=private_root)

    migrated = TaskRepository(source_root, create=False).read_batch("n1", "attempt-escape")
    assert len(migrated["images"]) == 1
    assert migrated["images"][0]["image_url"] == candidate["image_url"]
    assert migrated["images"][0]["local_path"] is None
    assert migrated["images"][0]["download_status"] == "retryable_failed"
    assert any("out-of-scope" in item["error"] for item in report["diagnostics"])


def test_private_retry_invalid_kind_isolated(tmp_path):
    source_root = tmp_path / "source-invalid-kind"
    private_root = tmp_path / "private-invalid-kind"
    _write(
        source_root / "raw" / "image_index.json",
        {"news_items": [{"news_id": "n1", "title": "News", "section": "Games"}], "candidates": []},
    )
    _write(
        private_root / "retries" / "n1" / "attempt-bad-kind" / "image_index.json",
        {"kind": "other", "status": "completed", "candidates": [], "failures": []},
    )

    report = migrate_task(source_root, private_task_root=private_root)

    assert TaskRepository(source_root, create=False).read_batch("n1", "attempt-bad-kind") is None
    assert any("invalid retry batch kind" in item["error"] for item in report["diagnostics"])


def test_private_retries_symlink_escape_rejected_before_read(tmp_path):
    source_root = tmp_path / "source-symlink"
    private_root = tmp_path / "private-symlink"
    outside = tmp_path / "outside-retries"
    _write(
        source_root / "raw" / "image_index.json",
        {"news_items": [{"news_id": "n1", "title": "News", "section": "Games"}], "candidates": []},
    )
    _write(outside / "n1" / "attempt-1" / "image_index.json", {"kind": "retry", "candidates": []})
    private_root.mkdir(parents=True)
    try:
        (private_root / "retries").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    report = migrate_task(source_root, private_task_root=private_root)

    assert TaskRepository(source_root, create=False).list_batches() == []
    assert any("retries root escapes" in item["error"] for item in report["diagnostics"])


def test_malformed_critical_json_does_not_create_database_or_touch_source(tmp_path):
    root = tmp_path / "task"
    path = root / "raw" / "image_index.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        migrate_task(root)
    assert path.read_text(encoding="utf-8") == "{bad"
    assert not (root / "task_state.sqlite3").exists()


def test_index_only_migration_persists_issue_and_news_metadata(tmp_path):
    root = tmp_path / "index-only"
    _write(
        root / "raw" / "image_index.json",
        {
            "issue_id": "issue-7",
            "news_items": [
                {"news_id": "n1", "sequence": 1, "section": "新作", "title": "Game"}
            ],
            "candidates": [
                {
                    "id": "im1",
                    "news_id": "n1",
                    "image_url": "https://example.test/a.jpg",
                }
            ],
        },
    )
    _write(root / "raw" / "video_index.json", {"issue_id": "issue-7", "videos": []})

    report = migrate_task(root)
    repository = TaskRepository(root, create=False)
    images, videos, reviews, failures = repository.export_review_payloads()
    assert report["migrated"]
    assert not (root / "review_state.json").exists()
    assert repository.get_meta("issue_id") == "issue-7"
    assert images["news_items"][0]["news_id"] == "n1"
    assert images["candidates"][0]["id"] == "im1"
    assert videos["issue_id"] == "issue-7"
    assert repository.load_checkpoint() is None


def test_legacy_options_fallback_and_public_post_cache_filtering(tmp_path):
    root = tmp_path / "legacy-options"
    _write(root / "options.json", {"offline": True, "max_images": 3})
    _write(
        root / "x_media_cache" / "123.json",
        {
            "input_sha256": "in",
            "config_sha256": "cfg",
            "state": "pending",
            "payload": {"id": "123", "full_text": "public", "api_key": "do-not-store"},
        },
    )
    migrate_task(root)
    repository = TaskRepository(root, create=False)
    assert repository.load_options() == {"offline": True, "max_images": 3}
    post = repository.load_posts({"input_sha256": "in", "config_sha256": "cfg"})["123"]
    assert post == {"state": "pending", "payload": {"id": "123", "full_text": "public"}}


def test_old_media_journal_cannot_overwrite_index_candidate(tmp_path):
    root = tmp_path / "journal-precedence"
    news = {"news_id": "n1", "sequence": 1, "section": "new", "title": "Game"}
    _write(
        root / "raw" / "image_index.json",
        {
            "issue_id": "i1",
            "news_items": [news],
            "candidates": [
                {
                    "id": "im1",
                    "news_id": "n1",
                    "image_url": "https://example.test/current.jpg",
                },
            ],
        },
    )
    _write(root / "raw" / "video_index.json", {"issue_id": "i1", "videos": []})
    _write(
        root / "media_state" / "snapshot.json",
        {
            "input_sha256": "in",
            "config_sha256": "cfg",
            "news_id": "n1",
            "journal_revision": 1,
            "candidates": [
                {
                    "id": "im1",
                    "news_id": "n1",
                    "image_url": "https://example.test/old.jpg",
                }
            ],
        },
    )
    migrate_task(root)
    repository = TaskRepository(root, create=False)
    candidate = repository.query_candidates(news_id="n1", kind="image")[0]
    assert candidate["image_url"] == "https://example.test/current.jpg"
    assert repository.load_journal({"input_sha256": "in", "config_sha256": "cfg"})[
        1
    ] == {"n1"}


def test_checkpoint_isolates_bad_candidate_record_and_keeps_valid_checkpoint(tmp_path):
    root = tmp_path / "checkpoint-task"
    news = NewsItem(issue_id="i1", sequence=1, section="新作", title="Game", body="")
    issue = Issue(issue_id="i1", input_path="legacy://input", news_items=[news])
    candidate = ImageCandidate(
        news_id=news.id,
        image_url="https://example.test/a.jpg",
        source_url="https://example.test/",
        fetched_at=datetime.now(timezone.utc),
    )
    checkpoint = Checkpoint(
        status="cancelled",
        task_id="task-1",
        issue_id="i1",
        input_sha256="input",
        config_sha256="config",
        issue=issue,
        candidates=[candidate],
    ).model_dump(mode="json")
    checkpoint["candidates"].append({"id": "broken"})
    _write(root / "checkpoint.json", checkpoint)

    report = migrate_task(root)
    loaded = TaskRepository(root, create=False).load_checkpoint()
    assert report["migrated"]
    assert len(loaded["candidates"]) == 1
    assert any(item.get("collection") == "candidates" for item in report["diagnostics"])


def test_cancelled_migration_stays_non_authoritative_and_can_resume(tmp_path):
    root = tmp_path / "task"
    _write(root / "raw" / "image_index.json", {"candidates": [], "news_items": []})
    _write(root / "raw" / "video_index.json", {"videos": []})
    report = migrate_task(root, cancelled=lambda: True)
    assert report["cancelled"] is True
    assert not (root / "review_state.json").exists()
    assert not TaskRepository.is_authoritative(root)
    resumed = migrate_task(root)
    assert resumed["migrated"] is True
    assert TaskRepository.is_authoritative(root)


def test_corrupt_retry_batch_is_diagnostic_and_valid_batch_survives(tmp_path):
    root = tmp_path / "task"
    valid = root / "retries" / "n1" / "ok" / "image_index.json"
    _write(
        root / "raw" / "image_index.json",
        {
            "issue_id": "i1",
            "candidates": [],
            "news_items": [
                {
                    "news_id": "n1",
                    "id": "n1",
                    "sequence": 1,
                    "title": "A",
                    "section": "新作",
                }
            ],
        },
    )
    _write(root / "raw" / "video_index.json", {"issue_id": "i1", "videos": []})
    _write(
        valid,
        {
            "kind": "retry",
            "status": "completed",
            "candidates": [
                {
                    "id": "im1",
                    "news_id": "n1",
                    "image_url": "https://example.test/a.jpg",
                }
            ],
            "videos": [],
            "failures": [],
        },
    )
    corrupt = root / "retries" / "n1" / "bad" / "image_index.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("[]", encoding="utf-8")
    mismatch = root / "retries" / "n1" / "wrong-news" / "image_index.json"
    _write(mismatch, {"news_id": "n2", "candidates": []})
    report = migrate_task(root)
    assert report["counts"]["retry_batches"] == 1
    assert any(
        item["source"].replace("\\", "/").endswith("bad/image_index.json")
        for item in report["diagnostics"]
    )
    assert any(
        "batch news_id does not match" in item["error"]
        for item in report["diagnostics"]
    )
    assert (
        TaskRepository(root, create=False).read_batch("n1", "ok")["attempt_id"] == "ok"
    )


def test_stale_candidate_root_is_rebased_only_when_hash_matches(tmp_path):
    root = tmp_path / "current" / "copied-task"
    old_root = tmp_path / "old" / "copied-task"
    asset = root / "raw" / "n1" / "a.png"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(asset, format="PNG")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    stale = str(old_root / "raw" / "n1" / "a.png")
    _write(
        root / "raw" / "image_index.json",
        {
            "candidates": [
                {
                    "id": "im1",
                    "news_id": "n1",
                    "image_url": "https://example.test/a.jpg",
                    "local_path": stale,
                    "sha256": digest,
                    "download_status": "downloaded",
                }
            ]
        },
    )
    TaskRepository(root, create=True)
    # The pre-existing, empty repository is not authoritative; migration can
    # resume it, but should only use the copied asset with its matching hash.
    migrate_task(root)
    saved = TaskRepository(root, create=False).query_candidates(kind="image")[0]
    assert saved["local_path"] == str(asset.resolve())
    assert (
        hashlib.sha256((root / saved["local_path"]).read_bytes()).hexdigest() == digest
    )
