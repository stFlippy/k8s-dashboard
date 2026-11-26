import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtTest import QTest
from quaber.background import Background, PanelBackground
from quaber.monitoring import MonitoringState
from test_v3_requests import fake_api


class DemandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate):
        deadline = time.monotonic()+3
        while not predicate() and time.monotonic() < deadline: QTest.qWait(2)
        self.assertTrue(predicate())

    def test_application_filter_detaches_when_window_is_destroyed(self):
        from PyQt5.QtCore import QCoreApplication, QEvent
        from quaber.ui import MainWindow
        from test_perf_matrix import MemoryStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('quaber.ui.locate_kubectl', return_value=root/'unused.exe'), \
                 patch.object(MainWindow, '_prompt_expired_login'), patch.object(MainWindow, '_load_initial_matrix'):
                window = MainWindow(MemoryStore(root), root)
            actions = window.shell_actions
            window.close()
            window._cleanup_thread.join(5)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertTrue(actions._detached)
            other = QWidget()
            self.assertFalse(actions.eventFilter(other, QEvent(QEvent.KeyPress)))
            other.deleteLater()

    def test_tab_cancel_stops_next_physical_page_and_keeps_card_attributes(self):
        api, clock, clusters = fake_api()
        state = MonitoringState()
        api.monitoring = state
        owner = QWidget()
        owner.attributes = {'uid': 'unchanged', 'status': 'Running'}
        background = Background(owner)
        background.monitoring = SimpleNamespace(token=state.token, valid=state.allows,
            read_scope=state.read_scope, reconnected=state.reconnected)
        panel = PanelBackground(background, owner)
        entered, release = threading.Event(), threading.Event()
        delivered = []
        def route(req):
            if len(api.opener.calls) == 1:
                entered.set()
                release.wait(3)
                return 200, {}, {'items': [], 'metadata': {'continue': 'next-synthetic'}}
            return 200, {}, {'items': []}
        api.opener.route = route
        panel.submit(lambda: api.list_pages('c0', '/api/v1/pods'), delivered.append, delivered.append, demand='tab')
        try:
            self.wait(entered.is_set)
            panel.cancel_reads(tab_only=True)
        finally:
            release.set()
        self.wait(lambda: not background.running)
        self.assertEqual(len(api.opener.calls), 1)
        self.assertEqual(delivered, [])
        self.assertEqual(owner.attributes, {'uid': 'unchanged', 'status': 'Running'})
        panel.submit(lambda: api.request('c1', '/api/v1/pods'), delivered.append, delivered.append)
        self.wait(lambda: not background.running)
        self.assertEqual(delivered, [{'items': []}])
        self.assertEqual(len(api.opener.calls), 2)
        background.stop()
        owner.deleteLater()

    def test_inventory_hidden_cancels_after_response_without_followup_or_stale_delivery(self):
        from quaber.inventory import InventoryService
        api, clock, clusters = fake_api()
        for cluster in clusters: cluster['server'] = 'https://synthetic.invalid'
        state = MonitoringState()
        api.monitoring = state
        window = QWidget()
        window.api, window._closing = api, False
        window.monitoring = SimpleNamespace(policy=state, token=state.token, valid=state.allows,
            read_scope=state.read_scope, reconnected=state.reconnected)
        window.background = Background(window)
        window.background.monitoring = window.monitoring
        window.store = SimpleNamespace(clusters=lambda: clusters)
        service = InventoryService(window)
        service.set_demanded(True)
        entered, release = threading.Event(), threading.Event()
        original = api.opener.route
        def route(req):
            entered.set()
            release.wait(3)
            return original(req)
        api.opener.route = route
        service.refresh(['c0'], force=True)
        try:
            self.wait(entered.is_set)
            service.set_demanded(False)
            self.assertFalse(service.loading)
        finally:
            release.set()
        self.wait(lambda: not window.background.running)
        self.assertEqual(len(api.opener.calls), 1)
        self.assertFalse(service.snapshots)
        service.refresh(['c0'], force=True)
        self.assertFalse(window.background.running)
        self.assertEqual(len(api.opener.calls), 1)
        window.background.stop()
        window.deleteLater()

    def test_generic_panel_hide_cancels_physical_continuation(self):
        api, clock, clusters = fake_api()
        state = MonitoringState()
        api.monitoring = state
        owner = QWidget()
        owner.show()
        self.app.processEvents()
        background = Background(owner)
        background.monitoring = SimpleNamespace(token=state.token, valid=state.allows,
            read_scope=state.read_scope, reconnected=state.reconnected)
        panel = PanelBackground(background, owner)
        entered, release = threading.Event(), threading.Event()
        delivered = []
        def route(req):
            entered.set()
            release.wait(3)
            return 200, {}, {'items': [], 'metadata': {'continue': 'synthetic-next'}}
        api.opener.route = route
        panel.submit(lambda: api.list_pages('c0', '/api/v1/pods'), delivered.append, delivered.append)
        try:
            self.wait(entered.is_set)
            owner.hide()
        finally:
            release.set()
        self.wait(lambda: not background.running)
        self.assertEqual(len(api.opener.calls), 1)
        self.assertEqual(delivered, [])
        background.stop()
        owner.deleteLater()
