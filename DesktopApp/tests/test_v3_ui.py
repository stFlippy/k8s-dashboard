import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import unittest
from unittest.mock import Mock, patch
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QWidget, QTableWidgetItem
from quaber.ui import ResourceDialog
from quaber.resource_list import ResourceList
from quaber.resource_actions import ResourceActions
from quaber.table_batch import batch_table


from helpers_background import DeferredBackend as Deferred


class UIRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_reload_and_update_resolve_real_controls_and_old_delivery(self):
        for readonly in (False, True):
            parent = QWidget()
            parent.preferences, parent.store = {'read_only': readonly}, object()
            api, bg = Mock(), Deferred()
            api.cluster.return_value = {'name': 'Synthetic'}
            old = {'metadata': {'name': 'cm', 'uid': 'u', 'resourceVersion': '1'}, 'data': {'key': 'old'}}
            panel = ResourceDialog(parent, api, bg, 'c', 'configmap', 'ns', 'cm', old)
            try:
                panel.tabs.setCurrentWidget(panel.yaml_editor)
                fn, done, failed = bg.jobs[-1]
                fresh = copy.deepcopy(old)
                fresh['data']['key'], fresh['metadata']['resourceVersion'] = 'new', '2'
                panel._adopt_live(fresh)
                panel.reload_yaml()
                bg.finish(fresh, job_id=bg.jobs[-1].id)
                done(fn())
                failed('obsolete')
                self.assertIn('new', panel.yaml_editor.toPlainText())
                self.assertEqual(panel.version, '2')
                self.assertEqual(panel.yaml_editor.editor.isReadOnly(), readonly)
                self.assertEqual(panel.yaml_save.isEnabled(), not readonly)
                panel._updated(fresh, notify=False)
                self.assertEqual(panel.yaml_editor.editor.isReadOnly(), readonly)
                self.assertEqual(panel.yaml_save.isEnabled(), not readonly)
            finally:
                panel.background.active = False
                parent.deleteLater()

    def test_filtered_current_is_not_restored_or_actionable(self):
        table = ResourceList()
        table.setColumnCount(2)
        table.setRowCount(1)
        table.setItem(0, 0, QTableWidgetItem('Running'))
        cell = QTableWidgetItem('pod')
        cell.setData(Qt.UserRole, ('c', 'pod', 'ns', 'pod', 'uid'))
        table.setItem(0, 1, cell)
        table.set_filter('Running')
        table.selectRow(0)
        @batch_table(lambda: table, identity_column=1)
        def refresh():
            table.setItem(0, 0, QTableWidgetItem('Failed'))
            table.apply_filter()
        refresh()
        self.assertTrue(table.topLevelItem(0).isHidden())
        self.assertEqual(table.currentRow(), -1)
        self.assertIsNone(table.currentItem())
        self.assertEqual(table.selectedItems(), [])
        table.deleteLater()

    def test_replica_arrows_only_create_an_explicit_apply_draft(self):
        parent, api, bg = QWidget(), Mock(), Deferred()
        parent.preferences = {'read_only': False}
        api.cluster.return_value = {'name': 'Synthetic'}
        resource = {'metadata': {'name': 'd', 'uid': 'u', 'resourceVersion': '1'},
                    'spec': {'replicas': 3}, 'status': {'replicas': 3}}
        panel = ResourceDialog(parent, api, bg, 'c', 'deployment', 'ns', 'd', resource)
        try:
            jobs = len(bg.jobs)
            panel.scale_by(1)
            self.assertEqual(panel._scaling_target, 4)
            self.assertIn('Replicas: 3', panel.scale_apply.text())
            self.assertFalse(panel.scale_delay_timer.isActive())
            self.assertFalse(panel.scale_countdown_timer.isActive())
            self.assertEqual(len(bg.jobs), jobs)
            api.scale.assert_not_called()
            panel._cancel_scale_draft()
            self.assertIsNone(panel._scaling_target)
            api.scale.assert_not_called()
            panel.scale_by(-3)
            self.assertEqual(panel._scaling_target, 0)
            with patch('quaber.ui.confirm', return_value=False) as confirm:
                panel._commit_scale_draft()
                confirm.assert_called_once()
            self.assertEqual(panel._scaling_target, 0)
            self.assertEqual(len(bg.jobs), jobs)
            api.scale.assert_not_called()
            panel._cancel_scale_draft()
            api.cluster.return_value['username'] = 'new-credentials'
            parent.set_read_only = lambda value: parent.preferences.__setitem__('read_only', value)
            with patch('quaber.ui.error') as message:
                panel._mutate(lambda: api.scale(), lambda _: None, action='scale', parameters={'replicas': 4})
                self.assertIn('Подключение изменилось', message.call_args.args[1])
            self.assertTrue(parent.preferences['read_only'])
            self.assertEqual(len(bg.jobs), jobs)
            api.scale.assert_not_called()
        finally:
            panel.background.active = False
            parent.deleteLater()
