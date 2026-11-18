import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from quaber.storage import UserStore
from quaber.ui import MainWindow, ResourceDialog
from quaber.views import MatrixViews, validate_preferences
from quaber.metrics import ThrottleSamples
from quaber.safety import validate_document


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_geometry_roundtrip_and_invalid_sizes(self):
        value = {"views": [{"id": "x", "name": "Geometry", "columnWidths": {"c/ns": 99}, "rowHeights": {"app": 75}}]}
        normalized = validate_preferences(value)
        self.assertEqual(normalized["views"][0]["rowHeights"], {"app": 75})
        source = MatrixViews(normalized)
        exported = source.export([])
        target = MatrixViews({})
        target.import_data(exported, [])
        self.assertEqual(target.active["rowHeights"], {"app": 75})
        value["views"][0]["rowHeights"]["app"] = 1000000
        self.assertTrue(validate_preferences(value)["preferences_recovered"])

    def test_frozen_deployment_header_and_rows_stay_aligned(self):
        from quaber.widgets import PodMatrix, NamespaceHeader
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QTableWidgetItem
        matrix = PodMatrix()
        self.addCleanup(matrix.deleteLater)
        matrix.setHorizontalHeader(NamespaceHeader(Qt.Orientation.Horizontal, matrix))
        matrix.setColumnCount(5)
        matrix.setRowCount(3)
        matrix.setHorizontalHeaderLabels(["Deployment", "ns-1", "ns-2", "ns-3", "ns-4"])
        matrix.setColumnWidth(0, 150)
        for column in range(1, 5):
            matrix.setColumnWidth(column, 100)
        for row in range(3):
            matrix.setItem(row, 0, QTableWidgetItem(f"deployment-{row}"))
            matrix.setRowHeight(row, 35+row*10)
        matrix.resize(380, 240)
        matrix.show()
        QTest.qWait(20)
        header = matrix.horizontalHeader()
        header.setSectionsMovable(True)
        header.moveSection(1, 0)
        self.assertEqual(header.visualIndex(0), 0)
        header.moveSection(0, 2)
        self.assertEqual(header.visualIndex(0), 0)
        matrix.verticalHeader().moveSection(0, 2)
        for visual in range(3):
            self.assertEqual(matrix.verticalHeader().logicalIndex(visual), matrix.frozen.verticalHeader().logicalIndex(visual))
        matrix.horizontalScrollBar().setValue(matrix.horizontalScrollBar().maximum())
        QTest.qWait(20)
        self.assertTrue(matrix.frozen.horizontalHeader().isVisible())
        self.assertEqual(matrix.frozen.x(), matrix.viewport().x())
        self.assertEqual(matrix.frozen.y()+matrix.frozen.viewport().y(), matrix.viewport().y())
        for row in range(3):
            self.assertEqual(matrix.rowViewportPosition(row), matrix.frozen.rowViewportPosition(row))
        matrix.close()

    def test_throttling_repeated_sample_preserves_measurement(self):
        def text(periods, throttled):
            return f'container_cpu_cfs_periods_total{{namespace="ns",pod="p",container="app"}} {periods}\ncontainer_cpu_cfs_throttled_periods_total{{namespace="ns",pod="p",container="app"}} {throttled}'
        samples = ThrottleSamples()
        samples.parse(text(100, 10), "c", "ns", "p")
        self.assertEqual(samples.parse(text(200, 60), "c", "ns", "p")["app"], 50)
        self.assertEqual(samples.parse(text(200, 60), "c", "ns", "p")["app"], 50)

    def test_recursive_document_is_rejected(self):
        recursive = {}
        recursive["self"] = recursive
        with self.assertRaises(ValueError):
            validate_document(recursive)

    def test_empty_failed_deployment_is_visible_and_order_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow(UserStore(root/"UserData"), root)
            window.timer.stop()
            window.auth_timer.stop()
            QTest.qWait(20)
            window.snapshot = {"c": {"name": "Fixture", "namespaces": {"ns": {
                "deployments": [{"metadata": {"name": f"app-{i}", "uid": f"uid-{i}"}, "spec": {"replicas": 1}, "status": {"availableReplicas": 0}} for i in range(4)],
                "pods": [], "replica_sets": []}}}}
            window.render_matrix()
            self.assertFalse(window.matrix.isColumnHidden(1))
            self.assertIn("Нет Pod", window.matrix.item(0, 1).text())
            header = window.matrix.verticalHeader()
            header.moveSection(0, 2)
            def order():
                return [window.matrix.item(header.logicalIndex(i), 0).text() for i in range(4)]
            before = order()
            window.render_matrix()
            self.assertEqual(before, order())
            self.assertFalse(hasattr(window, "allpods_page"))
            self.assertFalse(hasattr(window, "pod_controls"))
            self.assertFalse(any("Все Pod" == window.tabs.tabText(i) for i in range(window.tabs.count())))
            window.close()
            window._cleanup_thread.join(5)
            window.deleteLater()

    def test_updated_details_preserve_dirty_yaml_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow(UserStore(root/"UserData"), root)
            window.timer.stop()
            api = Mock()
            api.cluster.return_value = {"name": "Fixture"}
            resource = {"metadata": {"name": "settings", "namespace": "ns", "uid": "uid", "resourceVersion": "1"}, "data": {"key": "before"}}
            panel = ResourceDialog(window, api, window.background, "c", "configmap", "ns", "settings", resource)
            panel.yaml_editor.editor.insertPlainText("# local\n")
            draft = panel.yaml_editor.toPlainText()
            changed = {**resource, "metadata": {**resource["metadata"], "resourceVersion": "2"}}
            panel._adopt_live(changed)
            self.assertEqual(panel.version, "1")
            self.assertEqual(panel.yaml_editor.toPlainText(), draft)
            self.assertEqual(panel.live_resource["metadata"]["resourceVersion"], "2")
            panel._allow_discard = True
            panel.close()
            panel.deleteLater()
            window.close()
            window._cleanup_thread.join(5)
            window.deleteLater()
