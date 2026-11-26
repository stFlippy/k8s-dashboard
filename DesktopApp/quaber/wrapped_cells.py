"""Paint full source values with dynamic two-line wrapping after column resizing."""
from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QTextLayout, QTextOption
from PyQt5.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle


class WrappedCells(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(option.fontMetrics.lineSpacing()*2+18)
        return size

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        text, styled.text = styled.text, ''
        styled.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        layout = QTextLayout(text, styled.font)
        wrapping = QTextOption()
        wrapping.setWrapMode(QTextOption.WrapMode.WrapAnywhere)
        layout.setTextOption(wrapping)
        layout.beginLayout()
        height = 0
        for _ in range(2):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, styled.rect.width()-24))
            line.setPosition(QPointF(0, height))
            height += line.height()
        layout.endLayout()
        painter.save()
        painter.setClipRect(styled.rect.adjusted(12, 0, -12, 0))
        painter.setPen(styled.palette.highlightedText().color() if styled.state & QStyle.StateFlag.State_Selected else styled.palette.text().color())
        layout.draw(painter, QPointF(styled.rect.left()+12, styled.rect.center().y()-height/2))
        painter.restore()
