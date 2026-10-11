"""Offline UI evidence: five pages, two themes and three DPI scales.

Use QT_QPA_PLATFORM=offscreen and QT_SCALE_FACTOR=1/1.5/2 in separate runs.
All catalogs, credentials and review state belong to the evidence folder.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.theme import apply_theme
from galgame_news.desktop.window import MainWindow
from galgame_news.pipeline import ProgressEvent
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend


ROOT = Path(__file__).resolve().parents[1]


def make_fixture(destination: Path) -> Path:
    raw = destination / "demo" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    source = ROOT / "output/async_benchmark_20261002/real/async/task"
    original = json.loads((source / "image_index.json").read_text(encoding="utf-8")) if source.is_dir() else {}
    originals = [c for c in original.get("candidates", []) if c.get("image_type") == "game_cg" and c.get("downloadable")]
    candidates = []
    for index in range(12):
        candidate = dict(originals[index % len(originals)]) if originals else {
            "image_type": "game_cg", "image_url": f"https://example.test/cg{index}.png",
            "source_url": "https://example.test/graphic", "width": 800, "height": 600,
        }
        local = Path(candidate.get("local_path") or "")
        if not local.is_absolute():
            local = source / local
        target = raw / f"demo-{index}.png"
        if local.is_file():
            # Only copy; preserve actual encoded extension and historical files.
            target = target.with_suffix(local.suffix)
            shutil.copy2(local, target)
        else:
            Image.new("RGB", (800, 600), (160 + index * 3, 155, 205)).save(target)
        candidate.update(id=f"demo-{index}", news_id="demo-news-1", local_path=str(target),
                         selected=index < 6, downloadable=True, curation_status="selected" if index < 6 else "unselected")
        if index in (6, 7):
            candidate.update(duplicate_of="demo-0", selection_reasons=["duplicate_of_better_candidate"])
        if index == 0:
            candidate.update(source_type="official_x", source_url="https://x.com/example/status/123")
        candidates.append(candidate)
    candidates.extend([
        {"id": "demo-failed", "news_id": "demo-news-2", "image_url": "https://example.test/missing.png",
         "source_url": "https://example.test/", "downloadable": False, "curation_status": "unselected",
         "selection_reasons": ["download_failed"]},
        {"id": "demo-invalid", "news_id": "demo-news-2", "image_url": "https://example.test/broken.png",
         "source_url": "https://example.test/", "downloadable": False, "curation_status": "invalid",
         "signals": {"invalid_reason": "fixture decoder failure"}},
    ])
    news = [{"news_id": "demo-news-1", "sequence": 1, "section": "新作",
             "title": "《花鐘カナデ＊グラム》官网更新与游戏 CG 公开"},
            {"news_id": "demo-news-2", "sequence": 2, "section": "新作",
             "title": "长标题示例：下载失败与无效图片仍保留来源，方便手动复核和重新抓取"},
            {"news_id": "demo-news-3", "sequence": 3, "section": "汉化", "title": "暂无可用配图的新闻"}]
    (raw / "image_index.json").write_text(json.dumps({"issue_id": "226", "news_items": news, "candidates": candidates}, ensure_ascii=False), encoding="utf-8")
    (raw / "failed_items.json").write_text(json.dumps([{"candidate_id": "demo-failed"}]), encoding="utf-8")
    return raw


def pump(app, seconds=.35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)


def select_news(review, news_id):
    root = review.news_list.topLevelItem(0)
    if news_id is None:
        review.news_list.setCurrentItem(root)
        return
    for group_index in range(root.childCount()):
        group = root.child(group_index)
        for index in range(group.childCount()):
            item = group.child(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == ("news", news_id):
                review.news_list.setCurrentItem(item)
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "output/toolbox_ui_20261002")
    parser.add_argument("--scale", default="100")
    args = parser.parse_args()
    destination = args.output.resolve()
    shots = destination / "screenshots" / f"scale-{args.scale}"
    shots.mkdir(parents=True, exist_ok=True)
    raw = make_fixture(destination)
    app = QApplication.instance() or QApplication([])
    # Never construct a real keyring backend or pipeline runner in this script.
    controller = DesktopController(app_data=destination / f"demo/catalog-{args.scale}",
                                   credential_store=CredentialStore(InMemoryCredentialBackend()))
    window = MainWindow(controller)
    controller.new_task_page.set_input_path(ROOT / "input/226_副本.docx")
    controller.new_task_page.output_edit.setText(str(ROOT / "output"))
    controller.new_task_page.no_videos_checkbox.setChecked(True)
    controller.import_output(raw)
    controller.task_store.create_task("261", ROOT / "input/261.docx", task_root=destination / "demo/task-261")
    controller.history_page.refresh()
    progress = controller.progress_page
    progress.set_running(True)
    for kind, message in (("parse_completed", "DOCX parsed"), ("news_started", "《花鐘カナデ＊グラム》官网 CG 更新"),
                          ("resolve_completed", "4 sources"), ("collect_completed", "collection complete"),
                          ("download_failed", "fixture: CDN connection timeout"), ("news_completed", "news completed"),
                          ("curate_started", "curating candidates")):
        progress.handle_event(ProgressEvent(kind, "demo", "226", news_index=1, total_news=3, message=message))
    controller.review_page.tabs.setCurrentIndex(0)
    window.show()
    report = []
    for theme in ("light", "dark"):
        apply_theme(theme, app)
        pump(app)
        for index, name in enumerate(("new-task", "progress", "review", "history", "settings")):
            window.navigation.setCurrentRow(index)
            pump(app, .7 if index == 2 else .2)
            path = shots / f"{theme}-{name}.png"
            window.grab().save(str(path))
            page = window.pages.currentWidget()
            report.append({"theme": theme, "page": name, "scale": args.scale,
                           "logical_window": [window.width(), window.height()], "device_pixel_ratio": window.devicePixelRatioF(),
                           "page_minimum_hint": [page.minimumSizeHint().width(), page.minimumSizeHint().height()],
                           "image": str(path.relative_to(destination))})
        review = controller.review_page
        window.navigation.setCurrentRow(2)
        review.tabs.setCurrentIndex(1)
        select_news(review, "demo-news-2")
        pump(app, .2)
        window.grab().save(str(shots / f"{theme}-review-failed.png"))
        review.media_list.setCurrentRow(1)
        pump(app, .2)
        window.grab().save(str(shots / f"{theme}-review-invalid.png"))
        select_news(review, "demo-news-3")
        pump(app, .2)
        window.grab().save(str(shots / f"{theme}-review-empty.png"))
        select_news(review, None)
        review.tabs.setCurrentIndex(0)
        review.raw_toggle.setChecked(True)
        window.resize(1100, 720)
        pump(app, .4)
        window.grab().save(str(shots / f"{theme}-review-details-minimum.png"))
        review.raw_toggle.setChecked(False)
        # Minimum-size evidence specifically for the dense review panel.
        window.resize(1100, 720)
        window.navigation.setCurrentRow(2)
        pump(app, .4)
        window.grab().save(str(shots / f"{theme}-review-minimum.png"))
        window.navigation.setCurrentRow(1)
        progress.technical_group.setChecked(True)
        pump(app, .2)
        window.grab().save(str(shots / f"{theme}-progress-details-minimum.png"))
        progress.technical_group.setChecked(False)
        window.resize(1280, 820)
    (shots / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    window.close()
    controller.task_store.close()
    pump(app, .1)


if __name__ == "__main__":
    main()
