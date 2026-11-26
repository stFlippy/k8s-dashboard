import time
import threading
from unittest.mock import patch
from PyQt5.QtTest import QTest
from PyQt5.QtCore import QTimer
from quaber.polling import mark_poll_timer
import unittest
import helpers_matrix as fixture
from test_v3_requests import fake_api


class MainDispatchTests(fixture.MatrixFixture, unittest.TestCase):
    def test_repeated_minimize_restore_does_not_cancel_or_restart_inflight_read(self):
        w = self.window
        w.store.clusters = lambda: []
        w.show()
        self.app.processEvents()
        api, _, clusters = fake_api(cluster_count=1)
        w.api, api.monitoring = api, w.monitoring
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = api.opener.route
        def route(request):
            if len(api.opener.calls) == 1:
                entered.set()
                release.wait(3)
            return original(request)
        api.opener.route = route
        with patch.object(w.store, 'clusters', return_value=clusters):
            w.views.active.update(selectedClusterIds=['c0'], selectedNamespaces=['ns0'])
            w.refresh_matrix()
            until = time.monotonic()+2
            while not entered.is_set() and time.monotonic() < until: QTest.qWait(2)
            self.assertTrue(entered.is_set())
            epoch, generation, cancel = w.monitoring.policy.epoch, w._matrix_generation, w._matrix_cancel
            for _ in range(3):
                w.showMinimized()
                self.app.processEvents()
                w.showNormal()
                self.app.processEvents()
            self.assertEqual(w.monitoring.policy.epoch, epoch)
            self.assertEqual(w._matrix_generation, generation)
            self.assertFalse(cancel.is_set())
            self.assertEqual(len(api.opener.calls), 1)
            release.set()
            until = time.monotonic()+3
            while (w.loading_matrix or w.background.running) and time.monotonic() < until: QTest.qWait(2)
            self.assertFalse(w.loading_matrix)
            self.assertEqual(len(api.opener.calls), 9)

    def test_card_blocks_underlying_quota_and_configmap_page_producers(self):
        from PyQt5.QtWidgets import QWidget
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        panel = QWidget(w)
        w._resource_panel = panel
        try:
            for index in (1, 2):
                blocked = w.tabs.blockSignals(True)
                w.tabs.setCurrentIndex(index)
                w.tabs.blockSignals(blocked)
                existing_jobs = set(w.background.running)
                with patch.object(w, '_refresh_resource_controls', wraps=w._refresh_resource_controls) as producer, \
                     patch.object(w.background,'submit',wraps=w.background.submit) as submit:
                    w.refresh()
                    producer.assert_not_called()
                    submit.assert_not_called()
                self.assertTrue(w.background.running.issubset(existing_jobs))
        finally:
            w._resource_panel = None
            panel.deleteLater()

    def test_initial_load_on_another_page_does_not_dispatch_hidden_matrix(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        blocked = w.tabs.blockSignals(True)
        w.tabs.setCurrentIndex(1)
        w.tabs.blockSignals(blocked)
        with patch.object(w, 'refresh_matrix', wraps=w.refresh_matrix) as matrix:
            w._load_initial_matrix()
            matrix.assert_not_called()
        wanted_jobs = set(w.background.running)  # The visible quota page may read.
        with patch.object(w.store, 'clusters', return_value=[{'id': 'c', 'enabled': True}]):
            w.refresh_matrix()
        self.assertTrue(w._matrix_visual_dirty)
        self.assertEqual(w.background.running, wanted_jobs)

    def test_manual_refresh_storm_keeps_one_latest_plan_and_physical_intent(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        api, clock, clusters = fake_api(pod_count=217)
        w.api, api.monitoring = api, w.monitoring
        entered, release = threading.Event(), threading.Event()
        route = api.opener.route
        def blocked(req):
            if len(api.opener.calls) == 1:
                entered.set()
                release.wait(3)
            return route(req)
        api.opener.route = blocked
        def wait(predicate):
            until = time.monotonic()+4
            while not predicate() and time.monotonic() < until: QTest.qWait(2)
            self.assertTrue(predicate())
        with patch.object(w.store, 'clusters', return_value=clusters):
            w.views.active.update(selectedClusterIds=['c0'], selectedNamespaces=['ns0'])
            w._manual_refresh()
            try:
                wait(entered.is_set)
                for _ in range(100): w._manual_refresh()
                self.assertTrue(w._matrix_manual_pending)
                self.assertLessEqual(len(w.background._pending), 1)
            finally:
                release.set()
            wait(lambda: not w.loading_matrix and not w.background.running and
                 not w.background._pending and not w.matrix_return_timer.isActive())
        self.assertEqual(len(api.opener.calls), 18)
        self.assertEqual(api.request_policy.by_purpose, {'manual': 18})
        self.assertFalse(w._matrix_manual_pending)

    def test_credentials_change_during_first_response_discards_delivery_and_replans(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        api, clock, clusters = fake_api(4, pod_count=217)
        w.api, api.monitoring = api, w.monitoring
        entered, release = threading.Event(), threading.Event()
        original = api.opener.route
        def route(req):
            entered.set()
            release.wait(3)
            return original(req)
        api.opener.route = route
        def wait(predicate):
            until = time.monotonic()+4
            while not predicate() and time.monotonic() < until: QTest.qWait(2)
            self.assertTrue(predicate())
        with patch.object(w.store, 'clusters', return_value=clusters):
            w.views.active.update(selectedClusterIds=['c0'], selectedNamespaces=['ns0'])
            w.refresh_matrix()
            try:
                wait(entered.is_set)
                generation = w._matrix_generation
                clusters[0]['username'] = 'new-synthetic-revision'
            finally:
                release.set()
            wait(lambda: not w.loading_matrix and not w.background.running and not w.background._pending
                 and not w.matrix_return_timer.isActive())
        self.assertGreater(w._matrix_generation, generation)
        self.assertEqual(len(api.opener.calls), 10)
        self.assertEqual(set(w.snapshot), {'c0'})
        self.assertEqual(set(w.snapshot['c0']['namespaces']), {'ns0','ns1','ns2','ns3'})
        self.assertFalse(w.snapshot['c0']['namespaces']['ns0'].get('_stale'))

    def test_scope_switch_cancels_old_dispatch_and_fetches_latest_even_with_auto_off(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        api, clock, clusters = fake_api(4, pod_count=217)
        w.api, api.monitoring = api, w.monitoring
        entered, release = threading.Event(), threading.Event()
        original = api.opener.route
        def route(req):
            if 'c0.invalid' in req.full_url:
                entered.set()
                release.wait(3)
            return original(req)
        api.opener.route = route
        def wait(predicate):
            until = time.monotonic()+4
            while not predicate() and time.monotonic() < until: QTest.qWait(2)
            self.assertTrue(predicate())
        with patch.object(w.store, 'clusters', return_value=clusters):
            w.views.active.update(selectedClusterIds=['c0'], selectedNamespaces=['ns0'])
            w.refresh_matrix()
            try:
                wait(entered.is_set)
                w.views.active.update(selectedClusterIds=['c1'], selectedNamespaces=['ns1'])
                w._matrix_scope_changed()
                self.app.processEvents()
                self.assertTrue(w.background._pending)
            finally:
                release.set()
            wait(lambda: not w.loading_matrix and not w.background.running and not w.background._pending)
        old = [c for c in api.opener.calls if c['cluster'] == 'c0.invalid']
        current = [c for c in api.opener.calls if c['cluster'] == 'c1.invalid']
        self.assertEqual(len(old), 1)
        self.assertEqual(len(current), 9)
        self.assertTrue(all('/namespaces/ns1/' not in c['path'] for c in current))
        self.assertEqual(set(w.snapshot), {'c1'})
        self.assertEqual(set(w.snapshot['c1']['namespaces']), {'ns0','ns1','ns2','ns3'})
        self.assertFalse(w.timer.isActive())

    def test_refresh_off_survives_pause_restore_without_stopping_local_ttl(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        self.assertEqual(w.preferences['refresh_ms'], 0)
        local = QTimer(w)
        local.start(60000)
        network = QTimer(w)
        network.start(5000)
        mark_poll_timer(network)
        self.assertFalse(network.isActive())
        w.showMinimized()
        self.app.processEvents()
        self.assertTrue(w.monitoring.policy.allows())
        self.assertTrue(local.isActive())
        w.showNormal()
        self.app.processEvents()
        self.assertFalse(network.isActive())
        self.assertFalse(w.timer.isActive())
        self.assertTrue(local.isActive())
        local.stop()

    def test_real_mainwindow_plan_dispatches_only_selected_scope(self):
        w = self.window
        w.store.clusters = lambda: []  # Show the empty shell before installing fake API connections.
        w.show()
        self.app.processEvents()
        api, clock, clusters = fake_api(4, pod_count=217)
        w.api = api
        api.monitoring = w.monitoring
        w.snapshot = {}
        w.views.active['selectedClusterIds'] = ['c1']
        w.views.active['selectedNamespaces'] = ['ns2']
        with patch.object(w.store, 'clusters', return_value=clusters):
            w.refresh_matrix()
            deadline = time.monotonic()+3
            while (w.loading_matrix or w.background.running or w.background._pending) and time.monotonic() < deadline:
                QTest.qWait(5)
        self.assertFalse(w.loading_matrix)
        self.assertFalse(w.background.running)
        self.assertEqual(set(w.snapshot), {'c1'})
        self.assertEqual(set(w.snapshot['c1']['namespaces']), {'ns0','ns1','ns2','ns3'})
        self.assertEqual(len(api.opener.calls), 9)
        self.assertTrue(all(c['cluster'] == 'c1.invalid' and '/namespaces/ns2/' not in c['path'] for c in api.opener.calls))
        self.assertTrue(all(ns == 'ns2' for _, _, ns in w._columns))
