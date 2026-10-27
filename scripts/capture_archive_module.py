"""Capture the archive feature using synthetic media and isolated app data only."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

from galgame_news.archive.models import ArchiveAsset
from galgame_news.desktop.controller import DesktopController
from galgame_news.desktop.theme import apply_theme
from galgame_news.desktop.window import MainWindow
from galgame_news.review import ReviewSession
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend


def pump(app, seconds=0.25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('output/archive-module-validation/ui'))
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for font_path in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf'):
        if Path(font_path).exists():
            identifier = QFontDatabase.addApplicationFont(font_path)
            families = QFontDatabase.applicationFontFamilies(identifier)
            if families:
                app.setFont(QFont(families[0], 10))
                break
    apply_theme('light', app)
    controller = DesktopController(app_data=target / 'app-data', credential_store=CredentialStore(InMemoryCredentialBackend()))
    controller.archive_controller.close(wait=True)
    window = MainWindow(controller, exit_handler=lambda *_: None)
    window.resize(1600, 1050)
    window.show()
    controller.new_task_page.archive_materials_checkbox.setChecked(True)
    window.navigation.setCurrentRow(0)
    pump(app)
    window.grab().save(str(target / '01-抓取选项.png'))

    raw = target / 'demo/raw'
    raw.mkdir(parents=True, exist_ok=True)
    news = {'news_id': 'archive-demo', 'sequence': 1, 'section': '新作', 'title': '《示例游戏》官网更新', 'game_names': ['示例游戏']}
    (raw / 'image_index.json').write_text(json.dumps({'issue_id': '231', 'news_items': [news], 'candidates': []}, ensure_ascii=False), encoding='utf-8')
    (raw / 'video_index.json').write_text('{"videos":[]}', encoding='utf-8')
    image = target / 'demo/preview.png'
    Image.new('RGB', (960, 540), (76, 106, 145)).save(image)
    page = controller.review_page
    page.set_session(ReviewSession.from_output(raw, task_root=raw.parent))
    item_iterator = QTreeWidgetItemIterator(page.news_list)
    while item_iterator.value():
        item = item_iterator.value()
        if item.data(0, Qt.ItemDataRole.UserRole) == ('news', 'archive-demo'):
            page.news_list.setCurrentItem(item)
            break
        item_iterator += 1
    page.tabs.setCurrentIndex(2)
    assets = [ArchiveAsset('demo-image', 'local', 'image', '230', '/230期/图包/示例游戏/1.png', '1.png', game_names=('示例游戏',), news_title='示例游戏 CG 公开', local_path=str(image), size=image.stat().st_size),
              ArchiveAsset('demo-video', 'nextcloud', 'video', '230', '/230期/视频素材/示例游戏/PV.mp4', 'PV.mp4', game_names=('示例游戏',), news_title='历史宣传视频（示例元数据，未下载）', size=1024 * 1024 * 12)]
    page.archive_panel.set_assets('archive-demo', assets, '示例：最近一期第230期 · 1图1视频 · 尚未采用')
    page.show_archive_preview(assets[0], image)
    window.navigation.setCurrentRow(2)
    pump(app)
    window.grab().save(str(target / '02-往期周报标签.png'))
    window.navigation.setCurrentRow(4)
    pump(app)
    window.grab().save(str(target / '03-索引设置.png'))
    assert controller.close(5000)
    window.hide()
    print(json.dumps({'screenshots': [str(p) for p in target.glob('*.png')], 'real_network_used': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
