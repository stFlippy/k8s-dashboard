"""Visible-paint and task lifetime regressions, not merely populated models."""
import threading
import time
import unittest
from unittest.mock import patch
from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtGui import QFont
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QTabWidget, QWidget, QVBoxLayout
from quaber.background import Background
from quaber.loading_status import LoadingStatus
from quaber.resource_list import ResourceList
from quaber.table_batch import batch_table
import test_inventory_lifecycle


def wait(condition):
    deadline = time.monotonic() + 4
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(5)
    if not condition():
        raise AssertionError('Qt condition did not become true')


class Paints(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Paint:
            self.count += 1
        return False


class TableBatchRepaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_hidden_tab_can_paint_after_batch_and_nested_exception_preserves_explicit_disable(self):
        tabs = QTabWidget()
        tabs.addTab(QWidget(), 'First')
        page = QWidget()
        table = ResourceList()
        QVBoxLayout(page).addWidget(table)
        tabs.addTab(page, 'Table')
        tabs.show()
        self.app.processEvents()
        painter = Paints(table.viewport())
        @batch_table(lambda: table)
        def update():
            table.setColumnCount(1)
            table.setHorizontalHeaderLabels(['Name'])
            table.setRowCount(1)
        try:
            # A hidden Qt page implicitly disables painting, but this is not a
            # caller's explicit request to leave the table disabled.
            update()
            self.assertFalse(table.testAttribute(Qt.WA_ForceUpdatesDisabled))
            tabs.setCurrentWidget(page)
            wait(lambda: painter.count > 0)
            self.assertTrue(table.updatesEnabled())
            self.assertTrue(table.viewport().updatesEnabled())
            table.setUpdatesEnabled(False)
            @batch_table(lambda: table)
            def outer():
                update()
                raise ValueError('synthetic rendering failure')
            with self.assertRaises(ValueError):
                outer()
            self.assertFalse(table.updatesEnabled())
            self.assertTrue(table.testAttribute(Qt.WA_ForceUpdatesDisabled))
        finally:
            tabs.close()
            tabs.deleteLater()
            self.app.processEvents()


class LoadingStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_running_coalesced_queue_failure_and_rejection_release_indicator(self):
        owner = QWidget()
        background = Background(owner)
        status = LoadingStatus(background, owner)
        QVBoxLayout(owner).addWidget(status)
        owner.show()
        release = threading.Event()
        failures = []
        try:
            background.submit(lambda: release.wait(3), key='one', label='Квоты ресурсов')
            background.submit(lambda: 1, key='one', label='Повтор квот')
            self.assertFalse(status.isHidden())
            self.assertIn('в очереди: 1', status.toolTip())
            self.assertEqual(background.activities()[1], ('Повтор квот',))
            release.set()
            wait(lambda: not background.running and not background._pending)
            self.assertTrue(status.isHidden())
            def fail():
                raise ValueError('synthetic failure')
            background.submit(fail, failed=failures.append, label='Чтение данных')
            wait(lambda: bool(failures))
            self.assertTrue(status.isHidden())
            background.stop()
            self.assertFalse(background.submit(lambda: 1))
            self.assertTrue(status.isHidden())
        finally:
            release.set()
            background.stop()
            background.pool.waitForDone(4000)
            self.app.processEvents()
            background._collect_retired()
            owner.close()
            owner.deleteLater()
            self.app.processEvents()

    def test_spinner_stops_hidden_or_minimized_and_restarts_on_restore(self):
        owner = QWidget()
        background = Background(owner)
        status = LoadingStatus(background, owner)
        QVBoxLayout(owner).addWidget(status)
        release = threading.Event()
        try:
            owner.show()
            background.submit(lambda: release.wait(3), monitor=False)
            self.app.processEvents()
            self.assertTrue(status.timer.isActive())
            owner.hide()
            self.assertFalse(status.timer.isActive())
            owner.show()
            self.app.processEvents()
            self.assertTrue(status.timer.isActive())
            owner.showMinimized()
            self.app.processEvents()
            self.assertFalse(status.timer.isActive())
            owner.showNormal()
            self.app.processEvents()
            self.assertTrue(status.timer.isActive())
            release.set()
            wait(lambda: not background.running)
            self.assertFalse(status.timer.isActive())
        finally:
            release.set()
            background.stop()
            background.pool.waitForDone(4000)
            self.app.processEvents()
            background._collect_retired()
            owner.close()
            owner.deleteLater()
            self.app.processEvents()


class DumpsVisibleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_inventory_lifecycle.InventoryLifecycleTests.setUpClass()

    def setUp(self):
        self.fixture = test_inventory_lifecycle.InventoryLifecycleTests('test_physical_success_loads_rows_and_clears_operation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.window = self.fixture.window

    def test_dumps_really_paints_after_first_open_and_hidden_refresh(self):
        f = self.fixture
        page = self.window.dumps_page
        painter = Paints(page.table.viewport())
        f.open_dumps()
        f.wait(lambda: f.service.get('c0') is not None and not f.service.loading)
        wait(lambda: painter.count > 0)
        self.assertEqual(page.table.rowCount(), 1)
        self.assertTrue(page.table.updatesEnabled())
        self.assertTrue(page.table.header().updatesEnabled())
        for unused in range(3):
            self.window.tabs.setCurrentWidget(self.window.matrix_page)
            page.render()
            self.window.tabs.setCurrentWidget(page)
            f.app.processEvents()
            self.assertTrue(page.table.viewport().updatesEnabled())
        self.assertFalse(page.table.visualRect(page.table.model().index(0, 0)).isEmpty())


    def test_cancelled_quota_load_releases_busy_state_and_can_retry(self):
        from quaber.monitoring import MonitoringCancelled
        f = self.fixture
        with patch.object(self.window.api, 'namespaces', side_effect=MonitoringCancelled('synthetic pause')):
            self.window.tabs.setCurrentWidget(self.window.quotas_page)
            self.window.load_quotas()
            wait(lambda: not getattr(self.window, '_loading_quotas', False))
        self.assertIn('приостановлено', self.window.quota_status.text())
        self.window.load_quotas()
        wait(lambda: not self.window._loading_quotas)
        self.assertIn('загружены', self.window.quota_status.text())
