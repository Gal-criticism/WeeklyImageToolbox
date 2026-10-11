import json
from pathlib import Path

import pytest

from galgame_news.review.session import ReviewDecision, ReviewSession


def _output(root: Path):
    root.mkdir(parents=True)
    for name, payload in {
        "image_index.json": {"candidates": [], "news_items": []},
        "video_index.json": {"videos": [], "news_items": []},
        "review_required.json": [],
    }.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_corrupt_main_index_and_state_raise_without_overwrite(tmp_path):
    output = _output(tmp_path / "output")
    (output / "image_index.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="审核数据"):
        ReviewSession.from_output(output)
    (output / "image_index.json").write_text('{"candidates": [], "news_items": []}', encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="审核数据"):
        ReviewSession.from_output(output, state_path=state)
    assert state.read_text(encoding="utf-8") == "{broken"


def test_batch_decisions_increment_once_and_snapshot_is_independent(tmp_path):
    output = _output(tmp_path / "output")
    session = ReviewSession.from_output(output, state_path=tmp_path / "state.json")
    snap = session.query_snapshot()
    assert snap.revision == session.revision
    session.set_decisions([], ReviewDecision.ACCEPTED, persist=False)
    assert session.revision == snap.revision
    payload = session.decision_state_payload()
    assert payload["revision"] == session.revision


@pytest.mark.parametrize("payload", [{"candidates": "broken"}, 42, {"candidates": [], "news_items": "broken"}])
def test_structurally_corrupt_main_index_does_not_create_state(tmp_path, payload):
    output = _output(tmp_path / "output")
    path = output / "image_index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = tmp_path / "state.json"
    with pytest.raises(ValueError, match="损坏"):
        ReviewSession.from_output(output, state_path=state)
    assert not state.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_state_payload_preserves_other_batch_ids(tmp_path):
    output = _output(tmp_path / "output")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"decisions": {"future-id": "accepted"}, "manual_decisions": ["future-id"]}), encoding="utf-8")
    session = ReviewSession.from_output(output, state_path=state)
    payload = session.decision_state_payload()
    assert payload["decisions"]["future-id"] == "accepted"
    assert "future-id" in payload["manual_decisions"]


def test_legacy_plain_state_preserves_decisions_and_rejects_corrupt_manual_list(tmp_path):
    output = _output(tmp_path / "output")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"future-id": "accepted"}), encoding="utf-8")
    session = ReviewSession.from_output(output, state_path=state)
    assert session.decision_state_payload()["decisions"]["future-id"] == "accepted"
    corrupt = {"decisions": {}, "manual_decisions": "broken"}
    state.write_text(json.dumps(corrupt), encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        ReviewSession.persist_state_payload(state, session.decision_state_payload())
    assert json.loads(state.read_text(encoding="utf-8")) == corrupt


def test_retry_delta_isolates_bad_record_and_keeps_failed_link(tmp_path):
    from PIL import Image
    output = _output(tmp_path / "output")
    task = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task, state_path=task / "review_state.json")
    attempt = task / "retries" / "n1" / "a1"
    attempt.mkdir(parents=True)
    Image.new("RGB", (800, 600), "red").save(attempt / "valid.png")
    def row(name, **kwargs):
        return {"id": name, "news_id": "n1", "image_url": f"https://example.test/{name}.png",
                "width": 800, "height": 600, **kwargs}
    payload = {"candidates": [row("ok", local_path="valid.png"), "broken",
                              row("outside", local_path=str(tmp_path / "foreign.png")),
                              row("wrong-news", news_id="n2"), row("failed", local_path="missing.png")]}
    (attempt / "image_index.json").write_text(json.dumps(payload), encoding="utf-8")
    delta = session.query_snapshot().prepare_retry_delta("n1", "a1")
    assert len(delta.diagnostics) == 4
    session.apply_retry_delta(delta, force_pending=True)
    assert set(session._image_by_id) == {"ok", "failed"}
    assert session.decision("ok") is ReviewDecision.PENDING
    assert session._image_by_id["failed"].image_url == "https://example.test/failed.png"
    assert session._source_paths["ok"].is_file()


def test_corrupt_failure_log_keeps_valid_batch_and_cancel_status(tmp_path):
    output = _output(tmp_path / "output")
    task = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task, state_path=task / "review_state.json")
    attempt = task / "retries" / "n1" / "cancelled"
    attempt.mkdir(parents=True)
    (attempt / "image_index.json").write_text(json.dumps({
        "kind": "retry", "status": "cancelled", "candidates": [],
    }), encoding="utf-8")
    (attempt / "failed_items.json").write_text("{broken", encoding="utf-8")
    delta = session.prepare_retry_delta("n1", "cancelled")
    assert delta.status == "cancelled"
    assert delta.failures == ()
    assert delta.diagnostics[0]["code"] == "corrupt_json"
    assert (attempt / "failed_items.json").read_text(encoding="utf-8") == "{broken"
