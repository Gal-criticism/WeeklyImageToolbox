"""PyInstaller entry point for the Windows desktop application."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import sys


def _prepare_writable_app_data() -> None:
    """Keep the packaged launcher usable when LOCALAPPDATA is read-only."""

    if os.name != "nt":
        return

    configured_base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if configured_base:
        configured_root = Path(configured_base) / "GalgameNewsToolbox"
        probe = configured_root / ".write-probe"
        try:
            configured_root.mkdir(parents=True, exist_ok=True)
            probe.touch()
            probe.unlink()
            return
        except OSError:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass

    fallback_base = Path(tempfile.gettempdir())
    fallback_root = fallback_base / "GalgameNewsToolbox"
    probe = fallback_root / ".write-probe"
    try:
        fallback_root.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise RuntimeError("no writable application-data directory is available") from exc
    os.environ["LOCALAPPDATA"] = str(fallback_base)


_prepare_writable_app_data()


def _configure_portable_runtime() -> None:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        os.chdir(root)
        if (root / "browser").is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "browser")


_configure_portable_runtime()

from galgame_news.desktop.app import main


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        import json
        from galgame_news.desktop.app import create_application
        from galgame_news.settings.ffmpeg import discover_ffmpeg
        from galgame_news.settings.browser import check_browser_runtime
        from galgame_news.config import load_config
        from playwright.sync_api import sync_playwright

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app, window = create_application([])
        browser = check_browser_runtime()
        launched = False
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            page = chromium.new_page()
            page.set_content("<html><body>portable offline check</body></html>")
            launched = "portable offline check" in page.content()
            chromium.close()
        from PIL import Image
        from galgame_news.review.session import ReviewSession, ReviewDecision
        from galgame_news.desktop.resources import resource_path
        with tempfile.TemporaryDirectory(prefix="周报 离线验收-") as fixture_name:
            fixture = Path(fixture_name)
            raw = fixture / "raw"
            raw.mkdir()
            sample = raw / "sample.png"
            Image.new("RGB", (960, 640), "#B7544E").save(sample)
            candidate = {"id": "offline-check", "news_id": "check-news", "image_type": "game_cg",
                         "image_url": "https://example.test/sample.png", "source_url": "https://example.test/news",
                         "width": 960, "height": 640, "local_path": str(sample), "downloadable": True,
                         "curation_status": "unselected", "selected": False}
            (raw / "image_index.json").write_text(json.dumps({"issue_id": "check", "candidates": [candidate],
                "news_items": [{"news_id": "check-news", "sequence": 1, "section": "新作", "title": "离线验收"}]}), encoding="utf-8")
            session = ReviewSession.from_output(raw, task_root=fixture)
            session.set_decision("offline-check", ReviewDecision.ACCEPTED)
            session.export_final()
            exported = list((fixture / "final" / "images").rglob("*.png"))
            offline_review = bool(exported) and ReviewSession.from_output(raw, task_root=fixture).decision("offline-check") is ReviewDecision.ACCEPTED
            for path in exported:
                with Image.open(path) as image:
                    image.verify()
        ffmpeg = discover_ffmpeg()
        root = Path(sys.executable).resolve().parent
        report = {"desktop": window.windowTitle() == "周报图片采集工具箱",
                  "five_pages": window.pages.count() == 5,
                  "brand_resources": resource_path("character.png").is_file() and resource_path("toolbox.ico").is_file(),
                  "offline_review_export_restore": offline_review, "browser_available": browser.available,
                  "browser_launch": launched, "ffmpeg": ffmpeg.available,
                  "ffprobe": bool(ffmpeg.ffprobe_path), "config": bool(load_config()),
                  "no_private_key_seed": not (root / ".private-socialdata-key").exists()}
        (root / "package_self_check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()
        raise SystemExit(0 if all(report.values()) else 1)
    raise SystemExit(main())
