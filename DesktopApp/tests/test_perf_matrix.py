import unittest
from unittest.mock import patch
from PyQt5.QtCore import Qt


from helpers_matrix import MemoryStore, MatrixFixture

class MatrixTests(MatrixFixture, unittest.TestCase):


    def test_compact_roles_and_no_snapshot_mutation(self):
        m = self.window.matrix
        self.assertEqual(tuple(m.item(0, 1).data(Qt.ItemDataRole.UserRole)), ('c', 'ns', 'app'))
        visual = m.cell_visuals(0, 1)[0]
        self.assertEqual(visual.identity, ('c', 'pod', 'ns', 'pod', 'p1'))
        self.assertEqual(visual.status, 'Running')
        self.assertIn('Status: Running', visual.tooltip)
        self.assertNotIn('_recent', self.pod)
        self.assertNotIn('_stale', self.pod)

    def test_unchanged_and_status_update_keep_item_identity(self):
        m = self.window.matrix
        old = m.item(0, 1)
        with patch.object(m, 'clear', wraps=m.clear) as clear:
            self.window.render_matrix()
            self.assertIs(m.item(0, 1), old)
            clear.assert_not_called()
            self.pod['status']['phase'] = 'Failed'
            self.window.render_matrix()
            clear.assert_not_called()
        self.assertIs(m.item(0, 1), old)
        self.assertEqual(m.cell_visuals(0, 1)[0].status, 'Failed')
        self.pod['metadata']['uid'] = 'p2'
        self.window.render_matrix()
        self.assertEqual(m.cell_visuals(0, 1)[0].identity[-1], 'p2')
        self.assertEqual(self.window.matrix_data[(0, 1)][4][0]['metadata']['uid'], 'p2')

    def test_removing_scope_removes_old_visuals(self):
        self.window.snapshot = {}
        self.window.render_matrix()
        self.assertEqual(self.window.matrix.rowCount(), 0)
        self.assertEqual(self.window.matrix.cell_visuals(0, 1), ())

    def test_unchanged_reuses_index_and_visual_and_emits_no_body_changes(self):
        w = self.window
        visuals = w.matrix.cell_visuals(0, 1)
        changes = []
        w.matrix.model().dataChanged.connect(lambda *args: changes.append(args))
        with patch('quaber.ui.index_scope', wraps=__import__('quaber.resource_index', fromlist=['index_scope']).index_scope) as index:
            w.render_matrix()
            index.assert_not_called()
        self.assertIs(w.matrix.cell_visuals(0, 1), visuals)
        self.assertEqual(changes, [])
        self.pod['metadata']['ownerReferences'][0]['uid'] = 'other'
        w.render_matrix()
        self.assertEqual(w.matrix_data[(0, 1)][3]['metadata']['uid'], 'other')

    def test_hidden_matrix_defers_latest_snapshot_once(self):
        window = self.window
        window._resource_panel = object()
        with patch.object(window, '_render_matrix_content', wraps=window._render_matrix_content) as apply:
            for i in range(10):
                self.pod['metadata']['uid'] = str(i)
                window.render_matrix()
            apply.assert_not_called()
            self.assertTrue(window._matrix_visual_dirty)
            window._resource_panel = None
            window.render_matrix()
            self.assertEqual(apply.call_count, 1)
        self.assertFalse(window._matrix_visual_dirty)
        self.assertEqual(window.matrix.cell_visuals(0, 1)[0].identity[-1], '9')
