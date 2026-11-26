"""Audit UI regressions: no saved user data and no real transport."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from PyQt5.QtCore import QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from helpers_background import DeferredBackend
from quaber.connection_state import ConnectionState
from quaber.storage import UserStore
from quaber.ui import ClusterDialog, ResourceDialog
from quaber.request_policy import credentials_revision
from test_storage import CONFIG
import test_perf_matrix as fixture


class AuditMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.MatrixTests.setUpClass()
        cls.app = fixture.MatrixTests.app
    setUp = fixture.MatrixTests.setUp
    tearDown = fixture.MatrixTests.tearDown

    def test_namespace_switch_accepts_response_and_rejects_older_cluster(self):
        window = self.window
        window.tabs.setCurrentWidget(window.configmaps_page)
        cluster, namespace, table = window.configmap_controls
        cluster.blockSignals(True)
        cluster.addItem('A', 'a')
        cluster.addItem('B', 'b')
        cluster.setCurrentIndex(0)
        cluster.blockSignals(False)
        namespace.blockSignals(True)
        namespace.addItem('old')
        namespace.blockSignals(False)
        plans = []
        def submit(fn, done=None, failed=None, **options):
            plans.append((done, options))
            return True
        with patch.object(window.background, 'submit', side_effect=submit):
            cluster.setCurrentIndex(1)
            first = plans[-1][0]
            first(['new-b'])
            self.assertEqual(namespace.currentText(), 'new-b')
            cluster.setCurrentIndex(0)
            last = plans[-1][0]
            first(['stale-b'])
            self.assertEqual(namespace.count(), 0)
            last(['new-a'])
            self.assertEqual(namespace.currentText(), 'new-a')

    def test_deleted_connection_and_selected_counter_do_not_survive_replacement(self):
        window = self.window
        scope = window.snapshot['c']
        window.snapshot = {'a': copy.deepcopy(scope), 'deleted': copy.deepcopy(scope)}
        window.store.clusters = lambda: [
            {'id': 'a', 'name': 'A', 'enabled': True},
            {'id': 'new', 'name': 'New', 'enabled': True}]
        window.views.active.update(selectedClusterIds=['a', 'deleted'], knownClusterIds=['a', 'deleted'])
        window.views.active['namespaceOrder'] = ['a/ns', 'deleted/ns']
        ConnectionState(window).reconcile()
        self.assertNotIn('deleted', window.snapshot)
        self.assertEqual(window.views.active['selectedClusterIds'], ['a', 'new'])
        self.assertNotIn('deleted', window.views.active['knownClusterIds'])
        self.assertEqual(window.views.active['namespaceOrder'], ['a/ns'])
        self.assertIn('1', window.scope_buttons['selectedClusterIds'][1].text())  # New connection has no displayed snapshot yet.

    def test_resource_controls_reload_namespace_after_selected_connection_is_removed(self):
        window = self.window
        window.tabs.setCurrentWidget(window.configmaps_page)
        cluster, namespace, table = window.configmap_controls
        cluster.blockSignals(True)
        cluster.addItem('Deleted', 'deleted')
        cluster.setCurrentIndex(0)
        cluster.blockSignals(False)
        namespace.blockSignals(True)
        namespace.addItem('old-namespace')
        namespace.blockSignals(False)
        window.store.clusters = lambda: [{'id': 'new', 'name': 'New', 'enabled': True}]
        with patch.object(window.background, 'submit', return_value=True) as submit:
            window._refresh_resource_controls('configmap')
            self.assertEqual(cluster.currentData(), 'new')
            self.assertEqual(namespace.count(), 0)
            self.assertEqual(submit.call_args.kwargs['key'], ('namespaces', 'configmap'))
        window.store.clusters = lambda: []
        window._refresh_resource_controls('configmap')
        self.assertEqual(table.rowCount(), 0)
        self.assertIn('Нет активных', table.state_label.text())

    def test_new_connection_does_not_broaden_an_explicit_narrow_scope(self):
        view = self.window.views.active
        view.update(selectedClusterIds=['a'], knownClusterIds=['a', 'b'])
        self.window.views.reconcile_connections({'a', 'b', 'new'})
        self.assertEqual(view['selectedClusterIds'], ['a'])
        self.assertEqual(view['knownClusterIds'], ['a', 'b', 'new'])

    def test_cronjob_is_one_status_marker_and_never_recent(self):
        window = self.window
        scope = window.snapshot['c']['namespaces']['ns']
        cron = dict(metadata=dict(name='nightly', uid='cj', namespace='ns'), spec=dict(schedule='*/5 * * * *', timeZone='UTC'))
        job = dict(metadata=dict(name='run', uid='job', creationTimestamp='2026-11-22T01:00:00Z',
                   ownerReferences=[dict(kind='CronJob', uid='cj', controller=True)]), status={})
        pod = dict(metadata=dict(name='batch', uid='batch', ownerReferences=[dict(kind='Job', uid='job', controller=True)]),
                   status={'phase': 'Running'})
        scope.update(cron_jobs=[cron], jobs=[job], pods=[self.pod, pod])
        for phase, conditions, expected in (
            ('Running', [], 'Running'), ('Pending', [], 'Pending'),
            ('Failed', [dict(type='Failed', status='True')], 'Failed'),
            ('Succeeded', [dict(type='Complete', status='True')], 'Completed')):
            pod['status']['phase'] = phase
            job['status'] = dict(active=1 if phase == 'Running' else 0, conditions=conditions)
            window.render_matrix()
            row = window._row_names.index('CronJob/nightly')
            visuals = window.matrix.cell_visuals(row, 1)
            self.assertEqual(len(visuals), 1)
            self.assertEqual(visuals[0].identity[1], 'cronjob')
            self.assertEqual(visuals[0].status, expected)
            self.assertFalse(visuals[0].recent)

    def test_partial_core_snapshot_does_not_invent_job_row(self):
        from quaber.resource_index import index_scope
        scope = {'_ownership_pending': True, 'pods': [
            dict(metadata=dict(name='batch', uid='p', ownerReferences=[dict(kind='Job', uid='unknown-job', controller=True)]))]}
        self.assertEqual(index_scope(scope)[0], {})
        scope.pop('_ownership_pending')
        self.assertIn('Job/', next(iter(index_scope(scope)[0])))


class AuditSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_optional_timezone_roundtrip_and_form(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory)/'UserData')
            record = store.put_cluster('C', 'https://example.test:6443', 'alice', CONFIG)
            before = record['encryptedKubeconfig']
            def save_zone(value):
                return store.put_cluster('C', record['server'], 'alice', None,
                                         cluster_id=record['id'], controller_timezone=value)
            save_zone('UTC+04:00')
            saved = store.clusters()[0]
            self.assertEqual(saved['controllerTimeZone'], 'UTC+04:00')
            self.assertEqual(saved['encryptedKubeconfig'], before)
            parent = QWidget()
            dialog = ClusterDialog(parent, store, Path(directory), saved)
            self.assertEqual(dialog.controller_timezone.text(), 'UTC+04:00')
            dialog.reject()
            parent.deleteLater()
            save_zone('')
            self.assertEqual(store.clusters()[0]['controllerTimeZone'], '')
            with self.assertRaises(ValueError):
                save_zone('Unknown/Zone')
            saved['controllerTimeZone'] = 'Europe/Samara'
            store.save_clusters([saved])
            self.assertEqual(store.clusters()[0]['controllerTimeZone'], '')
            self.assertEqual(store.clusters()[0]['encryptedKubeconfig'], before)


    def test_yaml_reload_preserves_draft_on_failure_and_does_not_adopt_old_callbacks(self):
        parent = QWidget()
        parent.preferences, parent.store = {'read_only': True}, object()
        api, backend = Mock(), DeferredBackend()
        api.cluster.return_value = {'name': 'Fixture', 'enabled': True}
        old = {'metadata': {'name': 'cm', 'uid': 'u', 'resourceVersion': '1'}, 'data': {'key': 'old'}}
        panel = ResourceDialog(parent, api, backend, 'c', 'configmap', 'ns', 'cm', old)
        try:
            panel.tabs.setCurrentWidget(panel.yaml_editor)
            initial = backend.jobs[-1]
            backend.finish(execute=True, job_id=initial.id)
            panel.yaml_editor.editor.insertPlainText('# keep my draft\n')
            draft = panel.yaml_editor.toPlainText()
            with patch('quaber.ui.confirm', return_value=True):
                panel.reload_yaml()
            job = backend.jobs[-1]
            self.assertEqual(panel.yaml_editor.toPlainText(), draft)
            backend.finish(failure='offline', job_id=job.id)
            self.assertEqual(panel.yaml_editor.toPlainText(), draft)
            self.assertEqual(panel.version, '1')
            fresh = copy.deepcopy(old)
            fresh['metadata']['resourceVersion'] = '2'
            fresh['data']['key'] = 'new'
            with patch('quaber.ui.confirm', return_value=True):
                panel.reload_yaml()
            backend.finish(fresh, job_id=backend.jobs[-1].id)
            backend.deliver_stale(initial, value='obsolete YAML')
            self.assertEqual(panel.version, '2')
            self.assertIn('new', panel.yaml_editor.toPlainText())
            self.assertNotIn('obsolete', panel.yaml_editor.toPlainText())
        finally:
            panel.yaml_editor.editor.document().setModified(False)
            parent.deleteLater()
