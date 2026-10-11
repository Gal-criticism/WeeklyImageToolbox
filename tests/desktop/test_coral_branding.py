from pathlib import Path
import sys
import tomllib

import pytest
from PIL import Image
from PySide6.QtGui import QPalette

from galgame_news.desktop import resources
from galgame_news.desktop.new_task_page import NewTaskPage
from galgame_news.desktop.theme import apply_theme, COLORS, stylesheet
from galgame_news.desktop.ui import icon
from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.window import MainWindow
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_coral_tokens_and_styles(qapp, theme):
    apply_theme(theme, qapp)
    for role, name in [(QPalette.ColorRole.Window, "window"), (QPalette.ColorRole.Base, "card"),
                       (QPalette.ColorRole.Highlight, "accent"), (QPalette.ColorRole.Text, "text")]:
        assert qapp.palette().color(role).name().upper() == COLORS[theme][name].upper()
    assert "@" not in qapp.styleSheet()
    for purple in ("#9680d5", "#7860b5", "#a58cd7"):
        assert purple not in stylesheet(theme == "dark")
    apply_theme("light", qapp)


def test_local_artwork_and_icon_formats(qapp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with Image.open(resources.resource_path("character.png")) as art:
        assert art.mode == "RGBA"
        assert art.getchannel("A").getextrema() == (0, 255)
    with Image.open(resources.resource_path("toolbox.ico")) as artwork:
        assert artwork.ico.sizes() == {(size, size) for size in (16, 24, 32, 48, 64, 128, 256)}
    assert not resources.configure_branding(qapp).isNull()


def test_frozen_resources_use_bundle_not_cwd(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    root = bundle / "galgame_news/desktop/assets"
    root.mkdir(parents=True)
    (root / "character.png").write_bytes(resources.resource_path("character.png").read_bytes())
    monkeypatch.setattr(resources, "__file__", str(tmp_path / "missing/package/resources.py"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert resources.resource_path("character.png") == root / "character.png"
    with pytest.raises(ValueError):
        resources.resource_path("../secret.png")


def test_art_scales_and_browse_icon_refreshes(qtbot, qapp):
    apply_theme("light", qapp)
    page = NewTaskPage()
    qtbot.addWidget(page)
    page.show()
    character = page.findChild(resources.CharacterArt)
    assert character is not None
    size = character.pixmap().deviceIndependentSize()
    assert size.width() <= character.width() and size.height() <= character.height()
    before = page.input_browse_button.icon().pixmap(24, 24).toImage()
    apply_theme("dark", qapp)
    qapp.processEvents()
    assert before != page.input_browse_button.icon().pixmap(24, 24).toImage()
    assert page.input_browse_button.icon().pixmap(24, 24).toImage() == icon("file").pixmap(24, 24).toImage()
    assert page.socialdata_checkbox.isChecked() is False
    apply_theme("light", qapp)


def test_packaging_declares_art_and_executable_icon():
    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "assets/*.png" in manifest["tool"]["setuptools"]["package-data"]["galgame_news.desktop"]
    spec = (root / "packaging/GalgameNewsToolbox.spec").read_text(encoding="utf-8")
    assert '"galgame_news/desktop/assets"' in spec
    assert 'icon=str(SRC_ROOT' in spec


@pytest.mark.parametrize("width,height", [(1100, 720), (1280, 820)])
def test_window_navigation_tracks_theme_without_review_overlay(qtbot, qapp, tmp_path, width, height):
    controller = DesktopController(app_data=tmp_path, credential_store=CredentialStore(InMemoryCredentialBackend()))
    window = MainWindow(controller)
    qtbot.addWidget(window)
    window.resize(width, height)
    window.show()
    assert window.review_page.findChild(resources.CharacterArt) is None
    for theme in ("light", "dark"):
        apply_theme(theme, qapp)
        qapp.processEvents()
        for index, symbol in enumerate(("new", "progress", "review", "history", "settings")):
            window.navigation.setCurrentRow(index)
            qapp.processEvents()
            assert window.pages.currentIndex() == index
            assert window.navigation.item(index).icon().pixmap(24, 24).toImage() == icon(symbol).pixmap(24, 24).toImage()
        assert window.settings_page.save_button.isVisible()
    assert controller.close(timeout_ms=5000)
    controller.task_store.close()
    apply_theme("light", qapp)
