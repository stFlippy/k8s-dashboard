from qt_helpers import label_text
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication, QCheckBox, QLabel
from PyQt5.QtCore import Qt
from quaber.matrix_counters import PARTITIONS, classify_pod, scope_summary, batch_origin
from quaber.resource_index import index_scope
from quaber.views import MatrixViews, default_view, validate_preferences, validate_view
import test_perf_matrix as fixture

NOW = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)


def obj(kind, name, owner=None, **extra):
    value = {'kind': kind, 'metadata': {'name': name, 'uid': name}}
    if owner:
        value['metadata']['ownerReferences'] = [{'kind': owner[0], 'uid': owner[1], 'name': owner[1], 'controller': True}]
    value.update(extra)
    return value


def pod(name, phase='Running', owner=None, recent=False):
    value = obj('Pod', name, owner, status={'phase': phase})
    value['metadata']['creationTimestamp'] = (NOW-timedelta(minutes=1 if recent else 60)).isoformat()
    return value


def summary(scope, selected=None, now=NOW):
    index = index_scope(scope)
    return scope_summary(scope, index, set(index[0]) if selected is None else selected, now)


class CounterTests(unittest.TestCase):
    def test_twenty_main_and_completed_cron_history(self):
        scope = {'pods': [pod(str(i)) for i in range(20)], 'jobs': [], 'cron_jobs': []}
        for i in range(2):
            cron, job = f'cron{i}', f'job{i}'
            scope['cron_jobs'].append(obj('CronJob', cron))
            scope['jobs'].append(obj('Job', job, ('CronJob', cron)))
            scope['pods'].append(pod('batch'+str(i), 'Succeeded', ('Job', job)))
        before = copy.deepcopy(scope)
        result = summary(scope)
        self.assertEqual(result['pods']['Running'], 20)
        self.assertEqual(sum(result['pods'].values()), 20)
        self.assertEqual(result['pods']['Completed'], 0)
        self.assertEqual(result['batch']['Completed'], 2)
        self.assertEqual(result['workloads']['CronJob'], 2)
        self.assertEqual(result['workloads']['Job'], 0)
        self.assertEqual(scope, before)

    def test_partition_and_age_boundary(self):
        values = [pod(str(i), recent=i < 5) for i in range(20)]
        result = summary({'pods': values})
        self.assertEqual((result['pods']['Running'], result['pods']['Recent']), (15, 5))
        self.assertEqual(sum(result['pods'].values()), 20)
        later = summary({'pods': values}, now=NOW+timedelta(minutes=30))
        self.assertEqual((later['pods']['Running'], later['pods']['Recent']), (20, 0))
        for phase, expected in [('Failed', 'Failed'), ('Pending', 'Pending'), ('Succeeded', 'Completed'),
                                ('Unknown', 'Unknown'), ('Weird', 'Other')]:
            self.assertEqual(classify_pod(pod('x', phase, recent=True), NOW), expected)
        value = pod('term', recent=True)
        value['metadata']['deletionTimestamp'] = NOW.isoformat()
        self.assertEqual(classify_pod(value, NOW), 'Terminating')

    def test_standalone_jobs_unknown_parent_cycles_and_duplicates(self):
        batch = pod('batch', 'Failed', ('Job', 'missing'))
        main = pod('main')
        scope = {'pods': [main, copy.deepcopy(main), batch, copy.deepcopy(batch)],
                 'jobs': [obj('Job', 'standalone', status={'failed': 1}), obj('Job', 'cycle', ('CronJob', 'cycle'))],
                 'cron_jobs': [obj('CronJob', 'empty', spec={})]}
        result = summary(scope)
        self.assertEqual(sum(result['pods'].values()), 1)
        self.assertEqual(result['batch']['Failed'], 1)
        self.assertEqual(result['workloads']['Job'], 1)
        self.assertEqual(result['workloads']['CronJob'], 1)
        self.assertEqual(result['batch_failed']['Job'], 1)
        self.assertIn('batch owner', result['unavailable'])
        self.assertEqual(len(index_scope(scope)[1]['main']), 1)
        self.assertEqual(batch_origin(pod('cycle', owner=('Job', 'cycle')), scope), 'Unknown Job')

    def test_workload_issues_are_not_pod_errors_and_unknown_is_not_zero(self):
        scope = {'deployments': [obj('Deployment', 'needs', spec={'replicas': 3}),
                                 obj('Deployment', 'zero', spec={'replicas': 0})],
                 'jobs': [obj('Job', 'suspended', spec={'suspend': True})],
                 '_type_errors': {'cronjob': 'synthetic forbidden'}}
        result = summary(scope)
        self.assertEqual(result['issues'], 1)
        self.assertEqual(sum(result['pods'].values()), 0)
        self.assertIn('cronjob', result['unavailable'])
        self.assertEqual(result['workloads']['Deployment'], 2)

    def test_legacy_views_import_export_migrate_recent_without_changing_scope(self):
        for recent in (True, False):
            old = dict(default_view(), recentRestartFilter=recent, selectedNamespaces=['ns'], columnWidths={'c/ns': 80})
            value = validate_view(old)
            self.assertNotIn('recentRestartFilter', value)
            self.assertEqual(value['selectedNamespaces'], ['ns'])
            self.assertEqual(value['columnWidths'], {'c/ns': 80})
            views = MatrixViews({})
            views.import_data({'format': 'quaber-matrix-views', 'version': 1, 'views': [dict(old, name='Imported')]}, [])
            exported = views.export([])
            self.assertTrue(all('recentRestartFilter' not in v for v in exported['views']))
        self.assertNotIn('recent', validate_preferences({'recent': False}))


class CounterUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self): fixture.MatrixTests.setUp(self)
    def tearDown(self): fixture.MatrixTests.tearDown(self)

    def test_recent_is_label_and_follows_running_with_old_false_view(self):
        now = datetime.now(timezone.utc)
        self.pod['metadata']['creationTimestamp'] = (now-timedelta(minutes=1)).isoformat()
        self.window.views.active['recentRestartFilter'] = False
        self.window.render_matrix()
        self.assertIsInstance(self.window.recent_label, QLabel)
        self.assertFalse(self.window.findChildren(QCheckBox, 'recentFilter'))
        self.assertEqual(self.window.status_filters['Running'].text(), 'Running  0')
        self.assertEqual(self.window.recent_label.text(), '● Recently started 1')
        self.assertEqual(label_text(self.window.summary_label), '1  Total Pods')
        self.window.status_filters['Running'].setChecked(False)
        self.assertEqual(self.window.matrix.cell_visuals(0, 1), ())
        self.assertEqual(self.window.recent_label.text(), '● Recently started 1')
        self.window.status_filters['Running'].setChecked(True)
        self.assertEqual(len(self.window.matrix.cell_visuals(0, 1)), 1)
        self.window.deploy_mode.setChecked(True)
        self.assertEqual(self.window.matrix_counts['pods']['Recent'], 1)
        self.assertEqual(len(self.window.matrix.cell_visuals(0, 1)), 1)

    def test_counter_identity_across_clusters_and_namespace_and_scope(self):
        self.window.snapshot['other'] = copy.deepcopy(self.window.snapshot['c'])
        self.window.snapshot['c']['namespaces']['ns2'] = copy.deepcopy(self.window.snapshot['c']['namespaces']['ns'])
        self.window.render_matrix()
        self.assertEqual(label_text(self.window.summary_label), '3  Total Pods')
        self.window.views.active['selectedClusterIds'] = ['c']
        self.window.views.active['selectedNamespaces'] = ['ns']
        self.window.render_matrix()
        self.assertEqual(label_text(self.window.summary_label), '1  Total Pods')
        self.window.search.setText('absent')
        self.window.render_matrix()
        self.assertEqual(label_text(self.window.summary_label), '0  Total Pods')

    def test_batch_errors_and_extra_counts_are_visible_and_clickable(self):
        scope = self.window.snapshot['c']['namespaces']['ns']
        scope['cron_jobs'] = [obj('CronJob', 'cron')]
        scope['jobs'] = [obj('Job', 'job', ('CronJob', 'cron'), status={'failed': 1})]
        scope['pods'] += [pod('batch', 'Failed', ('Job', 'job')), pod('complete', 'Succeeded')]
        self.window.render_matrix()
        self.assertEqual(self.window.matrix_counts['pods']['Failed'], 0)
        self.assertEqual(self.window.matrix_counts['batch']['Failed'], 1)
        self.assertIn('Completed 1', self.window.extra_counters.text())
        self.assertIn('CronJobs 1 · 1 failed', label_text(self.window.workload_counters))
        self.assertNotIn('<a ', self.window.workload_counters.text())
        self.assertEqual(self.window.workload_counters.textInteractionFlags(), Qt.NoTextInteraction)
        scope['pods'] = []
        scope['_type_errors'] = {'pod': 'synthetic forbidden'}
        self.window.render_matrix()
        self.assertEqual(label_text(self.window.summary_label), 'Unavailable · Total Pods')
        self.assertIn('Unavailable', self.window.workload_counters.text())

    def test_new_equal_snapshot_reuses_topology_but_rebinds_actual_resources(self):
        from quaber.resource_index import index_scope
        w = self.window
        old_index = w._scope_index_cache[('c', 'ns')][1]
        old_visuals = w.matrix.cell_visuals(0, 1)
        changes = []
        w.matrix.model().dataChanged.connect(lambda *a: changes.append(a))
        w.snapshot = copy.deepcopy(w.snapshot)
        current_scope = w.snapshot['c']['namespaces']['ns']
        with patch('quaber.ui.index_scope', wraps=index_scope) as build:
            w.render_matrix()
            build.assert_not_called()
        self.assertIs(w._scope_index_cache[('c', 'ns')][1], old_index)
        self.assertIs(w.matrix_data[(0, 1)][3]['metadata'], current_scope['deployments'][0]['metadata'])
        self.assertIs(w.matrix_data[(0, 1)][4][0], current_scope['pods'][0])
        self.assertIs(w.matrix.cell_visuals(0, 1), old_visuals)
        self.assertEqual(changes, [])
        # In-place status replacement with unchanged RV must still show current data.
        current_scope['pods'][0]['status'] = {'phase': 'Failed'}
        w.render_matrix()
        self.assertEqual(w.matrix.cell_visuals(0, 1)[0].status, 'Failed')
        self.assertEqual(w.matrix_counts['pods']['Failed'], 1)
