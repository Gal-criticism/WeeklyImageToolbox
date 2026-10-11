from __future__ import annotations

from PySide6.QtWidgets import QApplication

from galgame_news.desktop.settings_page import SettingsPage
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend, SettingsStore


def test_settings_page_exposes_only_socialdata_and_saves_secret_without_refilling(tmp_path):
    app = QApplication.instance() or QApplication([])
    backend = InMemoryCredentialBackend()
    credentials = CredentialStore(backend)
    credentials.set("brave_api_key", "keep-brave")
    credentials.set("x_bearer_token", "keep-x")
    credentials.set("twitterapi_api_key", "keep-twitterapi")
    page = SettingsPage(SettingsStore(app_data=tmp_path / "app"), credentials)

    assert set(page._credential_fields) == {"socialdata_api_key"}
    assert page.socialdata_edit.echoMode() == page.socialdata_edit.EchoMode.Password
    assert page.socialdata_edit.text() == ""
    page.socialdata_edit.setText("new-secret")
    page.save()
    assert credentials.get_socialdata_api_key() == "new-secret"
    assert page.socialdata_edit.text() == ""
    assert page.credential_status["socialdata_api_key"].text() == "已配置"
    page.socialdata_edit.setText("")
    page.save()
    assert credentials.get_socialdata_api_key() == "new-secret"
    assert credentials.get("brave_api_key") == "keep-brave"
    assert credentials.get("x_bearer_token") == "keep-x"
    assert credentials.get("twitterapi_api_key") == "keep-twitterapi"
    page.close()
    app.processEvents()


def test_secret_backend_failure_is_not_displayed_or_logged(qtbot, tmp_path, caplog):
    class FailingBackend(InMemoryCredentialBackend):
        def set_password(self, service_name, username, password):
            raise RuntimeError("backend error containing " + password)

    page = SettingsPage(SettingsStore(app_data=tmp_path), CredentialStore(FailingBackend()))
    qtbot.addWidget(page)
    page.socialdata_edit.setText("test-only-sensitive-value")
    page._save_from_ui()
    assert "保存失败" in page.save_status.text()
    assert "test-only-sensitive-value" not in page.save_status.text() + caplog.text
    assert page.socialdata_edit.text() == "test-only-sensitive-value"
    assert "test-only-sensitive-value" not in page.settings_store.path.read_text(encoding="utf-8")
