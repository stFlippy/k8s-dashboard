"""Container-instance identity and the keyboard accessible log source control."""
from .tooltips import show_tooltip
from dataclasses import dataclass
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import QToolButton, QMenu


@dataclass(frozen=True)
class LogSourceKey:
    cluster_id: str
    credentials: str
    namespace: str
    pod_name: str
    pod_uid: str
    container: str
    runtime: tuple
    previous: bool
    tail: int | None


def runtime_fingerprint(resource, container):
    spec = resource.get('spec', {})
    status = resource.get('status', {})
    for field, statuses in (('containers','containerStatuses'),
                             ('initContainers','initContainerStatuses'),
                             ('ephemeralContainers','ephemeralContainerStatuses')):
        if any(c.get('name') == container for c in spec.get(field, [])):
            entry = next((c for c in status.get(statuses, []) if c.get('name') == container), {})
            last = entry.get('lastState', {}).get('terminated', {})
            return (field, entry.get('containerID'), entry.get('restartCount'), last.get('finishedAt'))
    return ('unknown', None, None, None)


class LogSourceSelector(QToolButton):
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._previous = False
        self.availability = 'UNKNOWN'
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self._activate)
        self.set_availability('UNKNOWN')

    def isChecked(self):
        return self._previous

    def setChecked(self, previous):
        previous = bool(previous)
        if previous and self.availability != 'AVAILABLE':
            return
        if self._previous != previous:
            self._previous = previous
            self._present()
            self.toggled.emit(previous)

    def set_availability(self, state, explanation=None):
        self.availability = state
        self._explanation = explanation or {
            'UNKNOWN':'Доступность предыдущего лога ещё не подтверждена.',
            'CHECKING':'Проверяем доступность предыдущего лога…',
            'AVAILABLE':'Выберите источник лога.',
            'UNAVAILABLE':'Предыдущий лог недоступен для этого контейнера.',
            'ERROR':'Не удалось проверить доступность предыдущего лога.'}[state]
        self.setProperty('sourceSwitchAvailable', state == 'AVAILABLE')
        self._present()

    def _present(self):
        self.setText('Previous log' if self._previous else 'Current log')
        self.setArrowType(Qt.DownArrow if self.availability == 'AVAILABLE' else Qt.NoArrow)
        self.setToolTip(self._explanation)
        self.setAccessibleName(self.text())
        self.setAccessibleDescription(self._explanation)
        style = '' if self.availability == 'AVAILABLE' else 'QToolButton { color: #888888; }'
        if style != self.styleSheet():
            self.setStyleSheet(style)

    def _activate(self):
        if self.availability != 'AVAILABLE':
            show_tooltip(self.mapToGlobal(self.rect().bottomLeft()), self._explanation, self)
            return
        menu = QMenu(self)
        for label, previous in (('Current log',False),('Previous log',True)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(previous == self._previous)
            action.triggered.connect(lambda checked, value=previous: self.setChecked(value))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._activate()
            event.accept()
            return
        super().keyPressEvent(event)
