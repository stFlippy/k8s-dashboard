"""Pod log viewer: source-aware snapshots, independent polling and following."""
import re
import threading
import time
import copy
from contextlib import nullcontext
from datetime import datetime
from dataclasses import replace

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QTextCursor, QTextDocument
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QPlainTextEdit, QPlainTextDocumentLayout, QFileDialog, QMessageBox)
from .widgets import FullCheckBox, AppComboBox
from .themes import code_font
from .kubernetes import LogTooLarge, KubernetesAPIError
from .log_source import LogSourceKey, LogSourceSelector, runtime_fingerprint
from .polling import polling_allowed, mark_poll_timer
from .container_selection import preferred_container

TAIL_LINES = 1000
MAX_DISPLAY_LINES = 50000


class PodLogs(QWidget):
    def __init__(self, owner, containers, *, defer_initial=False):
        super().__init__(owner)
        from .read_lifecycle import register_read_owner
        register_read_owner(owner, self)
        self.setObjectName("podLogView")
        self.owner = owner
        self.loading = False
        self._load_operation = None
        self.again = False
        self.generation = 0
        self.source = None
        self.source_key = None
        self._resource = copy.deepcopy(getattr(owner, 'resource', {}))
        self._availability_key = None
        self._availability_operation = None
        self._download_cancel = threading.Event()
        self.pending_text = None

        self._source_text = None
        self._display_text = ''
        self._display_revision = 0
        self._display_source = None
        self._search_key = None
        self._display_stats = (0, 0)
        self._preparation_version = 0
        self._preparation_cancel = threading.Event()
        self._search_cancel = threading.Event()
        self._building = False
        self._build_state = None
        self._last_render_raw = None
        self._build_timer = QTimer(self)
        self._build_timer.setInterval(0)
        self._build_timer.timeout.connect(self._build_step)
        self.matches = []
        self.match_index = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(QLabel("Контейнер"))
        self.container = AppComboBox()
        self.container.addItems(containers)
        regular = [c.get("name", "") for c in getattr(owner, "resource", {}).get("spec", {}).get("containers", [])]
        preferred = preferred_container(regular or containers)
        if preferred in containers:
            self.container.setCurrentText(preferred)
        self.container.setMinimumWidth(100)
        self.container.setMaximumWidth(210)
        self.container.setToolTip("Контейнер для логов. Выбор не меняет контейнер для Terminal.")
        row.addWidget(self.container)
        self.search = QLineEdit()
        self.search.setMaximumWidth(300)
        self.search.setMinimumWidth(130)
        self.search.setPlaceholderText("Поиск в логе…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Enter / F3 — следующее совпадение; Shift+Enter / Shift+F3 — предыдущее.")
        row.addWidget(self.search, 1)
        self.count = QLabel("0/0")
        row.addWidget(self.count)
        for label, backwards in (("↑", True), ("↓", False)):
            button = QPushButton(label)
            button.setMaximumWidth(32)
            button.clicked.connect(lambda _, back=backwards: self.find_next(back))
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.fragment_label = QLabel("Последние 1000 строк")
        row.addWidget(self.fragment_label)
        self.expanded = FullCheckBox("full log")
        self.expanded.setToolTip("Показать весь доступный лог. Если он больше 4 МиБ или 50000 строк, будет предложено скачивание.")
        row.addWidget(self.expanded)
        self.follow = FullCheckBox("autoscrolling")
        self.follow.setChecked(True)
        self.follow.setToolTip("Следовать за концом лога. Действия в тексте/поиске отключают прокрутку, но не получение новых данных.")
        row.addWidget(self.follow)
        self.previous = LogSourceSelector(self)
        row.addWidget(self.previous)
        self.export_button = QPushButton("Сохранить log…")
        self.export_button.setEnabled(False)
        self.export_button.setToolTip("Сохранить показанный фрагмент лога.")
        self.export_button.clicked.connect(self.export)
        row.addWidget(self.export_button)
        row.addStretch()
        controls = QWidget()
        controls.setLayout(row)
        if hasattr(owner.controller, "_scroll_controls"):
            layout.addWidget(owner.controller._scroll_controls(controls))
        else:
            layout.addWidget(controls)
        self._full_prompt = False
        self._download_running = False
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.download_status = QLabel()
        self.download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.download_status.setWordWrap(True)
        self.download_status.hide()
        layout.addWidget(self.download_status)
        self.output = QPlainTextEdit()
        self.output.setObjectName("podLogs")
        self.output.setFont(code_font())
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setMaximumBlockCount(MAX_DISPLAY_LINES+1)
        self.output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output.viewport().installEventFilter(self)
        self.output.installEventFilter(self)
        self.search.installEventFilter(self)
        self.output.verticalScrollBar().actionTriggered.connect(self.pause_follow)
        self.output.verticalScrollBar().sliderPressed.connect(self.pause_follow)
        self.output.horizontalScrollBar().actionTriggered.connect(self.pause_follow)
        self.output.horizontalScrollBar().sliderPressed.connect(self.pause_follow)
        self.output.selectionChanged.connect(lambda: self.pause_follow() if self.output.textCursor().hasSelection() else None)
        layout.addWidget(self.output, 1)
        self.status = QLabel("Загрузка логов…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.update_search)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.textEdited.connect(self.pause_follow)
        self.search.returnPressed.connect(self.find_next)
        self.follow.toggled.connect(lambda enabled: self.resume_follow() if enabled else None)
        self.container.currentTextChanged.connect(self.load)
        self.previous.toggled.connect(self.load)
        self.expanded.toggled.connect(self.load)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(lambda: self.load(poll=True) if self.isVisible() else None)
        self.timer.start()
        mark_poll_timer(self.timer)
        if not defer_initial:
            QTimer.singleShot(0, self.load)

    def suspend_reads(self):
        self.cancel_preparation()
        self.loading = self.again = False
        self.generation += 1
        self._load_operation = None
        self._availability_operation = None

    def pause_follow(self, *_):
        self.follow.setChecked(False)

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous(): return
        if self.source is None:
            self.load()
        QTimer.singleShot(0, self.resume_follow)

    def hideEvent(self, event):
        if not event.spontaneous(): self.cancel_preparation()
        super().hideEvent(event)

    def _key(self, previous=False, tail=TAIL_LINES):
        return LogSourceKey(self.owner.cluster_id, getattr(self.owner,'_opened_credentials',''),
                            self.owner.namespace,self.owner.name,self._resource.get('metadata',{}).get('uid',''),
                            self.container.currentText(),runtime_fingerprint(self._resource,self.container.currentText()),
                            previous,tail)

    def update_resource(self, resource):
        self._resource = copy.deepcopy(resource)
        current = self._key(self.previous.isChecked(),None if self.expanded.isChecked() else TAIL_LINES)
        if self.source_key is not None and current != self.source_key:
            self._download_cancel.set()
            self.generation += 1
            self.cancel_preparation()
            self.source_key = None
            self._source_text = None
            self._availability_key = None
            self.previous.set_availability('UNKNOWN')
            self.previous.setChecked(False)
            self.status.setText('Экземпляр контейнера изменился; ожидается новый лог.')
        if self.isVisible():
            self._check_previous()

    def _check_previous(self):
        key = self._key()
        if key == self._availability_key:
            return
        self._availability_key = key
        self.previous.set_availability('UNKNOWN')
        self.previous.setChecked(False)
        _, _, restarts, finished = key.runtime
        if restarts == 0 and not finished:
            self.previous.set_availability('UNAVAILABLE','Предыдущий лог недоступен: контейнер ещё не перезапускался.')
            return
        if not (isinstance(restarts,int) and restarts > 0 or finished):
            return
        if not self.isVisible() or not polling_allowed(self):
            self._availability_key = None
            return
        self.previous.set_availability('CHECKING')
        operation = self._availability_operation = object()
        api = self.owner.api
        def probe():
            try:
                api.logs(key.cluster_id,key.namespace,key.pod_name,key.container,True,1)
                return 'AVAILABLE'
            except KubernetesAPIError as exc:
                if (exc.status == 400 and exc.reason == 'BadRequest' and
                        re.search(r'previous terminated container .+ not found',exc.message,re.I)):
                    return 'UNAVAILABLE'
                raise
        def done(state):
            if self._key() == key and self._availability_operation is operation:
                self.previous.set_availability(state)
        def failed(message):
            if self._key() == key and self._availability_operation is operation:
                self.previous.set_availability('ERROR',str(message)[:300])
        def cancelled():
            if self._key() == key and self._availability_operation is operation:
                self._availability_key = None
                self.previous.set_availability('UNKNOWN','Проверка приостановлена.')
                # showEvent can precede the card's tab epoch change. Its old
                # probe is cancelled correctly; request only the now active tab.
                QTimer.singleShot(0,self._resume_probe)
        if self.owner.background.submit(probe,done,failed,key=('log-source',key),demand='tab',cancelled=cancelled) is False:
            self._availability_key = None
            self.previous.set_availability('ERROR','Проверка отложена: очередь занята. Повторите обновление.')

    def _resume_probe(self):
        from PyQt5 import sip
        if not sip.isdeleted(self) and self.isVisible() and polling_allowed(self):
            self._check_previous()

    def cancel_preparation(self):
        self._preparation_version += 1
        self._preparation_cancel.set()
        self._search_cancel.set()
        self._build_timer.stop()
        if self._build_state is not None:
            self._build_state['document'].deleteLater()
        self._build_state = None
        self._building = False
        self._source_text = None

    def _preparation_valid(self, version, source):
        from PyQt5 import sip
        return (not sip.isdeleted(self) and version == self._preparation_version
                and source == self.source and polling_allowed(self))

    def eventFilter(self, watched, event):
        from PyQt5 import sip
        output = getattr(self, "output", None)
        if output is None or sip.isdeleted(output):
            return super().eventFilter(watched, event)
        if (watched is self.search and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.find_next(True)
            return True
        if watched is self.search and event.type() == QEvent.Type.MouseButtonPress:
            self.pause_follow()
        if (watched is self.output.viewport() and event.type() in (QEvent.Type.Wheel, QEvent.Type.TouchBegin)) or (
                watched is self.output and event.type() == QEvent.Type.KeyPress):
            self.pause_follow()
        if (watched is self.output.viewport() and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            self.pause_follow()
        return super().eventFilter(watched, event)

    def resume_follow(self):
        blocked = self.follow.blockSignals(True)
        self.follow.setChecked(True)
        self.follow.blockSignals(blocked)
        if self.pending_text is not None:
            self.show_text(self.pending_text)
            self.pending_text = None
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def load(self, *_, poll=False, force=False):
        if not polling_allowed(self):
            return
        if self._full_prompt:
            return
        if force:
            self._availability_key = None
        self._check_previous()
        wanted = (self.container.currentText(), self.previous.isChecked(), None if self.expanded.isChecked() else TAIL_LINES)
        key = self._key(wanted[1],wanted[2])
        if poll and wanted[1] and key == self.source_key:
            return
        if wanted != self.source or key != self.source_key:
            self.cancel_preparation()
            self.generation += 1
            self.source = wanted
            self.source_key = key
            self.fragment_label.setText("Полный доступный лог" if wanted[2] is None else "Последние 1000 строк")
            self.pending_text = None
            self._source_text = None
            blocked = self.follow.blockSignals(True)
            self.follow.setChecked(True)
            self.follow.blockSignals(blocked)
            self.export_button.setEnabled(bool(self._display_text))
            if self._display_text:
                shown_source = self._display_source or ('—', False, None)
                self.fragment_label.setText(f'Предыдущий фрагмент · {shown_source[0]} · '
                    f'{"предыдущий" if shown_source[1] else "текущий"} запуск')
            self.error_label.hide()
            self.status.setText(f"Загрузка: {wanted[0]} · {'предыдущий' if wanted[1] else 'текущий'} запуск…")
        if self.loading:
            self.again = True
            return
        if not wanted[0]:
            self.status.setText("В Pod нет доступных контейнеров.")
            return
        self.loading, self.again = True, False
        self._load_operation = operation = object()
        generation = self.generation
        def finish():
            if self._load_operation is not operation:
                return False
            self._load_operation = None
            self.loading = False
            if self.again:
                QTimer.singleShot(0, self.load)
            return generation == self.generation and key == self._key(wanted[1],wanted[2])
        def done(text):
            if not finish():
                return
            if isinstance(text, LogTooLarge):
                self.offer_download(wanted)
                return
            self.error_label.clear()
            self.error_label.hide()
            self.output.setToolTip("")
            if text == self._source_text:
                return
            self._source_text = text
            # Keep the review position stable while still receiving newer snapshots.
            if not self.follow.isChecked() and self._display_text and text != self._display_text:
                self.pending_text = text
                self.status.setText("Получен новый фрагмент. Текущий оставлен для чтения; включите autoscrolling для перехода к новым строкам.")
            else:
                self.pending_text = None
                self.show_text(text)
                if not text:
                    self.status.setText('Контейнер пока не вывел логов.')
        def failed(message):
            if not finish():
                return
            self.error_label.setText(f"Логи недоступны: {str(message)[:500]}." + ('' if wanted[1] else ' Повтор через 5 сек.'))
            self.error_label.show()
            if self._display_text:
                self.status.setText('Обновление не удалось. Показан последний успешный фрагмент этого источника.'
                    if self._display_source == wanted else 'Новый источник не загружен. Показан предыдущий фрагмент.')
            else:
                self.status.setText("Нет загруженного фрагмента этого источника.")
        api = self.owner.api
        def read():
            try:
                text = api.logs(key.cluster_id,key.namespace,key.pod_name,key.container,key.previous,key.tail)
                if text.count("\n") + bool(text and not text.endswith("\n")) > MAX_DISPLAY_LINES:
                    return LogTooLarge("Слишком много строк для просмотра.")
                return text
            except LogTooLarge as exc:
                return exc
            except KubernetesAPIError as exc:
                if (key.previous and exc.status == 400 and exc.reason == 'BadRequest' and
                        re.search(r'previous terminated container .+ not found',exc.message,re.I)):
                    return exc
                raise
        original_done = done
        def done(text):
            if isinstance(text,KubernetesAPIError):
                if finish():
                    self.previous.set_availability('UNAVAILABLE')
                    self.previous.setChecked(False)
                    self.error_label.setText('Предыдущий лог больше недоступен. Загружается Current log.')
                    self.error_label.show()
                return
            original_done(text)
        if self.owner.background.submit(read, done, failed, demand='tab',cancelled=finish) is False:
            finish()
            self.status.setText('Чтение отложено. Повторите загрузку.')

    def offer_download(self, source):
        key = self.source_key
        if key is None or key != self._key(source[1],source[2]):
            return
        self._full_prompt = True
        blocked = self.expanded.blockSignals(True)
        self.expanded.setChecked(False)
        self.expanded.blockSignals(blocked)
        try:
            if self._download_running:
                return
            answer = QMessageBox.question(self, "Лог слишком большой",
                "Полный лог превышает объём просмотра (4 МиБ / 50000 строк). Скачать его в файл?\nВ окне будут показаны последние 1000 строк.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            filename = f"{self.owner.name}-{source[0]}-{'previous' if source[1] else 'current'}-full.log"
            path, _ = QFileDialog.getSaveFileName(self, "Скачать полный доступный лог", filename, "Логи (*.log *.txt)")
            if not path:
                return
            self._start_download(key, path)
        finally:
            self._full_prompt = False
            self.load()

    def _start_download(self, key, path):
        if self._download_running:
            self.download_status.show()
            return
        self._download_running = True
        operation = self._download_operation = object()
        self._download_cancel = download_cancel = threading.Event()
        self.download_status.setText("Скачивание полного доступного лога…")
        self.download_status.show()
        def done(count):
            if key != self._key(key.previous, key.tail) or download_cancel.is_set():
                return
            self.download_status.setText(f"Полный лог сохранён: {path} ({count} байт).")
            self.download_status.show()
        def failed(message):
            if self._download_operation is operation:
                self.download_status.setText("Не удалось скачать лог: " + str(message)[:500])
                self.download_status.show()
        def finalized(outcome):
            if self._download_operation is operation:
                self._download_running = False
                self._download_operation = None
                if outcome == 'CANCELLED':
                    self.download_status.setText('Скачивание лога отменено; незавершённый файл не опубликован.')
        api = self.owner.api
        def download():
            from .request_policy import expected_credentials
            with expected_credentials({key.cluster_id: key.credentials}) if key.credentials else nullcontext():
                return api.download_logs(key.cluster_id, key.namespace, key.pod_name, key.container,
                                         key.previous, path, cancel=download_cancel)
        admitted = self.owner.background.submit(download, done, failed, demand='tab', finalized=finalized,
                                                label='Скачивание полного лога')
        if admitted is False:
            failed('Не удалось начать скачивание. Повторите попытку.')
            finalized('CANCELLED')

    def show_text(self, text):
        if self._display_source == self.source and text == self._last_render_raw:
            return
        # Common small suffixes keep the append path. Large reset preparation and
        # search run in workers; only bounded document pieces run on the GUI thread.
        if len(text) > 65536 or len(self._display_text) > 65536:
            self._prepare_large(text)
            return
        if self._building:
            self.cancel_preparation()
        text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\u2029', '\n').replace('\u2028', '\n')
        if self._display_text != text or self._display_source != self.source:
            if (self.follow.isChecked() and self._display_source == self.source
                    and self._display_text and text.startswith(self._display_text)):
                cursor = QTextCursor(self.output.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text[len(self._display_text):])
            else:
                self.output.setPlainText(text)
            self._display_text = text
            self._display_source = self.source
            self._display_revision += 1
            self._display_stats = (len(self._display_text.splitlines()), len(self._display_text.encode('utf-8')))
            self.update_search(select=False)
        self._last_render_raw = text
        self._show_fragment_status()

    def _show_fragment_status(self):
        shown = self._display_text
        self.export_button.setEnabled(bool(shown))
        source = self.source or (self.container.currentText(), self.previous.isChecked(), None)
        self.fragment_label.setText("Полный доступный лог" if source[2] is None else "Последние 1000 строк")
        self.status.setText(f"{source[0]} · {'предыдущий' if source[1] else 'текущий'} запуск · "
            f"обновлено {datetime.now():%H:%M:%S} · {self._display_stats[0]} строк · "
            f"{self._display_stats[1]/1024:.0f} КиБ · ограниченный фрагмент" if shown else "В этом фрагменте записей нет.")
        if self.follow.isChecked():
            self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _prepare_large(self, raw):
        from .log_text import prepare_text
        self.cancel_preparation()
        self._preparation_cancel = threading.Event()
        self._source_text = raw
        canceled = self._preparation_cancel
        version, source = self._preparation_version, self.source
        needle = self.search.text()
        self._building = True
        def done(value):
            if not self._preparation_valid(version, source): return
            if not self.follow.isChecked() and self._display_text:
                self.pending_text = raw
                self._building = False
                return
            text = value['text']
            if text == self._display_text and self._display_source == source:
                self._last_render_raw = raw
                self._building = False
                return
            if (self._display_source == source and self._display_text and text.startswith(self._display_text)
                    and len(text)-len(self._display_text) <= 65536):
                cursor = QTextCursor(self.output.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text[len(self._display_text):])
                self._accept_prepared(value, raw, source)
                return
            document = QTextDocument(self)
            document.setDocumentLayout(QPlainTextDocumentLayout(document))
            document.setUndoRedoEnabled(False)
            document.setDefaultFont(self.output.font())
            document.setMaximumBlockCount(MAX_DISPLAY_LINES+1)
            self._build_state = {'document': document, 'cursor': QTextCursor(document), 'position': 0,
                                 'value': value, 'raw': raw, 'source': source, 'version': version}
            self._build_timer.start()
        def failed(message):
            if not self._preparation_valid(version, source): return
            self._building = False
            self.error_label.setText(str(message)[:500])
            self.error_label.show()
        if self.owner.background.submit(lambda: prepare_text(raw, needle, canceled), done, failed,
                                        key=('log-prepare', id(self)), monitor=False) is False:
            failed('Не удалось подготовить фрагмент лога.')

    def _build_step(self):
        state = self._build_state
        if state is None: return
        timings = state['value']['timings']
        started = time.perf_counter()
        try:
            self._build_slice()
        finally:
            timings.setdefault('gui_steps_ms', []).append((time.perf_counter()-started)*1000)

    def _build_slice(self):
        state = self._build_state
        if state is None: return
        if not self._preparation_valid(state['version'], state['source']):
            self.cancel_preparation()
            return
        started = time.perf_counter()
        text = state['value']['text']
        while state['position'] < len(text):
            end = min(len(text), state['position']+4096)
            state['cursor'].insertText(text[state['position']:end])
            state['position'] = end
            if time.perf_counter()-started >= .006: return
        self._build_timer.stop()
        self._build_state = None
        if not self.follow.isChecked() and self._display_text:
            self.pending_text = state['raw']
            state['document'].deleteLater()
            self._building = False
            return
        old = self.output.document()
        old.setParent(self)
        timings = state['value']['timings']
        released_at = time.perf_counter()
        old.destroyed.connect(lambda _, times=timings, start=released_at:
                              times.__setitem__('old_release_latency_ms', (time.perf_counter()-start)*1000))
        started = time.perf_counter()
        self.output.setDocument(state['document'])
        timings['adopt_ms'] = (time.perf_counter()-started)*1000
        old.deleteLater()
        self._accept_prepared(state['value'], state['raw'], state['source'])

    def _accept_prepared(self, value, raw, source):
        self._last_phase_times = value.get('timings', {})
        self._building = False
        self._last_render_raw = raw
        self._display_text, self._display_stats, self._display_source = value['text'], value['stats'], source
        self._display_revision += 1
        self._search_key = None
        if value['needle'] == self.search.text():
            self._accept_search(value['needle'], value['matches'], value['utf16'])
        else:
            self.update_search(select=False)
        self._show_fragment_status()

    def _accept_search(self, needle, matches, utf16):
        self._search_key = (self._display_revision, needle)
        self.matches, self._utf16_matches = matches, utf16
        self.match_text, self.match_index = self._display_text, -1
        self.count.setText(f"0/{len(matches)}{'+' if len(matches) == 10000 else ''}")

    def update_search(self, select=True):
        needle = self.search.text()
        key = (self._display_revision, needle)
        if key == self._search_key:
            return
        if len(self._display_text) > 65536:
            from .log_text import search_positions
            self._search_cancel.set()
            self._search_cancel = threading.Event()
            text, canceled = self._display_text, self._search_cancel
            version = self._preparation_version
            source = self.source
            self.matches, self._utf16_matches = [], []
            self.count.setText('…')
            def done(result):
                if (self._preparation_valid(version, source) and key == (self._display_revision, self.search.text())):
                    self._accept_search(needle, *result)
                    if select and self.matches: self.find_next()
            self.owner.background.submit(lambda: search_positions(text, needle, canceled), done, lambda _: None,
                                         key=('log-search', id(self)), monitor=False)
            return
        self._search_key = key
        self.matches, self.match_index = [], -1
        self.match_text = self._display_text
        if needle:
            for match in re.finditer(re.escape(needle), self.match_text, re.IGNORECASE):
                self.matches.append(match.span())
                if len(self.matches) == 10000:
                    break
        self._utf16_matches = []
        position = units = 0
        for start, end in self.matches:
            units += len(self.match_text[position:start].encode('utf-16-le'))//2
            first = units
            units += len(self.match_text[start:end].encode('utf-16-le'))//2
            self._utf16_matches.append((first, units))
            position = end
        self.count.setText(f"0/{len(self.matches)}{'+' if len(self.matches) == 10000 else ''}")
        if select and self.matches:
            self.find_next()

    def find_next(self, backwards=False):
        if self.search_timer.isActive():
            self.search_timer.stop()
            if len(self._display_text) > 65536 and self._search_key != (self._display_revision, self.search.text()):
                self.update_search(select=True)
                return
            self.update_search(select=False)
        if not self.matches:
            return
        self.pause_follow()
        self.match_index = ((len(self.matches)-1 if backwards else 0) if self.match_index < 0 else
                            (self.match_index + (-1 if backwards else 1)) % len(self.matches))
        start, end = self._utf16_matches[self.match_index]
        cursor = self.output.textCursor()
        # QTextCursor positions count UTF-16 code units, including surrogate pairs.
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()
        self.count.setText(f"{self.match_index+1}/{len(self.matches)}{'+' if len(self.matches) == 10000 else ''}")

    def export(self):
        if self._download_running:
            self.download_status.show()
            return
        source = self._display_source or self.source or (self.container.currentText(), self.previous.isChecked(), None)
        key = replace(self._key(source[1], None), container=source[0],
                      runtime=runtime_fingerprint(self._resource, source[0]))
        filename = f"{self.owner.name}-{key.container}-{'previous' if key.previous else 'current'}-{datetime.now():%Y%m%d-%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить полный доступный лог", filename, "Логи (*.log *.txt)")
        if path:
            self._start_download(key, path)
