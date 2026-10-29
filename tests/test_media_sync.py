from io import BytesIO
import json

from PIL import Image

from galgame_news.media_sync import reconcile_task, task_media_roots
from galgame_news.tasks.repository import TaskRepository


def _png(color=(20, 80, 140)):
    data = BytesIO()
    Image.new("RGB", (8, 6), color).save(data, "PNG")
    return data.getvalue()


def _task(root):
    repo = TaskRepository(root)
    repo.activate()
    repo.save_checkpoint({"issue_id": "261", "issue": {"issue_id": "261", "news_items": [
        {"id": "news-a", "news_id": "news-a", "sequence": 1,
         "section": "\u65b0\u4f5c", "title": "Game update", "game_names": ["Game"]}
    ]}})
    return repo


def test_known_roots_have_separate_review_and_final_roles(tmp_path):
    root = tmp_path / "task"
    for relative in ("assets/originals", "assets/review", "raw/images", "final/images"):
        (root / relative).mkdir(parents=True)
    roots = task_media_roots(root)
    assert [(item.path.relative_to(root).as_posix(), item.role) for item in roots] == [
        ("assets/originals", "originals"), ("assets/review", "review"),
        ("raw/images", "review"), ("final/images", "final"),
    ]


def test_manual_canonical_folder_image_imports_once_as_pending(tmp_path):
    root = tmp_path / "task"
    repo = _task(root)
    manual = root / "assets" / "review" / "x1" / "manual.png"
    manual.parent.mkdir(parents=True)
    manual.write_bytes(_png())

    report = reconcile_task(root)
    rows = list(repo.iter_candidates(news_id="news-a"))

    assert report["complete"] is True
    assert report["last_checked_utc"]
    assert report["by_role"]["review"] == 1
    assert report["physical_unique"] == report["usable_unique"] == 1
    assert report["imported"] == 1
    assert len(rows) == 1
    assert rows[0]["local_path"] == str(manual.resolve())
    assert rows[0]["selected"] is False
    assert rows[0]["signals"]["provenance"] == "manual_unverified"

    again = reconcile_task(root)
    assert again["imported"] == 0
    assert len(list(repo.iter_candidates(news_id="news-a"))) == 1


def test_moved_manual_image_keeps_candidate_and_replaced_file_is_new(tmp_path):
    root = tmp_path / "task"
    repo = _task(root)
    old = root / "raw" / "images" / "Game" / "photo.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(_png())
    reconcile_task(root)
    candidate = list(repo.iter_candidates(news_id="news-a"))[0]
    candidate_id = candidate["id"]
    repo.save_review_state({"revision": 1, "decisions": {candidate_id: "accepted"},
                            "manual_decisions": [candidate_id]})

    moved = root / "assets" / "review" / "x1" / "renamed.png"
    moved.parent.mkdir(parents=True)
    old.rename(moved)
    moved_report = reconcile_task(root)
    rows = list(repo.iter_candidates(news_id="news-a"))
    assert moved_report["changes"]["moved"]
    assert len(rows) == 1 and rows[0]["id"] == candidate_id
    assert rows[0]["local_path"] == str(moved.resolve())
    assert rows[0]["signals"]["media_sync_available"] is True
    assert repo.load_review_state()["decisions"][candidate_id] == "accepted"

    moved.write_bytes(_png((200, 50, 10)))
    replaced = reconcile_task(root)
    rows = list(repo.iter_candidates(news_id="news-a"))
    assert replaced["changes"]["modified"]
    assert len(rows) == 1 and rows[0]["id"] == candidate_id
    assert rows[0]["sha256"] != candidate["sha256"]
    assert rows[0]["signals"]["media_sync_available"] is True
    state = repo.load_review_state()
    assert state["decisions"][candidate_id] == "pending"
    assert candidate_id not in state["manual_decisions"]


def test_final_manifest_owned_export_is_not_reimported(tmp_path):
    root = tmp_path / "task"
    repo = _task(root)
    final = root / "final" / "images" / "x1" / "x1.01.png"
    final.parent.mkdir(parents=True)
    content = _png()
    final.write_bytes(content)
    import hashlib
    digest = hashlib.sha256(content).hexdigest()
    (root / "final" / "final_manifest.json").write_text(json.dumps({
        "files": ["images/x1/x1.01.png"],
        "file_sha256": {"images/x1/x1.01.png": digest},
    }), encoding="utf-8")

    report = reconcile_task(root)

    assert report["complete"] is True
    assert report["imported"] == 0
    assert list(repo.iter_candidates(news_id="news-a")) == []


def test_legacy_index_is_preserved_and_manual_files_use_separate_overlay(tmp_path):
    root = tmp_path / "legacy"
    index = root / "raw" / "image_index.json"
    index.parent.mkdir(parents=True)
    original = json.dumps({"issue_id": "261", "news_items": [{
        "news_id": "news-a", "sequence": 1, "section": "\u65b0\u4f5c",
        "title": "Game update", "game_names": ["Game"]}], "candidates": []}, ensure_ascii=False)
    index.write_text(original, encoding="utf-8")
    manual = root / "assets" / "review" / "Game" / "manual.png"
    manual.parent.mkdir(parents=True)
    manual.write_bytes(_png())

    report = reconcile_task(root)
    overlay = json.loads((root / "media_sync_overlay.json").read_text(encoding="utf-8"))

    assert report["imported"] == 1
    assert index.read_text(encoding="utf-8") == original
    assert overlay["candidates"][0]["news_id"] == "news-a"
    assert overlay["candidates"][0]["selected"] is False
