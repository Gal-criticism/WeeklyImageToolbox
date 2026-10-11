"""Fixed-size, local-only benchmark for review latency and GUI responsiveness."""
from __future__ import annotations

import hashlib
import json
import statistics
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QDesktopServices
import PySide6

from galgame_news.curation.review_filter import reviewable_pending_images
from galgame_news.desktop.review_page import ReviewPage
from galgame_news.review import ReviewDecision, ReviewSession


NEWS_COUNT = 50
CANDIDATES_PER_NEWS = 60


def _benchmark_session(tmp_path):
    output = tmp_path / "benchmark-output"
    output.mkdir()
    assets = []
    # Deterministic, locally generated 800x600 assets. Most pHashes are far
    # apart, so this exercises indexing without making the fixture depend on
    # expensive visual-comparison behavior.
    for index in range(CANDIDATES_PER_NEWS - 2):
        image = Image.new("RGB", (800, 600), (12 + index * 3 % 220, 20, 45))
        draw = ImageDraw.Draw(image)
        draw.rectangle((index * 7, 40, index * 7 + 110, 180), fill=(20, 15 + index * 4 % 230, 210))
        draw.line((0, index * 8, 799, 599 - index * 6), fill=(230, 180, index * 5 % 250), width=17)
        path = output / f"asset-{index:02d}.jpg"
        image.save(path, format="JPEG", quality=82)
        assets.append((path, hashlib.sha256(path.read_bytes()).hexdigest()))

    news_items = []
    candidates = []
    for news_index in range(NEWS_COUNT):
        news_id = f"news-{news_index:02d}"
        news_items.append({"news_id": news_id, "sequence": news_index + 1, "section": "新作", "title": f"News {news_index}"})
        rows = []
        for asset_index, (path, digest) in enumerate(assets):
            rows.append({
                "id": f"{news_id}-image-{asset_index:02d}", "news_id": news_id,
                "image_url": f"https://local.invalid/{news_id}/{asset_index}.jpg",
                "source_url": "https://local.invalid/article", "image_type": "game_cg",
                "local_path": str(path), "width": 800, "height": 600,
                "sha256": digest, "perceptual_hash": f"{asset_index * 0x1111111111111111:016x}",
                "score": {"total": 70.0},
            })
        # Two exact-content variants ensure same-hash suppression is present
        # for every news item while preserving 60 candidate records per item.
        for duplicate_index in range(2):
            path, digest = assets[duplicate_index]
            rows.append({
                "id": f"{news_id}-variant-{duplicate_index}", "news_id": news_id,
                "image_url": f"https://local.invalid/{news_id}/variant-{duplicate_index}.jpg",
                "source_url": "https://local.invalid/article", "image_type": "game_cg",
                "local_path": str(path), "width": 800, "height": 600, "sha256": digest,
                "perceptual_hash": f"{duplicate_index * 0x1111111111111111:016x}",
                "score": {"total": 60.0},
            })
        candidates.extend(rows)
    (output / "image_index.json").write_text(json.dumps({"news_items": news_items, "candidates": candidates}), encoding="utf-8")
    (output / "video_index.json").write_text(json.dumps({"news_items": [], "videos": []}), encoding="utf-8")
    (output / "review_required.json").write_text("[]", encoding="utf-8")
    return ReviewSession.from_output(output, state_path=tmp_path / "state.json")


def _settled(page):
    expected = len(getattr(page, "_render_rows", page._render_source))
    return not page.query.busy and page._render_cursor >= expected


def _wait_for_render(qtbot, page):
    try:
        deadline = time.perf_counter() + 30
        while not _settled(page):
            if time.perf_counter() >= deadline:
                raise TimeoutError("review rendering did not finish")
            qtbot.wait(1)
    except Exception:
        print("render state:", page.query.busy, page._render_cursor,
              len(getattr(page, "_render_rows", [])), len(page._items),
              page.grid_summary.text(), page.save_status.text())
        raise


def _baseline_visible_ids(session):
    visible = []
    for news_id in dict.fromkeys(str(item.news_id) for item in session.images):
        pending = [item for item in session.images if str(item.news_id) == news_id
                   and session.decision(str(item.id)) is ReviewDecision.PENDING]
        visible.extend(reviewable_pending_images(pending, accepted=session.accepted_images(news_id)))
    return {str(item.id) for item in visible}


def test_review_3000_candidate_latency_and_gui_heartbeat(qtbot, tmp_path, monkeypatch):
    print(f"benchmark environment: {platform.platform()} Python={platform.python_version()} PySide6={PySide6.__version__} Qt=offscreen 1280x800")
    session = _benchmark_session(tmp_path)
    assert len(session.images) == NEWS_COUNT * CANDIDATES_PER_NEWS
    # Warm every news group and the scene pair cache. The following GUI query
    # still performs its own snapshot/filter pass and renders all 2,900 visible rows.
    baseline_ids = _baseline_visible_ids(session)
    assert len(baseline_ids) == NEWS_COUNT * (CANDIDATES_PER_NEWS - 2)
    assert {str(item.id) for item in session.pending_images()} == baseline_ids

    page = ReviewPage()
    qtbot.addWidget(page)
    page.resize(1280, 800)
    heartbeat = []
    timer = QTimer(page)
    timer.setInterval(20)
    timer.timeout.connect(lambda: heartbeat.append(time.perf_counter()))
    timer.start()
    slow_done = threading.Event()

    def slow_local_grab_verify():
        for _ in range(5):
            time.sleep(0.06)
        slow_done.set()

    worker = threading.Thread(target=slow_local_grab_verify, daemon=True)
    worker.start()
    page.set_session(session)
    page.show()
    _wait_for_render(qtbot, page)
    assert {str(item.id) for item in page._items} == baseline_ids
    qtbot.waitUntil(slow_done.is_set, timeout=5000)

    dispatch_ms, end_to_end_ms = [], []
    for _ in range(50):
        page.media_list.clearSelection()
        page.media_list.setCurrentRow(0)
        page.media_list.item(0).setSelected(True)
        selected = page._selected_items()
        assert len(selected) == 1
        media_id = str(selected[0].id)
        assert session.decision(media_id) is ReviewDecision.PENDING
        start = time.perf_counter()
        page._set_selected_decision(ReviewDecision.ACCEPTED)
        dispatch_ms.append((time.perf_counter() - start) * 1000)
        _wait_for_render(qtbot, page)
        end_to_end_ms.append((time.perf_counter() - start) * 1000)
        assert session.decision(media_id) is ReviewDecision.ACCEPTED
        assert media_id not in {str(item.id) for item in page._items}
        if len(dispatch_ms) % 10 == 0:
            print(f"completed GUI single decisions: {len(dispatch_ms)}/50", flush=True)

    batch_dispatch, batch_end_to_end = [], []
    for _ in range(20):
        page.media_list.clearSelection()
        page.media_list.setCurrentRow(0)
        batch_ids = {str(candidate.id) for candidate in page._items[:50]}
        for row in range(50):
            page.media_list.item(row).setSelected(True)
        assert len(page._selected_items()) == 50
        batch_start = time.perf_counter()
        page._set_selected_decision(ReviewDecision.REJECTED)
        batch_dispatch.append((time.perf_counter() - batch_start) * 1000)
        _wait_for_render(qtbot, page)
        batch_end_to_end.append((time.perf_counter() - batch_start) * 1000)
        assert all(session.decision(media_id) is ReviewDecision.REJECTED for media_id in batch_ids)
    # The uncached reference itself performs disk/pixel work. Keep that
    # comparison outside the GUI thread so it does not falsify app heartbeat.
    with ThreadPoolExecutor(max_workers=1) as reference_pool:
        reference = reference_pool.submit(_baseline_visible_ids, session.query_snapshot())
        qtbot.waitUntil(reference.done, timeout=30000)
        expected = reference.result()
    assert {str(item.id) for item in page._items} == expected

    first_news_item = None
    root_item = page.news_list.topLevelItem(0)
    for section_index in range(root_item.childCount()):
        section_item = root_item.child(section_index)
        for news_index in range(section_item.childCount()):
            candidate_item = section_item.child(news_index)
            if candidate_item.data(0, Qt.ItemDataRole.UserRole) == ("news", "news-00"):
                first_news_item = candidate_item
                break
    assert first_news_item is not None
    page.news_list.setCurrentItem(first_news_item)
    _wait_for_render(qtbot, page)
    per_news_ids = {str(item.id) for item in page._items}
    assert per_news_ids == {media_id for media_id in expected if media_id.startswith("news-00-")}
    page.news_list.setCurrentItem(root_item)
    _wait_for_render(qtbot, page)
    assert {str(item.id) for item in page._items} == expected

    p95_dispatch = statistics.quantiles(dispatch_ms, n=20)[18]
    p95_end_to_end = statistics.quantiles(end_to_end_ms, n=20)[18]
    batch_dispatch_ms = statistics.quantiles(batch_dispatch, n=20)[18]
    batch_end_to_end_ms = statistics.quantiles(batch_end_to_end, n=20)[18]
    print(f"review GUI benchmark: candidates={len(session.images)} news={NEWS_COUNT} warmup=all-news "
          f"single_dispatch_p95_ms={p95_dispatch:.3f} single_query_render_p95_ms={p95_end_to_end:.3f} "
          f"batch50_dispatch_p95_ms={batch_dispatch_ms:.3f} batch50_query_render_p95_ms={batch_end_to_end_ms:.3f}")

    timer.stop()
    gaps_ms = [(right - left) * 1000 for left, right in zip(heartbeat, heartbeat[1:])]
    max_gap = max(gaps_ms, default=0.0)
    print(f"review GUI responsiveness: heartbeat_interval_ms=20 max_gap_ms={max_gap:.3f} "
          f"initial_rendered_rows={NEWS_COUNT * (CANDIDATES_PER_NEWS - 2)} final_rendered_rows={page._render_cursor}")
    assert len(heartbeat) >= 3
    assert max_gap < 200
    assert p95_dispatch < 100
    assert batch_dispatch_ms < 200

    # Verify source/original URL actions without opening a real browser.
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    page.media_list.clearSelection()
    page.media_list.setCurrentRow(0)
    page.media_list.item(0).setSelected(True)
    page.open_source()
    page.open_original()
    selected_candidate = page._selected()
    assert opened == [selected_candidate.source_url, selected_candidate.image_url]

    # Keep final export small while exercising one older and one newer local
    # source through the unchanged X namespace and image conversion path.
    export_output = tmp_path / "export-sample"
    export_output.mkdir()
    selected_raw = []
    by_id = {str(raw["id"]): raw for raw in json.loads((session.output_dir / "image_index.json").read_text(encoding="utf-8"))["candidates"]}
    for target_id, asset_index in (("old-copy", 0), ("new-copy", 1)):
        raw = dict(by_id[f"news-00-image-{asset_index:02d}"])
        raw["id"] = target_id
        raw["news_id"] = target_id
        raw["local_path"] = str(session.output_dir / f"asset-{asset_index:02d}.jpg")
        raw["image_url"] = f"https://local.invalid/{target_id}.jpg"
        selected_raw.append(raw)
    (export_output / "image_index.json").write_text(json.dumps({"news_items": [
        {"news_id": "old-copy", "sequence": 1, "section": "新作", "title": "Old"},
        {"news_id": "new-copy", "sequence": 2, "section": "新作", "title": "New"}], "candidates": selected_raw}), encoding="utf-8")
    (export_output / "video_index.json").write_text(json.dumps({"news_items": [], "videos": []}), encoding="utf-8")
    (export_output / "review_required.json").write_text("[]", encoding="utf-8")
    export_session = ReviewSession.from_output(export_output, task_root=tmp_path / "export-task")
    export_session.set_decisions(["old-copy", "new-copy"], ReviewDecision.ACCEPTED)
    manifest = export_session.export_final()
    assert manifest["image_count"] == 2
    assert len(manifest["files"]) == 2


