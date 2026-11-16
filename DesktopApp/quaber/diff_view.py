"""Aligned before/after diff with original line numbers and changed-line colors."""
import re
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCursor, QTextFormat
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSplitter, QTextEdit, QPlainTextEdit
from .widgets import NumberedEditor
from .themes import code_font


def aligned_diff(diff):
    rows, removed, added = [], [], []
    old = new = 1
    inside_hunk = False
    def flush():
        for index in range(max(len(removed), len(added))):
            left = removed[index] if index < len(removed) else (None, "")
            right = added[index] if index < len(added) else (None, "")
            rows.append((left[0], left[1], right[0], right[1], True))
        removed.clear()
        added.clear()
    for line in diff.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            inside_hunk = True
            flush()
            if rows:
                rows.append((None, "···", None, "···", False))
            old, new = map(int, match.groups())
        elif not inside_hunk or line.startswith("\\"):
            continue
        elif line.startswith("-"):
            removed.append((old, line[1:]))
            old += 1
        elif line.startswith("+"):
            added.append((new, line[1:]))
            new += 1
        elif line.startswith(" "):
            flush()
            rows.append((old, line[1:], new, line[1:], False))
            old += 1
            new += 1
    flush()
    return rows


class DiffView(QWidget):
    def __init__(self, diff, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)
        rows = aligned_diff(diff)
        self.editors = []
        controller = getattr(parent, "controller", None)
        light = getattr(controller, "preferences", {}).get("theme") == "light" if controller else self.palette().text().color().lightness() < 128
        for side, title in ((0, "До"), (1, "После")):
            pane = QWidget()
            column = QVBoxLayout(pane)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(QLabel(title))
            editor = NumberedEditor()
            editor.setObjectName("codeEditorText")
            editor.setReadOnly(True)
            editor.setFont(code_font())
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            editor.line_numbers = [row[side*2] for row in rows]
            editor.setPlainText("\n".join(row[side*2+1] for row in rows))
            selections = []
            for index, row in enumerate(rows):
                if row[4] and row[side*2] is not None:
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = QTextCursor(editor.document().findBlockByNumber(index))
                    selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                    selection.format.setBackground(QColor(("#FADADD" if side == 0 else "#D9F2DF") if light else ("#4C2531" if side == 0 else "#203E32")))
                    selection.format.setForeground(QColor("#3B3B3B" if light else "#D8E1ED"))
                    selections.append(selection)
            editor.setExtraSelections(selections)
            column.addWidget(editor)
            splitter.addWidget(pane)
            self.editors.append(editor)
        for axis in ("verticalScrollBar", "horizontalScrollBar"):
            left, right = [getattr(editor, axis)() for editor in self.editors]
            left.valueChanged.connect(right.setValue)
            right.valueChanged.connect(left.setValue)
