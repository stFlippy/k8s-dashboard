"""Lazily loaded, UID-owned CronJob Pods and Jobs."""
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from .resource_list import ResourceList
from .cron_history import owned_by, job_order
from .polling import polling_allowed, mark_poll_timer
from .read_lifecycle import register_read_owner


class CronJobResources(QWidget):
    def __init__(self, owner, kind):
        super().__init__(owner)
        self.owner, self.kind = owner, kind
        self.loading, self.generation, self.revision = False, 0, None
        register_read_owner(owner, self)
        box = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.table = ResourceList()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(['Имя', 'Создан', 'Статус'])
        self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.open_row)
        box.addWidget(self.table, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start()
        mark_poll_timer(self.timer)

    def showEvent(self, event):
        super().showEvent(event)
        if not event.spontaneous(): QTimer.singleShot(0, self.refresh_data)

    def suspend_reads(self):
        self.generation += 1
        self.loading = False

    def refresh_data(self):
        if self.loading or not self.isVisible() or not polling_allowed(self): return
        self.loading = True
        self.generation += 1
        generation = self.generation
        owner = self.owner
        uid = owner.resource.get('metadata', {}).get('uid')
        def read():
            jobs = [j for j in owner.api.list_resources(owner.cluster_id, 'job', owner.namespace) if owned_by(j, 'CronJob', uid)]
            if self.kind == 'job': return sorted(jobs, key=job_order, reverse=True)
            identifiers = {j['metadata']['uid'] for j in jobs}
            pods = owner.api.pods(owner.cluster_id, owner.namespace) if jobs else []
            return sorted((p for p in pods if any(o.get('controller') and o.get('kind') == 'Job' and o.get('uid') in identifiers
                          for o in p.get('metadata', {}).get('ownerReferences', []))), key=job_order, reverse=True)
        def done(resources):
            if generation != self.generation: return
            from .ui import item
            from .widgets import pod_status
            rows = []
            for resource in resources:
                meta, state = resource.get('metadata', {}), resource.get('status', {})
                status = pod_status(resource) if self.kind == 'pod' else ('Succeeded' if state.get('succeeded') else 'Failed' if state.get('failed') else 'Running')
                rows.append((meta.get('name', ''), meta.get('creationTimestamp', '—'), status, meta.get('uid')))
            self.status.setText('' if rows else 'Объекты ещё не созданы или уже удалены.')
            if rows == self.revision: return
            selected = self.table.currentItem()
            selected_uid = selected.data(0, Qt.UserRole)[1] if selected is not None else None
            self.table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values[:3]): self.table.setItem(row, column, item(value))
                self.table.item(row, 0).setData(Qt.UserRole, (values[0], values[3]))
                if values[3] == selected_uid: self.table.setCurrentItem(self.table.topLevelItem(row))
            self.table.resizeColumnsToContents()
            self.revision = rows
        def failed(message):
            if generation == self.generation: self.status.setText(message)
        def finalized(_):
            if generation == self.generation: self.loading = False
        if owner.background.submit(read, done, failed, demand='tab', finalized=finalized) is False: self.loading = False

    def open_row(self, row, column):
        cell = self.table.item(row, 0)
        if cell is None: return
        name, uid = cell.data(Qt.UserRole)
        self.owner.controller.open_resource(self.owner.cluster_id, self.kind, self.owner.namespace, name, expected_uid=uid)
