from types import SimpleNamespace

from galgame_news.desktop.archive_panel import ArchivePanel
from galgame_news.desktop.new_task_page import NewTaskPage
from galgame_news.desktop.review_page import ReviewPage


def test_archive_materials_option_defaults_off_and_is_submitted(qtbot, tmp_path):
    page = NewTaskPage(default_output=tmp_path / "output")
    qtbot.addWidget(page)
    page.set_input_path(tmp_path / "261.docx")
    payloads = []
    page.start_requested.connect(payloads.append)

    assert page.archive_materials_checkbox.isChecked() is False
    page.archive_materials_checkbox.setChecked(True)
    page.request_start()

    assert payloads[0]["use_archive_materials"] is True


def test_archive_range_visibility_defaults_and_payload(qtbot, tmp_path):
    page = NewTaskPage(default_output=tmp_path / "output")
    qtbot.addWidget(page)
    page.set_input_path(tmp_path / "261.docx")
    payloads = []
    page.start_requested.connect(payloads.append)

    assert page.archive_range_container.isHidden()
    assert page.archive_range_combo.currentData() == "year"
    page.archive_materials_checkbox.setChecked(True)
    assert not page.archive_range_container.isHidden()
    assert "最近出现的两期全部匹配图片与视频" in page.archive_range_hint.text()
    page.request_start()
    assert payloads[-1]["archive_range"] == "year"

    page.archive_range_combo.setCurrentIndex(page.archive_range_combo.findData("all"))
    page.request_start()
    assert payloads[-1]["archive_range"] == "all"

    page.archive_materials_checkbox.setChecked(False)
    assert page.archive_range_container.isHidden()
    page.archive_materials_checkbox.setChecked(True)
    assert not page.archive_range_container.isHidden()
    assert page.archive_range_combo.currentData() == "all"


def test_review_archive_tab_is_third_and_has_separate_panel(qtbot):
    page = ReviewPage()
    qtbot.addWidget(page)

    assert page.tabs.tabText(2) == "往期周报"
    assert page.archive_panel is not None
    assert page.tabs.tabText(3) == "已排除"
    assert page.tabs.tabText(4) == "视频"


def test_archive_tab_never_uses_ordinary_review_candidates():
    class Snapshot:
        def accepted_images(self, _news_id):
            return ["accepted"]

        def pending_images(self, _news_id):
            return ["pending"]

        def rejected_images(self, _news_id):
            return ["rejected"]

        def accepted_videos(self, _news_id):
            return ["video"]

        def pending_videos(self, _news_id):
            return ["pending-video"]

    assert ReviewPage._query_snapshot_candidates(Snapshot(), None, None, 2) == []


def test_archive_panel_displays_all_assets_and_emits_query_and_selection_signals(qtbot):
    panel = ArchivePanel()
    qtbot.addWidget(panel)
    assets = [
        SimpleNamespace(asset_id="i1", kind="image", name="one.png"),
        SimpleNamespace(asset_id="i2", kind="image", name="two.png"),
        SimpleNamespace(asset_id="v1", kind="video", name="one.mp4"),
        SimpleNamespace(asset_id="v2", kind="video", name="two.mp4"),
    ]
    requested = []
    panel.query_requested.connect(requested.append)

    panel.set_assets("news-1", assets, "历史素材")
    assert panel.asset_count == 4
    assert panel.asset_kinds == ["image", "image", "video", "video"]
    panel.keyword_edit.setText("作品名")
    panel.search_button.click()
    assert requested == ["作品名"]
    assert panel.current_news_id == "news-1"


def test_archive_asset_card_shows_provenance_and_uncertainty(qtbot, tmp_path):
    panel = ArchivePanel()
    qtbot.addWidget(panel)
    asset = SimpleNamespace(
        asset_id="i1",
        kind="image",
        name="cover.png",
        issue_number="226",
        issue_date="2025-03-01",
        source_kind="local",
        size=2048,
        news_title="作品公布发售日",
        version=None,
        date_suspect=True,
        local_path=None,
    )

    panel.set_assets("news-1", [asset])

    text = panel.list_widget.item(0).text()
    for expected in ("226", "local", "2 KB", "作品公布发售日", "2025-03-01", "日期待核", "版本待核"):
        assert expected in text


def test_archive_panel_uses_only_existing_local_thumbnail(qtbot, tmp_path):
    from PySide6.QtGui import QImage, QColor

    image_path = tmp_path / "thumbnail.png"
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(image_path))
    panel = ArchivePanel()
    qtbot.addWidget(panel)
    asset = SimpleNamespace(
        asset_id="i1", kind="image", name="thumbnail.png", local_path=str(image_path),
    )

    panel.set_assets("news-1", [asset])

    assert not panel.list_widget.item(0).icon().isNull()


def test_archive_provenance_summary_is_visible_metadata():
    candidate = SimpleNamespace(archive_provenance={
        "issue_number": "225",
        "issue_date": "2025-02-01",
        "path": "Weekly/225/game/image.jpg",
        "original_source_url": "https://example.test/original",
    })

    summary = ReviewPage._archive_provenance_summary(candidate)

    assert "来源期号：225" in summary
    assert "归档路径：Weekly/225/game/image.jpg" in summary
    assert "原始来源：https://example.test/original" in summary
