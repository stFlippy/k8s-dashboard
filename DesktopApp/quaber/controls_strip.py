"""A horizontal controls strip that follows fonts, styles and scrollbar size."""
from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtWidgets import QScrollArea, QStyle


class ControlsStrip(QScrollArea):
    def __init__(self, widget):
        super().__init__()
        self.setWidget(widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)
        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.timeout.connect(self.fit_height)
        widget.installEventFilter(self)
        self.horizontalScrollBar().rangeChanged.connect(self.schedule_fit)
        self.fit_height()

    def schedule_fit(self, *unused):
        if not self._size_timer.isActive():
            self._size_timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.LayoutRequest, QEvent.FontChange, QEvent.StyleChange, QEvent.Polish, QEvent.Show):
            self.schedule_fit()
        return super().eventFilter(watched, event)

    def fit_height(self):
        widget = self.widget()
        height = max(widget.sizeHint().height(), widget.minimumSizeHint().height())
        if widget.minimumHeight() != height:
            widget.setMinimumHeight(height)
        bar = self.horizontalScrollBar()
        extra = self.style().pixelMetric(QStyle.PM_ScrollBarExtent) if bar.maximum() > bar.minimum() else 0
        total = max(1, height) + extra + 2 * self.frameWidth()
        if self.height() != total:
            self.setFixedHeight(total)
