from __future__ import annotations

import json
import threading

from PIL import Image

from galgame_news.desktop.review_jobs import ReviewStateWriter
from galgame_news.desktop.review_page import ReviewPage
from galgame_news.review import ReviewDecision, ReviewSession


def _session(root, *, count=2):
    root.mkdir(parents=True)
    candidates = []
    for index in range(count):
        Image.new("RGB", (40, 30), (20 + index, 80, 120)).save(root / f"{index}.jpg")
        candidates.append({"id": f"image-{index}", "news_id": "n1", "image_url": f"https://example.test/{index}.jpg",
                           "source_url": "https://example.test/news", "local_path": f"{index}.jpg",
                           "image_type": "game_cg", "width": 800, "height": 600})
    (root / "image_index.json").write_text(json.dumps({"candidates": candidates, "news_items": [{"news_id": "n1", "title": "Test"}]}), encoding="utf-8")
    (root / "video_index.json").write_text(json.dumps({"videos": [], "news_items": []}), encoding="utf-8")
    return ReviewSession.from_output(root, state_path=root.parent / f"{root.name}-state.json")


def test_writer_coalesces_blocked_versions_and_persists_newest(qtbot, tmp_path, monkeypatch):
    session = _session(tmp_path / "output")
    writer = ReviewStateWriter()
    entered, release = threading.Event(), threading.Event()
    writes = []
    original = ReviewSession.persist_state_payload

    def controlled(path, payload):
        writes.append(dict(payload))
        if len(writes) == 1:
            entered.set()
            assert release.wait(5)
        original(path, payload)

    monkeypatch.setattr(ReviewSession, "persist_state_payload", staticmethod(controlled))
    session.set_decisions(["image-0"], ReviewDecision.ACCEPTED, persist=False)
    writer.enqueue(session)
    assert entered.wait(2)
    session.set_decisions(["image-1"], ReviewDecision.REJECTED, persist=False)
    writer.enqueue(session)
    session.set_decisions(["image-0"], ReviewDecision.PENDING, persist=False)
    writer.enqueue(session)
    release.set()
    qtbot.waitUntil(lambda: not writer.busy, timeout=5000)
    saved = json.loads(session.state_path.read_text(encoding="utf-8"))
    assert saved["revision"] == session.revision
    assert saved["decisions"]["image-0"] == "pending"
    assert saved["decisions"]["image-1"] == "rejected"
    assert len(writes) == 2  # active write plus the latest coalesced snapshot


def test_writer_failure_is_visible_and_retry_saves(qtbot, tmp_path, monkeypatch):
    session = _session(tmp_path / "output", count=1)
    writer = ReviewStateWriter()
    should_fail = [True]
    original = ReviewSession.persist_state_payload

    def maybe_fail(path, payload):
        if should_fail[0]:
            raise OSError("controlled disk failure")
        original(path, payload)

    monkeypatch.setattr(ReviewSession, "persist_state_payload", staticmethod(maybe_fail))
    session.set_decisions(["image-0"], ReviewDecision.ACCEPTED, persist=False)
    writer.enqueue(session)
    qtbot.waitUntil(lambda: writer.has_failure(session), timeout=5000)
    assert not writer.is_saved(session)
    should_fail[0] = False
    writer.retry(session)
    qtbot.waitUntil(lambda: writer.is_saved(session) and not writer.busy, timeout=5000)
    assert json.loads(session.state_path.read_text(encoding="utf-8"))["decisions"]["image-0"] == "accepted"


def test_session_switch_discards_stale_review_query(qtbot, tmp_path, monkeypatch):
    first = _session(tmp_path / "first", count=1)
    second = _session(tmp_path / "second", count=2)
    page = ReviewPage()
    qtbot.addWidget(page)
    entered, release = threading.Event(), threading.Event()
    original = ReviewPage._query_snapshot_candidates
    blocked = [True]

    def delayed(snapshot, news_id, section, tab):
        if blocked[0]:
            blocked[0] = False
            entered.set()
            assert release.wait(5)
        return original(snapshot, news_id, section, tab)

    monkeypatch.setattr(ReviewPage, "_query_snapshot_candidates", staticmethod(delayed))
    page.set_session(first)
    assert entered.wait(2)
    page.set_session(second)
    release.set()
    qtbot.waitUntil(lambda: not page.query.busy and len(page._items) == 2, timeout=5000)
    assert page.session is second
    assert {str(item.id) for item in page._items} == {"image-0", "image-1"}
    assert all(item.news_id == "n1" for item in page._items)

