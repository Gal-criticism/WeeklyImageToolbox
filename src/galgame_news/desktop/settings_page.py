"""Settings page for non-secret settings, credentials, theme, and FFmpeg."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QProcess, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QScrollArea,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from .ui import page_header

from ..settings import (
    AppSettings,
    CredentialStore,
    InMemoryCredentialBackend,
    SettingsStore,
    browser_install_commands,
    check_browser_runtime,
    discover_ffmpeg,
)


class SettingsPage(QWidget):
    theme_changed = Signal(str)
    ffmpeg_checked = Signal(object)

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        credential_store: CredentialStore | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.settings_store = settings_store or SettingsStore()
        if credential_store is None:
            try:
                credential_store = CredentialStore()
            except RuntimeError:
                credential_store = CredentialStore(InMemoryCredentialBackend())
        self.credential_store = credential_store
        self._settings = self._load_settings()
        self._browser_install_steps: tuple[tuple[str, tuple[str, ...]], ...] = ()
        self._browser_install_index = 0
        self._browser_process = QProcess(self)
        self._browser_process.finished.connect(self._browser_install_finished)
        self._browser_process.errorOccurred.connect(self._browser_install_error)

        self.output_edit = QLineEdit(str(self._settings.default_output_path))
        self.theme_combo = QComboBox()
        for caption, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            self.theme_combo.addItem(caption, value)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self._settings.theme)))
        self.socialdata_edit = QLineEdit()
        self.socialdata_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save_from_ui)
        self.ffmpeg_button = QPushButton("检查视频工具")
        self.ffmpeg_button.clicked.connect(self.diagnose_ffmpeg)
        self.ffmpeg_status = QLabel("尚未检查")
        self.ffmpeg_status.setWordWrap(True)
        self.save_status = QLabel()
        self.credential_status: dict[str, QLabel] = {}

        appearance = QGroupBox("外观")
        QFormLayout(appearance).addRow("界面主题", self.theme_combo)
        destination = QGroupBox("输出目录")
        destination_row = QHBoxLayout(destination)
        destination_row.addWidget(self.output_edit, 1)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.choose_output)
        destination_row.addWidget(browse)
        secrets = QGroupBox("X 图片与视频 API（SocialData）")
        secret_form = QFormLayout(secrets)
        secret_form.setVerticalSpacing(12)
        self._credential_fields = {"socialdata_api_key": self.socialdata_edit}
        for name, caption in (("socialdata_api_key", "SocialData X 密钥"),):
            edit = self._credential_fields[name]
            edit.setPlaceholderText("留空保留已有密钥")
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(edit, 1)
            state = QLabel()
            state.setObjectName("muted")
            self.credential_status[name] = state
            row.addWidget(state)
            secret_form.addRow(caption, row)
        hint = QLabel('密钥仅存入 Windows 凭据库，已有内容不回填；留空会保留已有密钥。默认关闭，需在新建任务时单独启用。<a href="https://socialdata.tools/app/api-keys">申请 SocialData API 密钥</a>')
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setOpenExternalLinks(True)
        secret_form.addRow(hint)
        self._refresh_credential_status()
        ffmpeg = QGroupBox("视频工具")
        ffmpeg_layout = QVBoxLayout(ffmpeg)
        check_row = QHBoxLayout()
        check_row.addWidget(self.ffmpeg_status, 1)
        check_row.addWidget(self.ffmpeg_button)
        ffmpeg_layout.addLayout(check_row)
        self.ffmpeg_details = QGroupBox("技术详情")
        self.ffmpeg_details.setCheckable(True)
        self.ffmpeg_details.setChecked(False)
        self.ffmpeg_diagnostic = QLabel()
        self.ffmpeg_diagnostic.setWordWrap(True)
        QVBoxLayout(self.ffmpeg_details).addWidget(self.ffmpeg_diagnostic)
        self.ffmpeg_diagnostic.hide()
        self.ffmpeg_details.toggled.connect(self.ffmpeg_diagnostic.setVisible)
        ffmpeg_layout.addWidget(self.ffmpeg_details)
        browser = QGroupBox("动态网页浏览器")
        browser_layout = QVBoxLayout(browser)
        self.browser_enabled_checkbox = QCheckBox("启用动态页面浏览器增强")
        self.browser_enabled_checkbox.setChecked(self._settings.browser_enabled)
        self.browser_enabled_checkbox.setObjectName("browserEnabled")
        browser_layout.addWidget(self.browser_enabled_checkbox)
        browser_hint = QLabel("只访问公开网页，不读取 API 凭据或现有浏览器配置。安装组件需要联网，并由你手动启动。")
        browser_hint.setWordWrap(True)
        browser_hint.setObjectName("muted")
        browser_layout.addWidget(browser_hint)
        browser_row = QHBoxLayout()
        self.browser_status = QLabel("尚未检查")
        self.browser_status.setWordWrap(True)
        browser_row.addWidget(self.browser_status, 1)
        self.browser_check_button = QPushButton("检查浏览器")
        self.browser_check_button.clicked.connect(self.diagnose_browser)
        browser_row.addWidget(self.browser_check_button)
        self.browser_install_button = QPushButton("安装浏览器组件")
        self.browser_install_button.clicked.connect(self.install_browser)
        browser_row.addWidget(self.browser_install_button)
        browser_layout.addLayout(browser_row)
        content = QWidget()
        groups = QVBoxLayout(content)
        groups.setContentsMargins(0, 0, 6, 0)
        for group in (appearance, destination, secrets, ffmpeg, browser):
            groups.addWidget(group)
        groups.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.addWidget(page_header("设置", "选择外观与输出位置，管理本机 API 凭据。"))
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.save_status, 1)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    def _load_settings(self) -> AppSettings:
        try:
            return self.settings_store.load()
        except ValueError:
            return AppSettings()

    @staticmethod
    def _theme_value(text: str) -> str:
        return {"system": "system", "light": "light", "dark": "dark"}.get(text, "system")

    def _refresh_credential_status(self) -> None:
        for name, label in self.credential_status.items():
            try:
                label.setText("已配置" if self.credential_store.has(name) else "未配置")
            except Exception:
                label.setText("凭据库不可用")

    def choose_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择默认输出目录", self.output_edit.text())
        if selected:
            self.output_edit.setText(selected)

    def _save_from_ui(self) -> None:
        try:
            self.save()
        except Exception:
            # Backend exceptions may contain credentials; never display them.
            self.save_status.setText("保存失败，请检查输出目录权限或本机凭据库。")

    def save(self) -> AppSettings:
        theme = self._theme_value(self.theme_combo.currentData())
        values = self._settings.model_dump()
        values.update({
            "default_output_path": Path(self.output_edit.text()),
            "theme": theme,
            "browser_enabled": self.browser_enabled_checkbox.isChecked(),
        })
        settings = AppSettings.model_validate(values)
        self.settings_store.save(settings)
        self._settings = settings
        for name, edit in self._credential_fields.items():
            if edit.text():
                self.credential_store.set(name, edit.text())
                edit.clear()
        self._refresh_credential_status()
        self.save_status.setText("设置已保存")
        self.theme_changed.emit(theme)
        return settings

    def diagnose_browser(self):
        status = check_browser_runtime()
        self.browser_status.setText(status.message)
        self.browser_install_button.setEnabled(not status.available)
        return status

    def install_browser(self) -> None:
        if self._browser_process.state() != QProcess.ProcessState.NotRunning:
            return
        try:
            self._browser_install_steps = browser_install_commands()
        except RuntimeError as exc:
            self.browser_status.setText(str(exc))
            return
        self._browser_install_index = 0
        self.browser_install_button.setEnabled(False)
        self.browser_check_button.setEnabled(False)
        self.browser_status.setText("正在安装 Playwright；完成后将安装 Chromium…")
        self._start_browser_install_step()

    def _start_browser_install_step(self) -> None:
        if self._browser_install_index >= len(self._browser_install_steps):
            self.browser_status.setText("浏览器组件已安装，请检查运行状态。")
            self.browser_check_button.setEnabled(True)
            self.diagnose_browser()
            return
        program, arguments = self._browser_install_steps[self._browser_install_index]
        self._browser_process.start(program, list(arguments))

    def _browser_install_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if exit_code != 0:
            self.browser_status.setText("安装失败。请检查网络连接和 Python 环境后重试。")
            self.browser_check_button.setEnabled(True)
            self.browser_install_button.setEnabled(True)
            self._browser_install_steps = ()
            return
        self._browser_install_index += 1
        self._start_browser_install_step()

    def _browser_install_error(self, _error: QProcess.ProcessError) -> None:
        self.browser_status.setText("无法启动安装程序。请检查本机 Python 环境。")
        self.browser_check_button.setEnabled(True)
        self.browser_install_button.setEnabled(True)
        self._browser_install_steps = ()

    def diagnose_ffmpeg(self):
        settings = self._settings
        status = discover_ffmpeg(
            configured_location=settings.ffmpeg_location,
            configured_probe_location=settings.ffprobe_location,
        )
        self.ffmpeg_status.setText("FFmpeg 已就绪" if status.available else "未找到 FFmpeg，请安装或配置视频工具。")
        if status.available and not status.ffprobe_path:
            self.ffmpeg_status.setText("FFmpeg 可用，尚未找到 ffprobe")
        self.ffmpeg_diagnostic.setText(status.diagnostic)
        self.ffmpeg_checked.emit(status)
        return status


__all__ = ["SettingsPage"]
