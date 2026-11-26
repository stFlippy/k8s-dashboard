import copy
from unittest.mock import Mock, patch
import unittest
from helpers_matrix import MatrixFixture


class MatrixRegressionTests(MatrixFixture, unittest.TestCase):
    def populate_three(self):
        scope = self.window.snapshot['c']['namespaces']['ns']
        self.window.snapshot['c']['namespaces'] = {name: copy.deepcopy(scope) for name in ('a', 'b', 'c')}
        for scope in self.window.snapshot['c']['namespaces'].values():
            scope['deployments'] = [dict(metadata={'name': name, 'uid': name}) for name in ('a', 'b', 'c')]
            scope['pods'][0]['metadata']['ownerReferences'][0]['uid'] = 'a'
        self.window.render_matrix()

    def visible_columns(self):
        w = self.window
        h = w.matrix.horizontalHeader()
        return [w._columns[h.logicalIndex(v)-1][2] for v in range(1, h.count())]

    def test_drag_columns_and_rows_survive_two_refreshes(self):
        self.populate_three()
        w = self.window
        w.matrix.horizontalHeader().moveSection(3, 1)
        w.matrix.verticalHeader().moveSection(2, 0)
        columns = list(w.views.active['namespaceOrder'])
        rows = list(w.views.active['deploymentOrder'])
        for _ in range(2):
            w.render_matrix()
            self.assertEqual(self.visible_columns(), ['c', 'a', 'b'])
            h = w.matrix.verticalHeader()
            self.assertEqual([w._row_names[h.logicalIndex(v)] for v in range(h.count())], ['c', 'a', 'b'])
            self.assertEqual(w.views.active['namespaceOrder'], columns)
            self.assertEqual(w.views.active['deploymentOrder'], rows)

    def test_no_enabled_clusters_resets_all_layers(self):
        w = self.window
        w.store.clusters = lambda: []  # Explicitly disable the synthetic connection.
        w.refresh_matrix()
        w.render_matrix()
        self.assertEqual(w.snapshot, {})
        self.assertFalse(w.matrix_data)
        self.assertFalse(w.matrix._cell_visuals)
        self.assertEqual(w.matrix.rowCount(), 0)

    def test_external_clear_repairs_structure(self):
        w = self.window
        w.matrix.clear()
        w.matrix.setColumnCount(0)
        w.render_matrix()
        self.assertIsNotNone(w.matrix.horizontalHeaderItem(1))

    def test_disable_during_loading_and_old_callbacks(self):
        w = self.window
        w.store.clusters = lambda: []  # Explicitly disable the synthetic connection.
        w.show()
        self.app.processEvents()
        jobs = []
        w.background.submit = lambda fn, done, failed, **kwargs: jobs.append((fn, done, failed))
        with patch.object(w.store, 'clusters', return_value=[{'id': 'c', 'name': 'C', 'enabled': True}]):
            w.refresh_matrix()
        generation = w._matrix_generation
        w.refresh_matrix()
        self.assertFalse(w.loading_matrix)
        self.assertEqual(w.snapshot, {})
        jobs[0][1](({'old': {'name': 'Old', 'namespaces': {}}}, []))
        jobs[0][2]('old failure')
        from quaber.fetch_plan import FetchPlan, ClusterPlan, ScopeProgress
        target = ClusterPlan('old','Old','obsolete-revision')
        w._matrix_scope_loaded(ScopeProgress(FetchPlan((target,),None,generation),target,'ns',w.monitoring.token(),{}))
        self.assertEqual(w.snapshot, {})
        self.assertFalse(w.loading_matrix)

    def test_disable_enable_cycles_new_generation_wins(self):
        w = self.window
        w.store.clusters = lambda: []  # Explicitly disable the synthetic connection.
        saved = copy.deepcopy(w.snapshot)
        w.show()
        self.app.processEvents()
        jobs = []
        w.background.submit = lambda fn, done, failed, **kwargs: jobs.append((fn, done, failed))
        for _ in range(5):
            w.refresh_matrix()
            self.assertEqual(w.snapshot, {})
            with patch.object(w.store, 'clusters', return_value=[{'id': 'c', 'name': 'C', 'enabled': True}]):
                w.refresh_matrix()
                current = jobs[-1]
                for old in jobs[:-1]:
                    old[1](({}, []))
                    old[2]('stale')
                self.assertTrue(w.loading_matrix)
                current[1]((copy.deepcopy(saved), []))
            self.assertIsNotNone(w.matrix.horizontalHeaderItem(1))
            self.assertEqual(w.matrix.cell_visuals(0, 1)[0].identity[-1], 'p1')

    def test_identity_helper_restores_blocked_state(self):
        self.populate_three()
        h = self.window.matrix.horizontalHeader()
        h.blockSignals(True)
        h.moveSection(3, 1)
        self.window._restore_header_identity(h)
        self.assertTrue(h.signalsBlocked())
        self.assertEqual([h.logicalIndex(v) for v in range(h.count())], list(range(h.count())))
        h.blockSignals(False)

    def test_filter_and_structure_keep_saved_order(self):
        self.populate_three()
        w = self.window
        w.matrix.horizontalHeader().moveSection(3, 1)
        w.render_matrix()
        saved = list(w.views.active['namespaceOrder'])
        w.views.active['selectedNamespaces'] = ['a', 'c']
        w.render_matrix()
        self.assertEqual(self.visible_columns(), ['c', 'a'])
        w.views.active['selectedNamespaces'] = None
        w.render_matrix()
        self.assertEqual(self.visible_columns(), ['c', 'a', 'b'])
        self.assertEqual(w.views.active['namespaceOrder'], saved)
        scope = w.snapshot['c']['namespaces']
        scope['new'] = copy.deepcopy(scope['a'])
        del scope['a']
        w.render_matrix()
        self.assertEqual(self.visible_columns(), ['c', 'b', 'new'])
