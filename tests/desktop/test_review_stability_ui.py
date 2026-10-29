from __future__ import annotations

import json
import time
from pathlib import Path

from PIL import Image

from galgame_news.desktop.review_page import ReviewPage
from galgame_news.review import ReviewDecision, ReviewSession


def _session(root: Path, count: int = 3) -> ReviewSession:
    root.mkdir(parents=True)
    candidates = []
    for i in range(count):
        Image.new("RGB", (900, 700), "blue").save(root / f"{i}.jpg")
        candidates.append({"id": f"image-{i}", "news_id": "n1",
                           "image_url": f"https://example.test/{i}.jpg",
                           "source_url": "https://example.test/news", "source_type": "official_site",
                           "local_path": f"{i}.jpg", "width": 900, "height": 700,
                           "image_type": "game_cg", "score": {"total": 80}})
    (root / "image_index.json").write_text(json.dumps({
        "schema_version": 1,
        "news_items": [{"news_id": "n1", "title": "新闻一"}],
        "candidates": candidates,
    }), encoding="utf-8")
    (root / "video_index.json").write_text(json.dumps({"schema_version": 1, "news_items": [], "videos": []}), encoding="utf-8")
    return ReviewSession.from_output(root, state_path=root / "review-state.json")


def _wait_loaded(qtbot, page: ReviewPage, count: int) -> None:
    qtbot.waitUntil(lambda: not page.query.busy and len(page._items) == count, timeout=5000)
    assert len(page._items) == count, f"summary={page.grid_summary.text()}, items={len(page._items)}"


def test_async_refresh_reuses_list_items_and_discards_stale_result(qtbot, tmp_path, monkeypatch):
    session = _session(tmp_path / "output", 3)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    _wait_loaded(qtbot, page, 3)
    first = page.media_list.item(0)

    original = ReviewPage._query_snapshot_candidates
    def slow(snapshot, news_id, allowed, tab):
        if news_id is None:
            time.sleep(0.08)
        return original(snapshot, news_id, allowed, tab)
    monkeypatch.setattr(ReviewPage, "_query_snapshot_candidates", staticmethod(slow))
    page.refresh(preserve_position=True)
    page.refresh(preserve_position=True)
    _wait_loaded(qtbot, page, 3)
    assert page.media_list.item(0) is first
    page.tabs.setCurrentIndex(0)
    qtbot.waitUntil(lambda: not page.query.busy and len(page._items) == 0, timeout=5000)

    assert page._items == []
    page.tabs.setCurrentIndex(1)
    _wait_loaded(qtbot, page, 3)


def test_decision_updates_memory_then_uses_background_writer(qtbot, tmp_path):
    session = _session(tmp_path / "output", 1)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    _wait_loaded(qtbot, page, 1)
    page.media_list.item(0).setSelected(True)
    page.accept_selected()

    assert session.decision("image-0") is ReviewDecision.ACCEPTED
    assert page.state_writer.busy or page.state_writer.is_saved(session)
    qtbot.waitUntil(lambda: page.state_writer.is_saved(session), timeout=5000)
    assert json.loads(Path(session.state_path).read_text(encoding="utf-8"))["decisions"]["image-0"] == "accepted"
    assert "已保存" in page.save_status.text()


def test_export_waits_for_latest_state_write(qtbot, tmp_path, monkeypatch):
    session = _session(tmp_path / "output", 1)
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    _wait_loaded(qtbot, page, 1)
    page.media_list.item(0).setSelected(True)
    page.accept_selected()
    assert page.state_writer.busy

    called = []
    monkeypatch.setattr(page.exporter, "start", lambda value, **kwargs: called.append(value) or False)
    page.export_final()
    assert page._export_waiting_session is session
    assert not called
    qtbot.waitUntil(lambda: page._export_waiting_session is None, timeout=5000)
    assert called == [session]


def test_async_video_rows_keep_local_playback_entry(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    root = tmp_path / "video-output"
    _session(root, 0)
    (root / "sample.mp4").write_bytes(b"offline video placeholder")
    (root / "video_index.json").write_text(json.dumps({"videos": [{
        "id": "video-1", "news_id": "n1", "source_type": "official_site",
        "source_url": "https://example.test/news", "video_url": "https://example.test/sample.mp4",
        "local_path": "sample.mp4",
    }]}), encoding="utf-8")
    session = ReviewSession.from_output(root, state_path=root / "review-state.json")
    page = ReviewPage()
    qtbot.addWidget(page)
    page.set_session(session)
    page.tabs.setCurrentIndex(5)
    _wait_loaded(qtbot, page, 1)
    page.media_list.setCurrentRow(0)
    assert page.play_button.isEnabled()
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url) or True)
    page.play_video()
    assert Path(urls[0].toLocalFile()) == root / "sample.mp4"

