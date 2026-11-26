"""Shared login presentation and bounded time-zone choices (no transport)."""
import hashlib
from PyQt5.QtGui import QColor
from .widgets import AppComboBox


def login_color(username):
    hue = int.from_bytes(hashlib.sha256(username.encode('utf-8')).digest()[:2], 'big') % 360
    return QColor.fromHsv(hue, 150, 210).name()


def password_groups(clusters):
    groups = {}
    for cluster in sorted(clusters, key=lambda c: (c['username'].casefold(), c['name'].casefold())):
        key = cluster.get('accountId') or cluster['id']
        groups.setdefault(key, {'accountId': cluster.get('accountId'), 'username': cluster['username'], 'clusters': []})['clusters'].append(cluster)
    return list(groups.values())


class TimeZonePicker(AppComboBox):
    def __init__(self, value='', parent=None):
        super().__init__(parent)
        self.setObjectName('connectionTimeZone')
        self.addItem('Не задано', '')
        offsets = sorted({hours * 60 for hours in range(-12, 15)} | {-570, -210, 210, 270, 330, 345, 390, 525, 570, 630, 765, 825})
        for minutes in offsets:
            sign = '+' if minutes >= 0 else '−'
            hours, remainder = divmod(abs(minutes), 60)
            key = 'UTC' if not minutes else f"UTC{'+' if minutes >= 0 else '-'}{hours:02d}:{remainder:02d}"
            self.addItem('UTC 0' if not minutes else f'UTC{sign}{hours:02d}:{remainder:02d}', key)
        self.setCurrentIndex(max(0, self.findData(value)))
        self.setMaxVisibleItems(12)
        self.setToolTip('Постоянное UTC-смещение для расчёта CronJob. Значение задаётся вручную и действует круглый год.')

    def text(self):
        return self.currentData() or ''


class ConnectionStatePicker(AppComboBox):
    """Scrolling the connection table must not switch monitoring on or off."""
    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()
