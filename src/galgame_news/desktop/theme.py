"""Theme helpers for system/light/dark desktop presentation."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


def system_theme(app: QApplication | None = None) -> str:
    """Return the current platform color scheme as ``light`` or ``dark``."""

    app = app or QApplication.instance()
    if app is not None:
        try:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
        except AttributeError:
            pass
        try:
            window = app.palette().color(QPalette.ColorRole.Window)
            return "dark" if window.lightness() < 128 else "light"
        except (AttributeError, RuntimeError):
            pass
    return "light"


COLORS = {
    "light": dict(window="#FFF7F3", card="#FFFDFC", accent="#B7544E", selection="#F6E1DA",
                  text="#303747", border="#E4CCC3", muted="#77737F", hover="#9C443F",
                  disabled="#91868B", accent_text="#FFFFFF", secondary="#788B9E",
                  error="#A72D2D", warning="#8B631B"),
    "dark": dict(window="#242832", card="#303541", accent="#D98270", selection="#49404A",
                 text="#F5EFEA", border="#60616B", muted="#B8B0AC", hover="#E69A89",
                 disabled="#A09AA3", accent_text="#242832", secondary="#94A7BD",
                 error="#FF9B96", warning="#EBC582"),
}


def theme_colors() -> dict[str, str]:
    app = QApplication.instance()
    return COLORS["dark" if app and app.palette().color(QPalette.ColorRole.Window).lightness() < 128 else "light"]


def _palette(dark: bool) -> QPalette:
    palette = QPalette()
    c = COLORS["dark" if dark else "light"]
    colors = {
        "Window": c["window"], "WindowText": c["text"], "Base": c["card"],
        "AlternateBase": c["selection"], "Text": c["text"], "Button": c["card"],
        "ButtonText": c["text"], "Highlight": c["accent"], "HighlightedText": c["accent_text"],
        "Mid": c["border"], "PlaceholderText": c["muted"],
        "ToolTipBase": c["card"], "ToolTipText": c["text"], "Link": c["accent"],
    }
    for role, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["disabled"]))
    return palette


STYLE = """
QWidget { color: palette(window-text); }
QMainWindow, QStackedWidget { background: palette(window); }
QLabel { background: transparent; }
QLabel#pageTitle { font-size: 25px; font-weight: 700; }
QLabel#brand { font-size: 19px; font-weight: 700; }
QLabel#muted { color: palette(placeholder-text); }
QFrame#sidebar, QGroupBox, QFrame#card { background: palette(base); border: 1px solid palette(mid); border-radius: 14px; }
QGroupBox { margin-top: 16px; padding: 22px 16px 14px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 18px; padding: 2px 8px; }
QLineEdit, QComboBox, QPlainTextEdit { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; padding: 8px; selection-background-color: palette(highlight); }
QLineEdit:focus, QComboBox:focus { border: 1px solid @accent; }
QPushButton { background: palette(button); border: 1px solid palette(mid); border-radius: 8px; padding: 9px 15px; }
QPushButton:hover { border-color: @accent; background: palette(alternate-base); }
QPushButton:pressed { background: palette(mid); }
QPushButton[primary="true"] { background: @accent; color: @accent_text; border: 1px solid @accent; font-weight: 600; }
QPushButton[primary="true"]:hover { background: @hover; }
QPushButton:disabled, QPushButton[primary="true"]:disabled { color: @disabled; background: palette(alternate-base); border-color: palette(mid); }
QCheckBox { spacing: 9px; padding: 7px 0; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid palette(mid); border-radius: 4px; background: palette(base); }
QCheckBox::indicator:checked { background: @accent; border: 3px solid @selection; }
QCheckBox::indicator:disabled { background: palette(alternate-base); border-color: @disabled; }
QListWidget, QTreeWidget { background: palette(base); border: 1px solid palette(mid); border-radius: 12px; padding: 6px; outline: none; }
QListWidget::item, QTreeWidget::item { padding: 10px; border-radius: 8px; }
QListWidget::item:selected, QTreeWidget::item:selected { background: palette(alternate-base); color: palette(text); border: 1px solid @accent; }
QListWidget#navigation { background: transparent; border: none; padding: 0; }
QListWidget#navigation::item { padding: 15px 10px; margin: 4px 0; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: palette(base); padding: 10px 15px; border: 1px solid palette(mid); }
QTabBar::tab:selected { color: @accent; border-bottom: 3px solid @accent; }
QProgressBar { background: palette(base); border: 1px solid palette(mid); border-radius: 7px; text-align: center; min-height: 23px; }
QProgressBar::chunk { background: @accent; border-radius: 6px; }
QSplitter::handle { background: palette(window); width: 7px; height: 7px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: palette(mid); border-radius: 5px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { padding: 7px; border: 1px solid palette(mid); }
QLabel[severity="error"] { color: @error; }
QLabel[severity="warning"] { color: @warning; }
"""


def stylesheet(dark: bool) -> str:
    result = STYLE
    # Replace longer names first (accent_text before accent).
    for name, color in sorted(COLORS["dark" if dark else "light"].items(), key=lambda item: -len(item[0])):
        result = result.replace("@" + name, color)
    return result


def _prepare_font(app: QApplication) -> None:
    if app.property("toolboxFontReady"):
        return
    from pathlib import Path
    # Qt offscreen environments may not discover installed Windows fonts.
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        path = Path("C:/Windows/Fonts") / filename
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    font = QFont("Microsoft YaHei UI", 10)
    font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "sans-serif"])
    app.setFont(font)
    app.setProperty("toolboxFontReady", True)


def apply_theme(theme: str = "system", app: QApplication | None = None) -> str:
    """Apply a small palette override and return the effective theme."""

    app = app or QApplication.instance()
    selected = str(theme or "system").casefold()
    if selected not in {"system", "light", "dark"}:
        selected = "system"
    effective = system_theme(app) if selected == "system" else selected
    if app is not None:
        if not app.property("toolboxThemeConnected"):
            app.setProperty("toolboxSystemDark", system_theme(app) == "dark")
            app.styleHints().colorSchemeChanged.connect(
                lambda scheme: _system_changed(app, scheme)
            )
            app.setProperty("toolboxThemeConnected", True)
        if selected == "system":
            effective = "dark" if app.property("toolboxSystemDark") else "light"
        app.setProperty("toolboxTheme", selected)
        if app.style().objectName().casefold() != "fusion":
            app.setStyle("Fusion")
        _prepare_font(app)
        app.setPalette(_palette(effective == "dark"))
        app.setStyleSheet(stylesheet(effective == "dark"))
    return effective


def _system_changed(app: QApplication, scheme) -> None:
    app.setProperty("toolboxSystemDark", scheme == Qt.ColorScheme.Dark)
    if app.property("toolboxTheme") == "system":
        apply_theme("system", app)


__all__ = ["apply_theme", "system_theme"]
