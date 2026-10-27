from __future__ import annotations

import json

from galgame_news.settings import AppSettings, ArchiveSettings, SettingsStore


def test_archive_settings_have_safe_defaults_and_round_trip(tmp_path):
    defaults = AppSettings().archive
    assert defaults.server_url == ""
    assert defaults.username == ""
    assert defaults.root_path == ""
    assert defaults.local_roots == []
    assert defaults.range_days == 365

    archive = ArchiveSettings(
        server_url="https://cloud.example",
        username="alice",
        root_path="/weekly",
        local_roots=[str(tmp_path / "weekly")],
        range_days=120,
    )
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(archive=archive))
    loaded = store.load()
    assert loaded.archive == archive


def test_archive_password_stays_out_of_settings_json(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication
    from galgame_news.desktop.settings_page import SettingsPage
    from galgame_news.settings import CredentialStore, InMemoryCredentialBackend

    QApplication.instance() or QApplication([])
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    credentials = CredentialStore(InMemoryCredentialBackend())
    page = SettingsPage(settings_store=store, credential_store=credentials)
    qtbot.addWidget(page)
    page.archive_password_edit.setText("archive-secret")
    page.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "archive-secret" not in path.read_text(encoding="utf-8")
    assert credentials.get("archive_app_password") == "archive-secret"
    assert "archive_app_password" not in payload


def test_archive_settings_buttons_emit_settings_and_status_is_controller_owned(qtbot):
    from galgame_news.desktop.settings_page import SettingsPage
    from galgame_news.settings import CredentialStore, InMemoryCredentialBackend

    page = SettingsPage(credential_store=CredentialStore(InMemoryCredentialBackend()))
    qtbot.addWidget(page)
    page.archive_server_edit.setText("https://cloud.example")
    page.archive_username_edit.setText("alice")
    page.archive_local_roots_edit.setText("C:/weekly; D:/archive")
    connection_requests = []
    index_requests = []
    page.archive_connection_test_requested.connect(connection_requests.append)
    page.archive_index_update_requested.connect(index_requests.append)

    page.archive_test_button.click()
    page.archive_index_button.click()
    page.set_archive_status("连接成功")

    assert connection_requests[0].server_url == "https://cloud.example"
    assert connection_requests[0].username == "alice"
    assert index_requests[0].local_roots == ["C:/weekly", "D:/archive"]
    assert page.archive_status.text() == "连接成功"


def test_archive_connection_request_uses_credential_store_without_emitting_password(qtbot):
    from galgame_news.desktop.settings_page import SettingsPage
    from galgame_news.settings import CredentialStore, InMemoryCredentialBackend

    credentials = CredentialStore(InMemoryCredentialBackend())
    page = SettingsPage(credential_store=credentials)
    qtbot.addWidget(page)
    page.archive_password_edit.setText("archive-secret")
    requests = []
    page.archive_connection_test_requested.connect(requests.append)

    page.archive_test_button.click()

    assert credentials.get("archive_app_password") == "archive-secret"
    assert "archive-secret" not in repr(requests[0])
