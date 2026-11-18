"""Cluster dump records and label-based microservice version comparison."""
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt, pyqtSignal, QPointF
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox,
                            QTableWidgetItem, QStyledItemDelegate, QStyleOptionViewItem, QStyle)
from PyQt5.QtWidgets import QMenu
from .widgets import MultiSelectMenu
from .resource_list import ResourceList
from .inventory import APP_LABEL, CSV_FIELDS, inventory_row, namespace_snapshots, compare_versions
from .dump_export import export_dump
from .wrapped_cells import WrappedCells


COMPARISON_DETAILS = {
    "Совпадает": "Значения выбранных меток одинаковые во всех выбранных namespace. Пустые значения с обеих сторон также совпадают.",
    "Различается": "Значения выбранных меток различаются между namespace или у ресурсов одного микросервиса внутри namespace.",
    "Отсутствует": "Микросервис с этой меткой app.kubernetes.io/name не найден хотя бы в одном из выбранных namespace.",
    "Нет меток версий": "В ресурсах нет меток для сверки, либо хотя бы одна из выбранных меток отсутствует или пуста.",
    "Один namespace": "Для сравнения выберите минимум два namespace одного или разных кластеров.",
    "Неполные данные": "Чтение ещё не завершено или часть данных недоступна из-за ошибки. Результат сверки пока не определён.",
    "Выберите метки": "Выберите в списке «Labels» метки, значения которых нужно сравнить.",
}


def comparison_colors(light):
    return {"match": "#176F3D", "different": "#AA2430"} if light else {
        "match": "#8DE0AB", "different": "#FF9DA4"}


def cell(value, tooltip=""):
    result = QTableWidgetItem(str(value))
    result.setToolTip(tooltip)
    return result


LABEL_COLORS = int(Qt.ItemDataRole.UserRole)+1


class LabelComparisonDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        colors = index.data(LABEL_COLORS)
        if not colors:
            super().paint(painter, option, index)
            return
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        lines = styled.text.splitlines()
        styled.text = ""
        styled.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        painter.save()
        painter.setClipRect(styled.rect)
        painter.setFont(styled.font)
        metrics = styled.fontMetrics
        top = styled.rect.center().y()-len(lines)*metrics.lineSpacing()/2+metrics.ascent()
        for row, (line, color) in enumerate(zip(lines, colors)):
            painter.setPen(styled.palette.highlightedText().color() if styled.state & QStyle.StateFlag.State_Selected else
                           QColor(color) if color else styled.palette.text().color())
            painter.drawText(QPointF(styled.rect.left()+12, top+row*metrics.lineSpacing()),
                             metrics.elidedText(line, Qt.TextElideMode.ElideRight, max(0, styled.rect.width()-24)))
        painter.restore()


class NamespacePicker(QPushButton):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__("Namespace: Все ▾")
        self.selected = None
        self.choices = []
        self.clicked.connect(self.open_menu)

    def ids(self):
        available = {identity for identity, label in self.choices}
        return available if self.selected is None else self.selected & available

    def update_choices(self, scopes):
        self.choices = [(scope["scope_id"], scope["scope_name"]) for scope in scopes]
        self.setText("Namespace: Все ▾" if self.selected is None else f"Namespace: {len(self.ids())} ▾")
        self.setToolTip("\n".join(label for identity, label in self.choices if identity in self.ids()) or "Выберите namespace для сверки")

    def open_menu(self):
        menu = MultiSelectMenu(self)
        def apply(selected):
            self.selected = selected
            self.changed.emit()
            for identity, action in actions.items():
                action.setChecked(identity in self.ids())
        actions = {}
        menu.addAction("Все namespace", lambda: apply(None))
        menu.addAction("Снять все", lambda: apply(set()))
        menu.addSeparator()
        for identity, label in self.choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(identity in self.ids())
            actions[identity] = action
            def toggle(checked, identity=identity):
                selected = set(self.ids())
                selected.add(identity) if checked else selected.discard(identity)
                apply(selected)
            action.triggered.connect(toggle)
        try:
            menu.exec_(self.mapToGlobal(self.rect().bottomLeft()))
        finally:
            menu.deleteLater()


class ClusterPicker(QPushButton):
    changed = pyqtSignal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.available_clusters = service if callable(service) else service.clusters
        self.selected = None
        self.clicked.connect(self.open_menu)
        self.update_text()

    def ids(self, clusters=None):
        available = {cluster["id"] for cluster in (self.available_clusters() if clusters is None else clusters)}
        return available if self.selected is None else self.selected & available

    def update_text(self):
        clusters = self.available_clusters()
        ids = self.ids(clusters)
        self.setText("Кластеры: Все ▾" if self.selected is None else f"Кластеры: {len(ids)} ▾")
        self.setToolTip("\n".join(cluster["name"] for cluster in clusters if cluster["id"] in ids) or "Выберите кластеры")

    def open_menu(self):
        menu = MultiSelectMenu(self)
        all_action = menu.addAction("Все кластеры")
        all_action.setCheckable(True)
        all_action.setChecked(self.selected is None)
        actions = {}
        def sync():
            all_action.setChecked(self.selected is None)
            for cid, action in actions.items():
                action.setChecked(cid in self.ids())
            self.update_text()
            self.changed.emit()
        def all_changed():
            self.selected = None if all_action.isChecked() else set()
            sync()
        all_action.triggered.connect(all_changed)
        menu.addSeparator()
        for cluster in self.available_clusters():
            cid = cluster["id"]
            action = menu.addAction(cluster["name"])
            action.setCheckable(True)
            action.setChecked(cid in self.ids())
            actions[cid] = action
            def toggle(checked, cid=cid):
                selected = set(self.ids())
                selected.add(cid) if checked else selected.discard(cid)
                self.selected = selected
                sync()
            action.triggered.connect(toggle)
        try:
            menu.exec_(self.mapToGlobal(self.rect().bottomLeft()))
        finally:
            menu.deleteLater()


class InventoryPage(QWidget):
    def __init__(self, window, service, title):
        super().__init__()
        self.window, self.service = window, service
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(16, 12, 16, 12)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        self.layout_box.addWidget(heading)
        self.controls = QHBoxLayout()
        self.clusters = ClusterPicker(service)
        self.controls.addWidget(self.clusters)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.clicked.connect(lambda: self.refresh_data(force=True))
        self.controls.addWidget(self.refresh_button)
        self.layout_box.addLayout(self.controls)
        self.status = QLabel("Выберите кластеры.")
        self.status.setWordWrap(True)
        self.layout_box.addWidget(self.status)
        self.table = ResourceList()
        self.layout_box.addWidget(self.table, 1)
        self.clusters.changed.connect(self.refresh_data)
        service.changed.connect(self.render)

    def showEvent(self, event):
        super().showEvent(event)
        self.clusters.update_text()
        self.refresh_data()

    def refresh_data(self, *, force=False):
        self.clusters.update_text()
        self.service.refresh(self.clusters.ids(), force=force)
        self.render()

    def selected_clusters(self):
        clusters = self.service.clusters()
        ids = self.clusters.ids(clusters)
        return [cluster for cluster in clusters if cluster["id"] in ids]

    def snapshots(self):
        return [snapshot for cluster in self.selected_clusters() if (snapshot := self.service.get_for_cluster(cluster)) is not None]

    def waiting(self):
        return any(cluster["id"] in self.service.loading or self.service.get_for_cluster(cluster) is None
                   for cluster in self.selected_clusters())


class DumpsPage(InventoryPage):
    def __init__(self, window, service):
        super().__init__(window, service, "Dumps")
        self.exporting = False
        self.controls.addStretch()
        self.export_button = QPushButton("Экспортировать дамп")
        export_menu = QMenu(self.export_button)
        for format_name in ("CSV", "XLSX"):
            export_menu.addAction(format_name, lambda checked=False, format_name=format_name: self.export(format_name.lower()))
        self.export_button.setMenu(export_menu)
        self.export_button.setToolTip("Сохранить отображаемые строки таблицы в CSV или XLSX: хост, namespace, тип, имя, образы, ветка, автор, версия и реплики.")
        self.controls.addWidget(self.export_button)
        self.table.setColumnCount(len(CSV_FIELDS))
        self.table.setHorizontalHeaderLabels(["Хост", "Namespace", "Тип", "Имя", "Образы контейнеров", "Ветка", "Кем задеплоен", "Версия", "Реплики"])
        for column, key in enumerate(CSV_FIELDS):
            self.table.headerItem().setToolTip(column, key)
        self.table.setWordWrap(True)
        self.table.setItemDelegate(WrappedCells(self.table))
        self.table.setUniformRowHeights(True)
        self.table.header().sectionResized.connect(lambda *_: self.table.viewport().update())
        self.table.set_empty_text("Выберите кластеры для чтения снимка.")
        self.render()

    def render(self):
        selected = self.selected_clusters()
        waiting = self.waiting()
        snapshots = self.snapshots()
        records = [(snapshot, record) for snapshot in snapshots for record in snapshot["records"]]
        self.table.setRowCount(len(records))
        for row, (snapshot, record) in enumerate(records):
            for column, value in enumerate(inventory_row(snapshot, record)):
                self.table.setItem(row, column, cell(value, snapshot["cluster_name"] + "\n" + str(value) if column == 0 else str(value)))
        errors = sum(len(snapshot["errors"]) for snapshot in snapshots)
        self.status.setText("Выберите кластеры." if not selected else "Чтение снимков…" if waiting else
                            f"Кластеров: {len(snapshots)} · ресурсов: {len(records)}" + (f" · ошибок: {errors}. Экспорт будет частичным; подробности в подсказке." if errors else ""))
        self.status.setToolTip("\n".join(
            f"{snapshot['cluster_name']} · {snapshot['server']} · " +
            datetime.fromisoformat(snapshot["captured_at"]).astimezone().strftime("%d.%m %H:%M:%S") +
            ("\n"+"\n".join(snapshot["errors"]) if snapshot["errors"] else " · Готово") for snapshot in snapshots))
        self.table.set_empty_text("Выберите кластеры для чтения снимка." if not selected else
                                  "Чтение снимков…" if waiting else "Не удалось прочитать ресурсы. Подробности в подсказке над таблицей." if errors else
                                  "Deployment и CronJob в выбранных кластерах не найдены.")
        self.export_button.setEnabled(bool(selected) and not waiting and not self.exporting and
                                      any(snapshot["namespace_count"] is not None for snapshot in snapshots))
        self.table.resizeColumnsToContents()

    def export(self, format_name="csv"):
        if not self.export_button.isEnabled():
            return
        rows = tuple(tuple(self.table.item(row, column).text() for column in range(self.table.columnCount()))
                     for row in range(self.table.rowCount()) if not self.table.topLevelItem(row).isHidden())
        name, _ = QFileDialog.getSaveFileName(self, "Экспортировать таблицу Dumps", f"cluster-dump-{datetime.now():%Y-%m-%d_%H-%M-%S}.{format_name}", f"{format_name.upper()} (*.{format_name})")
        if not name:
            return
        destination = Path(name)
        if destination.suffix.lower() != "."+format_name:
            destination = Path(str(destination)+"."+format_name)
            if destination.exists() and QMessageBox.question(self, "Заменить файл?", str(destination),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
        self.exporting = True
        self.clusters.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.render()
        def done(paths):
            self.exporting = False
            self.clusters.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.render()
            self.status.setText(f"Сохранена таблица: {paths[0]} · строк: {len(rows)}")
        def failed(message):
            self.exporting = False
            self.clusters.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.render()
            QMessageBox.warning(self, "Не удалось экспортировать дамп", message)
        if self.window.background.submit(lambda: export_dump(destination, rows), done, failed) is False:
            failed("Не удалось начать экспорт.")


class VersionsPage(InventoryPage):
    def __init__(self, window, service):
        super().__init__(window, service, "Сверка версий")
        self.label_keys = None
        self.available_labels = []
        self.namespaces = NamespacePicker()
        self.namespaces.changed.connect(self.render)
        self.controls.addWidget(self.namespaces)
        self.labels_button = QPushButton("Labels ▾")
        self.labels_button.clicked.connect(self.choose_labels)
        self.controls.addWidget(self.labels_button)
        self.controls.addStretch()
        self.match_legend = QLabel("● Совпадает")
        self.different_legend = QLabel("● Различается / отсутствует")
        self.controls.addWidget(self.match_legend)
        self.controls.addWidget(self.different_legend)
        self.controls.removeWidget(self.refresh_button)
        self.controls.addWidget(self.refresh_button)
        self.table.setWordWrap(True)
        self.table.setItemDelegate(LabelComparisonDelegate(self.table))
        self.table.set_empty_text("Микросервисы сопоставляются по app.kubernetes.io/name.")

    def choose_labels(self):
        menu = MultiSelectMenu(self)
        menu.setToolTipsVisible(True)
        for key in sorted(set(self.available_labels) | set(self.label_keys or [])):
            action = menu.addAction(key if key in self.available_labels else key+" (нет в данных)")
            action.setToolTip(key)
            action.setCheckable(True)
            action.setChecked(key in (self.label_keys or []))
            def toggle(checked, key=key):
                selected = set(self.label_keys or [])
                selected.add(key) if checked else selected.discard(key)
                self.label_keys = sorted(selected)
                self.render()
            action.triggered.connect(toggle)
        if not self.available_labels:
            menu.addAction("Нет меток для сверки").setEnabled(False)
        try:
            menu.exec_(self.labels_button.mapToGlobal(self.labels_button.rect().bottomLeft()))
        finally:
            menu.deleteLater()

    def render(self):
        if not self.isVisible():
            return
        snapshots = self.snapshots()
        all_scopes = namespace_snapshots(snapshots)
        if self.namespaces.selected is not None:
            available = {scope["scope_id"] for scope in all_scopes}
            for snapshot in snapshots:
                for cid, namespace in sorted(self.namespaces.selected):
                    if cid == snapshot["cluster_id"] and (cid, namespace) not in available:
                        all_scopes.append(dict(snapshot, scope_id=(cid, namespace), namespace=namespace,
                            scope_name=f"{snapshot['cluster_name']} / {namespace}", records=[],
                            errors=snapshot["errors"] or ["Namespace больше не найден в кластере."]))
        self.namespaces.update_choices(all_scopes)
        scopes = [scope for scope in all_scopes if scope["scope_id"] in self.namespaces.ids()]
        waiting = self.waiting()
        unresolved = any(snapshot["namespace_count"] is None for snapshot in snapshots)
        self.available_labels = sorted({key for scope in scopes for record in scope["records"] for key in record["labels"] if key != APP_LABEL})
        if self.label_keys is None and self.available_labels:
            self.label_keys = next(([key] for key in ("app.kubernetes.io/version", "version", "GIT_BRANCH", "vtb.ru/branch") if key in self.available_labels), [])
        keys = self.label_keys or []
        self.labels_button.setText(f"Labels: {len(keys)} ▾")
        self.labels_button.setToolTip("\n".join(keys) or ("Выберите метки, значения которых нужно сравнить между namespace."
                                                       if self.available_labels else "Нет меток версий в полученных ресурсах."))
        rows, skipped = compare_versions(scopes, keys)
        colors = comparison_colors(self.window.preferences.get("theme") == "light")
        self.match_legend.setStyleSheet(f"color:{colors['match']};")
        self.different_legend.setStyleSheet(f"color:{colors['different']};")
        self.table.setColumnCount(len(scopes)+1)
        self.table.setHorizontalHeaderLabels(["Микросервис"]+[scope["scope_name"] for scope in scopes])
        self.table.setRowCount(len(rows))
        for row, data in enumerate(rows):
            self.table.setItem(row, 0, cell(data["service"], f"{APP_LABEL}={data['service']}"))
            state = "Неполные данные" if waiting or unresolved else "Нет меток версий" if not keys and not self.available_labels else data["status"]
            service = self.table.item(row, 0)
            service.setToolTip(str(service.data(Qt.ItemDataRole.ToolTipRole) or "") + "\n" + state + ": " + COMPARISON_DETAILS[state])
            service.setData(Qt.ItemDataRole.UserRole, state)
            for column, scope in enumerate(scopes, 1):
                text, detail = data["cells"][scope["scope_id"]]
                self.table.setItem(row, column, cell(text, detail))
            tone = "match" if state == "Совпадает" else "different" if state in ("Различается", "Отсутствует") else None
            if tone:
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    item.setData(Qt.ItemDataRole.ForegroundRole, QColor(colors[tone]))
            if not waiting and not unresolved:
                line_colors = [colors["match"] if data["label_statuses"][key] == "Совпадает" else
                               colors["different"] if data["label_statuses"][key] in ("Различается", "Отсутствует") else "" for key in keys]
                for column in range(1, self.table.columnCount()):
                    if self.table.item(row, column).text() not in ("Отсутствует", "Нет данных"):
                        self.table.item(row, column).setData(LABEL_COLORS, line_colors)
        different = sum(data["status"] in ("Различается", "Отсутствует") for data in rows)
        errors = [f"{snapshot['cluster_name']}: {error}" for snapshot in snapshots for error in snapshot["errors"]]
        self.status.setText("Чтение снимков…" if waiting else
                            f"Сопоставление: {APP_LABEL} · сервисов: {len(rows)} · различий: {different} · без метки приложения: {len(skipped)}" +
                            f" · namespace: {len(scopes)}"+
                            (" · выберите минимум два namespace" if len(scopes) < 2 else "")+
                            (f" · ошибок чтения: {len(errors)}; данные неполные" if errors else ""))
        self.status.setToolTip("\n".join(errors+skipped))
        self.table.resizeColumnsToContents()
