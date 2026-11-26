import copy
from qt_helpers import label_text
import unittest
from unittest.mock import patch
from PyQt5.QtWidgets import QLabel
from quaber.quota_card import QuotaCard
from test_storage import CONFIG
from test_ux import ShellFixture, Deferred


class PendingQuotaTests(ShellFixture, unittest.TestCase):
    def test_stalled_rollout_can_scale_to_zero_and_deployment_header_has_no_pod(self):
        panel = self.open_panel('deployment')
        self.assertTrue(panel.nav_pod.isHidden())
        panel.resource['status'].update(updatedReplicas=0, availableReplicas=0)
        self.window.set_read_only(False)
        panel.update_write_controls()
        self.assertTrue(panel.replica_down.isEnabled())
        deferred = Deferred()
        panel.background.background = deferred
        self.api.scale.return_value = copy.deepcopy(self.deployment)
        with patch.object(panel, '_updated'), patch.object(panel, 'reload_deployment_pods'), \
             patch('quaber.ui.confirm', return_value=True) as confirmation:
            panel.scale_by(-1)
            panel._commit_scale_draft()
            deferred.finish(execute=True)
        confirmation.assert_called_once()
        self.assertEqual(confirmation.call_args.args[1], 'Scale to zero')
        self.assertEqual(self.api.scale.call_count, 1)
        call = self.api.scale.call_args
        self.assertEqual(call.args, ('c', 'ns', 'app', 0, 'd', '1'))
        from quaber.write_guard import WritePermit
        self.assertIsInstance(call.kwargs['permit'], WritePermit)
        self.assertEqual(call.kwargs['permit'].intent.identity, ('c', 'deployment', 'ns', 'app', 'd'))
        self.assertEqual(call.kwargs['permit'].intent.action, 'scale')
        self.assertTrue(self.window.preferences['read_only'])

    def test_dump_replica_count_is_observed_not_desired(self):
        from quaber.inventory import inventory_record, inventory_row
        resource = copy.deepcopy(self.deployment)
        resource['status']['replicas'] = 2
        record = inventory_record(resource, 'Deployment', 'ns')
        self.assertEqual(inventory_row({'server': 'https://host'}, record)[-1], 2)
        self.assertEqual(inventory_record(resource, 'CronJob', 'ns')['replicas'], '—')

    def pending_scope(self):
        deployment = copy.deepcopy(self.deployment)
        deployment['spec']['replicas'] = 1
        deployment['status'] = {'replicas': 2, 'unavailableReplicas': 2}
        sets, pods = [], []
        for revision in (1, 2):
            uid = f'rs{revision}'
            sets.append({'metadata': {'uid': uid, 'name': uid, 'annotations': {'deployment.kubernetes.io/revision': str(revision)},
                          'ownerReferences': [{'uid': 'd', 'kind': 'Deployment', 'controller': True}]}, 'spec': {'replicas': 1}})
            pod = copy.deepcopy(self.pod)
            pod['metadata'].update(name=f'pending-{revision}', uid=f'p{revision}',
                ownerReferences=[{'kind': 'ReplicaSet', 'uid': uid, 'controller': True}])
            pod['status'] = {'phase': 'Pending'}
            pods.append(pod)
        return deployment, pods, sets

    def test_pending_counters_survive_hidden_filter_and_reappear_when_enabled(self):
        deployment, pods, sets = self.pending_scope()
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {'c': {'name': 'Lab', 'namespaces': {'ns': {
            'deployments': [deployment], 'pods': pods, 'replica_sets': sets}}}}
        self.window.status_filters['Pending'].setChecked(False)
        self.window.render_matrix()
        self.assertEqual(self.window.status_filters['Pending'].text(), 'Pending  2')
        self.assertEqual(label_text(self.window.summary_label), '2  Total Pods')
        self.assertEqual(self.window.matrix.item(0, 1).text(), 'Скрыто: 2')
        self.assertIn('Pending: 2', self.window.matrix.item(0, 1).toolTip())
        self.assertEqual(self.window.matrix_data[(0, 1)][4], [])
        self.window.status_filters['Pending'].setChecked(True)
        self.assertEqual(len(self.window.matrix_data[(0, 1)][4]), 2)
        self.assertEqual(self.window.status_filters['Pending'].text(), 'Pending  2')

    def test_two_pending_replicas_are_split_by_real_revisions_in_card(self):
        panel = self.open_panel('deployment')
        deployment, pods, sets = self.pending_scope()
        panel._displayed_pods, panel._displayed_sets = pods, sets
        panel._update_replica_target(deployment)
        self.assertTrue(panel.previous_ring.isVisible())
        self.assertEqual(panel.previous_ring.counts, {'Pending': 1})
        self.assertEqual(panel.pod_donut.counts, {'Pending': 1})
        self.assertIn('Всего Pod: 1', panel.pod_donut.toolTip())

    def test_quota_filter_reads_selected_clusters_and_discards_previous_response(self):
        store = self.window.store
        a = store.put_cluster('A', 'https://example.test:6443', 'alice', CONFIG)
        b = store.put_cluster('B', 'https://b.test:6443', 'alice', CONFIG.replace('example.test', 'b.test'))
        self.api.namespaces.return_value = ['ns']
        self.api.list_resources.return_value = [{'metadata': {'name': 'quota'}, 'status': {'hard': {'pods': '5'}, 'used': {'pods': '2'}}}]
        self.api.all_namespace_resources.return_value = [
            dict(self.api.list_resources.return_value[0], metadata={'name': 'quota', 'namespace': 'ns'})]
        deferred = Deferred()
        picker = self.window.quota_clusters
        with patch.object(self.window, 'background', deferred):
            self.window.tabs.setCurrentWidget(self.window.quotas_page)
            self.window.load_quotas()  # old selection: both
            picker.selected = {b['id']}
            picker.changed.emit()
            deferred.finish(execute=True)
            self.assertEqual(self.window.quota_layout.count(), 0)
            self.api.namespaces.reset_mock()
            deferred.finish(execute=True)
            self.api.namespaces.assert_called_once_with(b['id'])
            cards = [self.window.quota_layout.itemAt(i).widget() for i in range(self.window.quota_layout.count())]
            cards = [card for card in cards if isinstance(card, QuotaCard)]
            self.assertEqual(len(cards), 1)
            self.assertIn('B / ns', cards[0].findChild(QLabel, 'quotaTitle').text())
            self.assertIs(self.window.quota_clusters, picker)
            picker.selected = set()
            picker.changed.emit()
            self.api.namespaces.reset_mock()
            deferred.finish(execute=True)
            self.api.namespaces.assert_not_called()
            self.assertEqual(self.window.quota_layout.itemAt(0).widget().text(), 'Выберите кластеры.')
