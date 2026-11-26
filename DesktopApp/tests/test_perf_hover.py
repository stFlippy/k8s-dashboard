import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PyQt5.QtCore import QPoint
from PyQt5.QtWidgets import QApplication
from quaber.widgets import PodMatrix


class HoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.matrix = PodMatrix()
        self.matrix.setRowCount(40)
        self.matrix.setColumnCount(20)
        self.matrix.resize(650, 400)
        self.matrix.show()
        self.app.processEvents()

    def tearDown(self):
        self.matrix.close()
        self.matrix.deleteLater()
        self.app.processEvents()

    def center(self, view, row, col):
        return view.visualRect(view.model().index(row, col)).center()

    def test_identical_hover_does_not_invalidate(self):
        self.matrix._set_hover((2, 2), 0)
        with patch.object(self.matrix, '_hover_region', wraps=self.matrix._hover_region) as region:
            self.matrix._set_hover((2, 2), 0)
            region.assert_not_called()

    def test_cross_is_disjoint_and_leave_clears_old_cross(self):
        m = self.matrix
        region = m._hover_region(m, (1, 1), (3, 3))
        self.assertTrue(region.contains(self.center(m, 1, 4)))
        self.assertTrue(region.contains(self.center(m, 4, 3)))
        self.assertFalse(region.contains(self.center(m, 4, 4)))
        leave = m._hover_region(m, (1, 1), (-1, -1))
        self.assertTrue(leave.contains(self.center(m, 1, 4)))
        self.assertFalse(leave.contains(self.center(m, 4, 4)))

    def test_same_cell_marker_only(self):
        m = self.matrix
        region = m._hover_region(m, (2, 2), (2, 2))
        self.assertTrue(region.contains(self.center(m, 2, 2)))
        self.assertFalse(region.contains(self.center(m, 2, 3)))

    def test_scrolled_reordered_hidden_and_frozen(self):
        m = self.matrix
        m.horizontalHeader().moveSection(3, 1)
        m.verticalHeader().moveSection(4, 1)
        m.setRowHidden(2, True)
        m.setColumnHidden(2, True)
        m.verticalScrollBar().setValue(1)
        m.sync_frozen()
        self.app.processEvents()
        for view in (m, m.frozen):
            region = m._hover_region(view, (4, 3), (-1, -1))
            point = self.center(view, 4, 0)
            if view.viewport().rect().contains(point):
                self.assertTrue(region.contains(point))
            self.assertTrue(m._hover_region(view, (2, 2), (-1, -1)).isEmpty())
            self.assertTrue(m._hover_region(view, (-1, -1), (999, 999)).isEmpty())
