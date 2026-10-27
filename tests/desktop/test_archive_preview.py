from PIL import Image

from galgame_news.archive.models import ArchiveAsset
from galgame_news.desktop.review_page import ReviewPage


def test_archive_preview_uses_existing_right_column_without_selecting_candidate(qtbot, tmp_path):
    page = ReviewPage()
    qtbot.addWidget(page)
    page.tabs.setCurrentIndex(2)
    path = tmp_path / "preview.png"
    Image.new("RGB", (100, 60), "blue").save(path)
    asset = ArchiveAsset("a", "local", "image", "230", "/archive/Game/a.png", "a.png", game_names=("Game",), local_path=str(path))
    page.show_archive_preview(asset, path)
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    assert "230" in page.metadata.text()
    assert "Game" in page.metadata.text()
    assert page._selected() is None
    with qtbot.waitSignal(page.archive_panel.preview_requested) as result:
        page.open_original()
    assert result.args == [asset]


def test_archive_video_without_cache_requests_only_explicit_preview(qtbot):
    page = ReviewPage()
    qtbot.addWidget(page)
    page.tabs.setCurrentIndex(2)
    asset = ArchiveAsset("v", "nextcloud", "video", "230", "/archive/Game/op.mp4", "op.mp4")
    page.show_archive_preview(asset)
    assert "230" in page.metadata.text()
    with qtbot.waitSignal(page.archive_panel.preview_requested) as result:
        page.play_video()
    assert result.args == [asset]


def test_archive_video_thumbnail_is_not_a_playable_original(qtbot, tmp_path):
    page = ReviewPage()
    qtbot.addWidget(page)
    page.tabs.setCurrentIndex(2)
    thumb = tmp_path / "thumb.jpg"
    Image.new("RGB", (120, 60), "blue").save(thumb)
    asset = ArchiveAsset("v", "nextcloud", "video", "230", "/archive/Game/op.mp4", "op.mp4")
    page.show_archive_preview(asset, thumb)
    qtbot.waitUntil(lambda: not page._current_pixmap.isNull())
    assert page._archive_preview_path is None
    with qtbot.waitSignal(page.archive_panel.preview_requested) as result:
        page.play_video()
    assert result.args == [asset]
