"""Create sanitized, offline screenshots for the public documentation."""
from __future__ import annotations

import argparse
import gc
import json
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PySide6.QtWidgets import QApplication

from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.theme import apply_theme
from galgame_news.desktop.window import MainWindow
from galgame_news.pipeline import ProgressEvent
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend

ROOT = Path(__file__).resolve().parents[1]


def draw_demo_image(path: Path, variant: int) -> None:
    """Draw a copyright-free landscape card used only by this demo fixture."""
    palettes = [((30, 52, 82), (95, 145, 160)), ((56, 51, 90), (180, 120, 140)),
                ((34, 86, 88), (153, 174, 123)), ((82, 58, 72), (197, 151, 113))]
    top, bottom = palettes[variant % len(palettes)]
    image = Image.new("RGB", (960, 640))
    draw = ImageDraw.Draw(image)
    for y in range(image.height):
        f = y / image.height
        color = tuple(round(top[i] * (1 - f) + bottom[i] * f) for i in range(3))
        draw.line((0, y, image.width, y), fill=color)
    draw.ellipse((685, 82, 815, 212), fill=(247, 218, 160))
    draw.polygon([(0, 432), (160, 260 + variant * 8), (320, 450), (510, 230),
                  (720, 445), (860, 285), (960, 400), (960, 640), (0, 640)], fill=(34, 62, 77))
    draw.polygon([(0, 520), (240, 390), (430, 550), (675, 365), (960, 535),
                  (960, 640), (0, 640)], fill=(41, 90, 91))
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 34) if font_path.exists() else ImageFont.load_default()
    draw.rounded_rectangle((34, 34, 380, 95), radius=18, fill=(12, 20, 34))
    draw.text((55, 45), f"示意素材 · 场景 {variant + 1}", font=font, fill=(245, 242, 232))
    image.save(path)


def pump(app: QApplication, seconds: float = .3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(.01)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/screenshots")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    apply_theme(args.theme, app)
    with tempfile.TemporaryDirectory(prefix="galgame-public-docs-", dir=args.temp_root) as temp_name:
        temp = Path(temp_name)
        raw = temp / "fixture" / "raw"
        raw.mkdir(parents=True)
        candidates = []
        for i in range(6):
            image_path = raw / f"scene-{i}.png"
            draw_demo_image(image_path, i)
            candidates.append({
                "id": f"demo-{i}", "news_id": "demo-news-1", "image_type": "game_cg",
                "image_url": f"https://example.test/images/scene-{i}.png",
                "source_url": "https://example.test/news/demo", "width": 960, "height": 640,
                "local_path": str(image_path), "downloadable": True,
                "selected": i < 3, "curation_status": "selected" if i < 3 else "unselected",
                "selection_reasons": ["demo_fixture"],
            })
        news = [{"news_id": "demo-news-1", "sequence": 1, "section": "新作",
                 "title": "春日主题新作公开：官网更新角色与配图"},
                {"news_id": "demo-news-2", "sequence": 2, "section": "汉化",
                 "title": "本期汉化动态与发行信息整理"}]
        (raw / "image_index.json").write_text(json.dumps({"issue_id": "demo-01", "news_items": news,
                                                            "candidates": candidates}, ensure_ascii=False), encoding="utf-8")
        (raw / "failed_items.json").write_text("[]", encoding="utf-8")

        controller = DesktopController(app_data=temp / "app-data",
                                       credential_store=CredentialStore(InMemoryCredentialBackend()))
        window = None
        try:
            window = MainWindow(controller)
            new = controller.new_task_page
            new.input_edit.setText("input/demo.docx")
            new.issue_edit.setText("demo-01")
            new.drop_hint.setText("已选择：demo.docx")
            new.output_edit.setText("output")
            new.offline_checkbox.setChecked(True)
            # Let the real form's scroll area use the full page height so all
            # options, including SocialData, are visible in the public shot.
            # The form already gives its scroll area the remaining height.
            controller.import_output(raw)
            if (review_session := controller.review_page.session) is not None:
                # The review footer normally shows real on-disk paths. For this
                # documentation capture, replace only those display values.
                review_session.output_dir = Path("output/raw")
                review_session.task_root = Path("output/task")
                controller.review_page.export_status.setText(
                    "来源目录：output/raw\n导出目录：output/task/final/images"
                )
            controller.task_store.create_task("demo-02", "input/previous-demo.docx",
                                              task_root=temp / "demo-task")
            latest = controller.task_store.create_task("demo-01", "input/demo.docx",
                                                        task_root=temp / "active-demo-task")
            controller.task_store.set_active(latest.task_id)
            controller.history_page.refresh()
            for row, label in enumerate(("第 demo-01 期 · 示例结果已导入", "第 demo-01 期 · 示例任务", "第 demo-02 期 · 示例任务")):
                if row < controller.history_page.task_list.count():
                    controller.history_page.task_list.item(row).setText(label)
            controller.settings_page.output_edit.setText("output")
            controller.settings_page.theme_combo.setCurrentIndex(
                controller.settings_page.theme_combo.findData(args.theme)
            )
            progress = controller.progress_page
            progress.set_running(True)
            for kind, message in (("parse_completed", "文档解析完成"), ("news_started", "春日主题新作公开：官网更新角色与配图"),
                              ("resolve_completed", "已找到 4 个公开来源"), ("collect_completed", "配图采集完成"),
                              ("download_failed", "1 张图片暂不可用"), ("news_completed", "已处理 1 / 2 条新闻")):
                progress.handle_event(ProgressEvent(kind, "demo-task", "demo-01", news_index=1,
                                                    total_news=2, message=message))
            review = controller.review_page
            review.tabs.setCurrentIndex(0)
            review.news_list.setCurrentItem(review.news_list.topLevelItem(0))
            review.media_list.setCurrentRow(0)
            review.retry_url_edit.setText("https://example.test/news/demo")

            window.resize(1280, 960)
            window.show()
            pump(app)
            page_files = [(0, "new-task.png"), (1, "progress.png"), (2, "review.png"),
                      (3, "history.png"), (4, "settings.png")]
            for index, name in page_files:
                window.navigation.setCurrentRow(index)
                if name == "review.png":
                    review.tabs.setCurrentIndex(0)
                    review.news_list.setCurrentItem(review.news_list.topLevelItem(0))
                    review.media_list.setCurrentRow(0)
                pump(app, .6 if name == "review.png" else .25)
                window.grab().save(str(destination / name))

            # Five labeled panels, including the main workflow and settings.
            images = [Image.open(destination / filename).convert("RGB") for filename in
                  ("new-task.png", "progress.png", "review.png", "history.png", "settings.png")]
            panel_w, panel_h, label_h, gap = 600, 386, 54, 20
            overview = Image.new("RGB", (gap + 3 * (panel_w + gap), 2 * (panel_h + label_h + gap) + gap), "#242832" if args.theme == "dark" else "#FFF7F3")
            draw = ImageDraw.Draw(overview)
            font_path = Path("C:/Windows/Fonts/msyh.ttc")
            font = ImageFont.truetype(str(font_path), 26) if font_path.exists() else ImageFont.load_default()
            for i, (image, title) in enumerate(zip(images, ("新建任务", "抓取进度", "图片审核", "历史任务", "设置"))):
                x = gap + (i % 3) * (panel_w + gap)
                y = gap + (i // 3) * (panel_h + label_h + gap)
                draw.text((x + 8, y), title, font=font, fill="#F5EFEA" if args.theme == "dark" else "#303747")
                thumb = image.resize((panel_w, panel_h), Image.Resampling.LANCZOS)
                overview.paste(thumb, (x, y + label_h))
            overview.save(destination / "overview.png")

        finally:
            controller.close(timeout_ms=5000)
            if window is not None:
                window.close()
            controller.task_store.close()
            pump(app, .1)
            gc.collect()  # release short-lived SQLite context connections


if __name__ == "__main__":
    main()
