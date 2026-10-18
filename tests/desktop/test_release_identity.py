from galgame_news.desktop.app import create_application
from galgame_news.settings.model import APP_DATA_NAME
from pathlib import Path


def test_public_name_and_legacy_storage(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    app, window = create_application([])
    qtbot.addWidget(window)
    assert window.windowTitle() == "周报图片采集工具箱"
    assert app.applicationDisplayName() == "周报图片采集工具箱"
    assert app.applicationVersion() == "1.1.0"
    assert APP_DATA_NAME == "GalgameNewsToolbox"
    window.controller.close(timeout_ms=5000)


def test_public_launcher_does_not_import_private_seed():
    source = (Path(__file__).resolve().parents[2] / "packaging/desktop_entry.py").read_text(encoding="utf-8")
    assert "def _import_private_test_key" not in source
    assert "store.set(" not in source
    assert '"no_private_key_seed": not' in source


def test_late_thread_callback_does_not_refresh_closed_catalog(qtbot, tmp_path):
    from galgame_news.desktop.controller import DesktopController
    from unittest.mock import patch
    controller = DesktopController(app_data=tmp_path)
    controller._closing = True
    controller._worker = object()
    with patch.object(controller.history_page, "refresh", side_effect=AssertionError("catalog already closed")):
        controller._on_thread_finished()
    assert controller._worker is None
    assert controller.close(5000)
