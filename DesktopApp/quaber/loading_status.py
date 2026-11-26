"""A small spinner for admitted background work, including queues."""
from collections import Counter
from PyQt5.QtCore import Qt, QTimer, QRectF, QEvent
from PyQt5.QtGui import QPainter, QPen
from PyQt5.QtWidgets import QWidget


def task_label(function, key=None):
    name = (str(key) + ' ' + getattr(function, '__qualname__', '')).lower()
    for needles, label in (
        (('quota',), 'Квоты ресурсов'),
        (('inventory',), 'Dumps / сверка версий'),
        (('matrix',), 'Нагрузки и Pod'),
        (('namespaces',), 'Список namespace'),
        (('configmap', 'resources'), 'Список ресурсов'),
        (('metrics',), 'Метрики'),
        (('log',), 'Логи'),
        (('access', 'capabilit', 'admin'), 'Проверка прав'),
        (('login', 'renew', 'authoriz'), 'Авторизация'),
        (('export',), 'Сохранение файла'),
        (('resource', 'deployment', 'pod'), 'Карточка ресурса'),
        (('schedule',), 'Расписание CronJob'),
    ):
        if any(needle in name for needle in needles):
            return label
    return 'Загрузка данных'


class LoadingStatus(QWidget):
    def __init__(self, background, parent=None):
        super().__init__(parent)
        self.background = background
        self.setObjectName('loadingStatus')
        self.setFixedSize(22, 22)
        self.setAccessibleName('Загрузка данных')
        self._angle = 0
        self._active = False
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._advance)
        self.window().installEventFilter(self)
        background.activity_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        running, queued = self.background.activities()
        counts = Counter(running + queued)
        self._active = bool(counts)
        self.setToolTip('\n'.join(f'{name}: {count}' for name, count in sorted(counts.items()))
                        + (f'\nВыполняются: {len(running)} · в очереди: {len(queued)}' if counts else ''))
        self.setVisible(bool(counts))
        self._sync_animation()

    def _sync_animation(self):
        if self._active and self.isVisible() and not self.window().isMinimized():
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_animation()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.WindowStateChange:
            self._sync_animation()
        return False

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def _advance(self):
        self._sync_animation()
        if self.timer.isActive():
            self._angle = (self._angle + 30) % 360
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(self.palette().highlight().color(), 2.5, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(QRectF(4, 4, 14, 14), -self._angle * 16, 260 * 16)
