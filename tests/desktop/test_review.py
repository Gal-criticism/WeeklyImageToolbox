from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from galgame_news.desktop.review_page import ReviewPage
from galgame_news.review import ReviewDecision, ReviewSession


def _output(tmp_path: Path) -> tuple[Path, ReviewSession]:
    output = tmp_path / "output"
    output.mkdir()
    Image.new("RGB", (800, 600), "red").save(output / "one.jpg")
    (output / "image_index.json").write_text(
        json.dumps(
            {
                "issue_id": "issue-1",
                "news_items": [{"news_id": "news-1", "sequence": 1, "title": "News"}],
                "candidates": [
                    {
                        "id": "image-1",
                        "news_id": "news-1",
                        "image_url": "https://example.test/one.jpg",
                        "source_url": "https://example.test/source",
                        "local_path": "one.jpg",
                        "width": 800,
                        "height": 600,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    session = ReviewSession.from_output(output, state_path=tmp_path / "state.json")
    return output, session


def wait_ready(qtbot, page: ReviewPage) -> None:
    qtbot.waitUntil(lambda: (
        not page.query.busy
        and page._render_cursor >= len(page._render_source)
        and page.query.pool.activeThreadCount() == 0
        and page.state_writer.pool.activeThreadCount() == 0
        and not page.query._retired
        and not page.state_writer._retired
        and (page._query_identity is None or (
            page.session is not None
            and page._query_identity[0] == id(page.session)
            and page._query_identity[1] == page.session.revision
        ))
    ), timeout=10000)


def test_review_page_persists_decision_and_exposes_source(qtbot, tmp_path: Path):
    output, session = _output(tmp_path)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    assert page.media_list.count() == 1
    page.media_list.setCurrentRow(0)
    page.accept_selected()
    assert session.decision("image-1") is ReviewDecision.ACCEPTED
    page.tabs.setCurrentIndex(0)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    assert page.source_button.isEnabled()
    assert page.retry_button.isEnabled()


def test_review_page_keeps_failed_candidates_in_session_order(qtbot, tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    candidates = []
    for index, score, color in (("b", 20, "blue"), ("a", 80, "green")):
        path = output / f"{index}.jpg"
        Image.new("RGB", (800, 600), color).save(path)
        candidates.append(
            {
                "id": f"image-{index}",
                "news_id": "news-1",
                "image_url": f"https://example.test/{index}.jpg",
                "source_url": "https://example.test/source",
                "local_path": path.name,
                "width": 800,
                "height": 600,
                "score": {"total": score, "relevance": score / 100},
            }
        )
    (output / "image_index.json").write_text(
        json.dumps({"candidates": candidates}), encoding="utf-8"
    )
    (output / "failed_items.json").write_text(
        json.dumps([{"candidate_id": "image-a"}, {"candidate_id": "image-b"}]),
        encoding="utf-8",
    )
    session = ReviewSession.from_output(output, state_path=tmp_path / "state.json")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    assert [str(item.id) for item in page.failed_candidates()] == ["image-a", "image-b"]


def test_review_page_zoom_controls_change_scale(qtbot):
    page = ReviewPage()
    qtbot.addWidget(page)
    assert page.zoom_factor == 1.0
    page.zoom_in()
    page.zoom_out()
    assert page.zoom_factor == 1.0


def test_pending_x_photos_sort_before_higher_scored_website_images(tmp_path: Path):
    output, _session = _output(tmp_path)
    payload = json.loads((output / "image_index.json").read_text(encoding="utf-8"))
    original = payload["candidates"][0]
    original["score"] = {"total": 99}
    Image.new("RGB", (800, 600), "blue").save(output / "x.jpg")
    payload["candidates"].append({
        **original, "id": "x-photo", "image_url": "https://pbs.twimg.com/media/photo?name=orig",
        "local_path": "x.jpg", "source_type": "official_x", "score": {"total": 1},
        "signals": {"socialdata_photo": True},
    })
    (output / "image_index.json").write_text(json.dumps(payload), encoding="utf-8")
    session = ReviewSession.from_output(output, state_path=tmp_path / "new-state.json")
    assert [str(image.id) for image in session.pending_images()] == ["x-photo", "image-1"]


def test_review_page_retry_exposes_optional_official_url(qtbot, tmp_path: Path):
    _output_dir, session = _output(tmp_path)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    wait_ready(qtbot, page)
    page.media_list.setCurrentRow(0)
    page.retry_url_edit.setText("https://official.example/retry")
    category = page.news_list.topLevelItem(0).child(2)  # The fixture belongs to 周边.
    page.news_list.setCurrentItem(category.child(0))
    seen: list[str] = []
    page.retry_requested.connect(seen.append)

    page.retry_selected()

    assert seen == ["news-1"]
    assert page.retry_url() == "https://official.example/retry"


def test_supplement_merge_defaults_new_assets_to_pending_and_preserves_decisions(tmp_path: Path):
    output, session = _output(tmp_path)
    session.task_root = tmp_path / "task"
    session.set_decision("image-1", ReviewDecision.ACCEPTED)
    retry_root = tmp_path / "task" / "retries" / "news-1" / "batch-1"
    retry_root.mkdir(parents=True)
    Image.new("RGB", (400, 400), "green").save(retry_root / "repaired.jpg")
    Image.new("RGB", (400, 400), "blue").save(retry_root / "new.jpg")
    original = json.loads((output / "image_index.json").read_text(encoding="utf-8"))["candidates"][0]
    repaired = {**original, "local_path": "missing.jpg", "selected": False}
    new = {**original, "id": "image-new", "image_url": "https://example.test/new.jpg",
           "local_path": "new.jpg", "selected": True}
    (retry_root / "image_index.json").write_text(
        json.dumps({"kind": "supplement", "candidates": [
            {**repaired, "local_path": "repaired.jpg"}, new
        ]}), encoding="utf-8"
    )
    # Simulate the prior candidate's asset being unavailable at reload time.
    session._source_paths["image-1"] = retry_root / "gone.jpg"
    from galgame_news.tasks.store import RetryAttempt
    attempt = RetryAttempt("task", "news-1", "batch-1", retry_root,
                           [{**repaired, "local_path": "repaired.jpg"}, new], [], [], "supplement")

    session.merge_retry_attempt(attempt, force_pending=True)

    assert session.decision("image-1") is ReviewDecision.ACCEPTED
    assert session._source_paths["image-1"] == (retry_root / "repaired.jpg").resolve()
    assert session.decision("image-new") is ReviewDecision.PENDING
    assert session._source_paths["image-new"] == (retry_root / "new.jpg").resolve()


def test_supplement_repairs_auto_rejected_candidate_and_restores_saved_manual_choice(tmp_path: Path):
    output, session = _output(tmp_path)
    session.task_root = tmp_path / "task"
    session._source_paths.pop("image-1")
    assert session.decision("image-1") is ReviewDecision.PENDING
    # Simulate the auto-rejected state produced when the initial download failed.
    session._decisions["image-1"] = ReviewDecision.REJECTED
    session._manual_decisions.discard("image-1")
    retry_root = session.task_root / "retries" / "news-1" / "batch-2"
    retry_root.mkdir(parents=True)
    Image.new("RGB", (400, 400), "green").save(retry_root / "restored.jpg")
    original = json.loads((output / "image_index.json").read_text(encoding="utf-8"))["candidates"][0]
    recovered = {**original, "local_path": "restored.jpg"}
    from galgame_news.tasks.store import RetryAttempt
    attempt = RetryAttempt("task", "news-1", "batch-2", retry_root, [recovered], [],
                           [{"candidate_id": "image-1"}], "supplement")

    session.merge_retry_attempt(attempt, force_pending=True)

    assert session.decision("image-1") is ReviewDecision.PENDING
    assert "image-1" not in session._failed_ids
    saved = json.loads(session.state_path.read_text(encoding="utf-8"))
    assert saved["decisions"]["image-1"] == ReviewDecision.PENDING.value


def test_supplement_does_not_import_candidates_for_another_news_item(tmp_path: Path):
    _output_dir, session = _output(tmp_path)
    session.task_root = tmp_path / "task"
    retry_root = session.task_root / "retries" / "news-1" / "batch-3"
    retry_root.mkdir(parents=True)
    from galgame_news.tasks.store import RetryAttempt
    attempt = RetryAttempt("task", "news-1", "batch-3", retry_root,
                           [{"id": "cross-news", "news_id": "news-2", "image_url": "https://example.test/x"}],
                           [], [], "supplement")

    result = session.merge_retry_attempt(attempt, force_pending=True)

    assert result.added_ids == []
    assert "cross-news" not in session._image_by_id


def test_sequential_supplement_merges_preserve_later_batches_manual_decisions(tmp_path: Path):
    _output_dir, session = _output(tmp_path)
    session.task_root = tmp_path / "task"
    saved_state = json.loads(session.state_path.read_text(encoding="utf-8"))
    saved_state["decisions"].update({
        "image-batch-one": ReviewDecision.ACCEPTED.value,
        "image-batch-two": ReviewDecision.REJECTED.value,
    })
    saved_state["manual_decisions"] = ["image-batch-one", "image-batch-two"]
    session.state_path.write_text(json.dumps(saved_state), encoding="utf-8")
    from galgame_news.tasks.store import RetryAttempt
    attempts = []
    for batch_id, media_id, color in (("batch-one", "image-batch-one", "blue"),
                                      ("batch-two", "image-batch-two", "green")):
        root = session.task_root / "retries" / "news-1" / batch_id
        root.mkdir(parents=True)
        Image.new("RGB", (800, 600), color).save(root / "asset.jpg")
        candidate = {
            "id": media_id, "news_id": "news-1",
            "image_url": f"https://example.test/{media_id}",
            "source_url": "https://example.test/source", "local_path": "asset.jpg",
            "width": 800, "height": 600,
        }
        attempts.append(RetryAttempt("task", "news-1", batch_id, root, [candidate], [], [], "supplement"))

    session.merge_retry_attempt(attempts[0], force_pending=True)
    mid_state = json.loads(session.state_path.read_text(encoding="utf-8"))
    assert mid_state["decisions"]["image-batch-two"] == ReviewDecision.REJECTED.value
    assert "image-batch-two" in mid_state["manual_decisions"]
    session.merge_retry_attempt(attempts[1], force_pending=True)

    assert session.decision("image-batch-one") is ReviewDecision.ACCEPTED
    assert session.decision("image-batch-two") is ReviewDecision.REJECTED
