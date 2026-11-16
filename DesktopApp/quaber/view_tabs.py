"""Matrix tabs with independent, readable background colors."""
import re
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QPainter, QIcon, QPixmap
from PyQt6.QtWidgets import QTabBar
from .themes import theme

VIEW_COLORS = (("transparent", "Без цвета"), ("#3b82f6", "Синий"),
               ("#10a87c", "Зелёный"), ("#d89516", "Янтарный"),
               ("#8b5cf6", "Фиолетовый"), ("#dc2626", "Красный"), ("#e2558f", "Розовый"))


def blended_color(base, tint, strength=.12):
    base = QColor(base)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", tint or ""):
        return base.name()
    tint = QColor(tint)
    return QColor(*[round(a*(1-strength)+b*strength) for a,b in zip(base.getRgb()[:3], tint.getRgb()[:3])]).name()


def color_icon(color, background):
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#888888"))
    painter.setBrush(QColor(background if color == "transparent" else color))
    painter.drawRoundedRect(1, 1, 15, 15, 3, 3)
    if color == "transparent":
        painter.drawLine(3, 14, 14, 3)
    painter.end()
    return QIcon(pixmap)


class ViewTabs(QTabBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.hovered_tab = -1
        self.colors = []
        self.light = False

    def sizeHint(self):
        return QSize(sum(self.tabSizeHint(i).width() for i in range(self.count())), 28)

    def minimumSizeHint(self):
        return QSize(min(120, self.sizeHint().width()), 28)

    def tabSizeHint(self, index):
        return QSize(self.fontMetrics().horizontalAdvance(self.tabText(index)) + 24, 28)

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)

    def mouseMoveEvent(self, event):
        hovered = self.tabAt(event.position().toPoint())
        if hovered != self.hovered_tab:
            self.hovered_tab = hovered
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered_tab = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        colors = theme(self.light)
        painter.fillRect(self.rect(), QColor(colors["sidebar"]))
        for index in range(self.count()):
            rect = self.tabRect(index)
            selected = index == self.currentIndex()
            tint = self.colors[index] if index < len(self.colors) else "transparent"
            base = colors["editor"] if selected else colors["input"] if index == self.hovered_tab else colors["sidebar"]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(blended_color(base, tint, .25 if selected else .10)))
            painter.drawRoundedRect(rect.adjusted(1, 2, -1, 0), 4, 4)
            painter.setPen(QColor(colors["border"]))
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
            accent = tint if re.fullmatch(r"#[0-9a-fA-F]{6}", tint or "") else colors["accent"]
            if selected or tint != "transparent":
                painter.fillRect(rect.x(), rect.y(), rect.width(), 2 if selected else 1, QColor(accent))
            painter.setPen(QColor(colors["foreground"] if selected else colors["muted"]))
            text = self.tabText(index)
            painter.drawText(rect.adjusted(12, 3, -12, 0), Qt.AlignmentFlag.AlignCenter, text)
            if self.hasFocus() and selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(colors["accent"]))
                painter.drawRect(rect.adjusted(2, 4, -3, -3))
