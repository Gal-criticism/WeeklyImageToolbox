"""Small shared widgets and offline vector artwork for the toolbox."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
from .theme import theme_colors


def page_header(title: str, subtitle: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 8)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    text = QLabel(subtitle)
    text.setObjectName("muted")
    text.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(text)
    return widget


def icon(name: str) -> QIcon:
    """Line icons painted locally; no fonts, downloads or data files needed."""
    result = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(24 * scale, 24 * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme_colors()["accent"]), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if name in {"new", "file"}:
            painter.drawRoundedRect(5, 3, 14, 18, 2, 2)
            painter.drawLine(9, 11, 15, 11)
            painter.drawLine(12, 8, 12, 14)
        elif name == "progress":
            painter.drawArc(3, 3, 18, 18, 30 * 16, 300 * 16)
            painter.drawLine(12, 6, 12, 12)
            painter.drawLine(12, 12, 16, 14)
        elif name == "review":
            painter.drawRoundedRect(3, 4, 18, 16, 2, 2)
            painter.drawEllipse(6, 7, 3, 3)
            painter.drawLine(4, 18, 12, 11)
            painter.drawLine(12, 11, 20, 18)
        elif name == "history":
            painter.drawRoundedRect(4, 3, 16, 18, 2, 2)
            for y in (8, 12, 16):
                painter.drawLine(8, y, 16, y)
        elif name == "settings":
            for y, x in ((6, 9), (12, 16), (18, 7)):
                painter.drawLine(3, y, 21, y)
                painter.drawEllipse(x - 2, y - 2, 4, 4)
        else:
            painter.drawLine(4, 12, 20, 12)
            painter.drawLine(12, 4, 12, 20)
        painter.end()
        result.addPixmap(pixmap)
    return result


class Starlight(QWidget):
    """Subtle, entirely local decoration, restricted to the start page."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        colors = theme_colors()
        for fraction, y, radius, color in ((.05, 18, 7, colors["accent"]), (.40, 12, 4, colors["secondary"]), (.79, 20, 6, colors["accent"]), (.95, 10, 3, colors["secondary"])):
            x = self.width() * fraction
            path = QPainterPath()
            path.moveTo(x, y - radius)
            path.lineTo(x + radius / 3, y - radius / 3)
            path.lineTo(x + radius, y)
            path.lineTo(x + radius / 3, y + radius / 3)
            path.lineTo(x, y + radius)
            path.lineTo(x - radius / 3, y + radius / 3)
            path.lineTo(x - radius, y)
            path.lineTo(x - radius / 3, y - radius / 3)
            path.closeSubpath()
            painter.fillPath(path, QColor(color))


__all__ = ["page_header", "icon", "Starlight"]
