"""Copying connection fields and keeping live resource text stable."""
import copy
import unittest
from unittest.mock import patch
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLineEdit, QTableWidgetItem
from quaber.resource_list import ResourceList
from quaber.themes import apply_palette, stylesheet
from test_ux import ShellFixture
from test_storage import CONFIG


class CopyAndRefreshTests(ShellFixture, unittest.TestCase):
    def test_new_scopes_are_selected_while_operator_exclusions_survive_reload(self):
        from quaber.views import MatrixViews
        views = self.window.views
        scopes = {"ClusterIds": {"a", "hidden"}, "Namespaces": {"ns", "hidden"}, "Deployments": {"app", "hidden"}}
        views.observe_scopes(scopes)
        for suffix in scopes:
            views.active["selected"+suffix] = [next(v for v in scopes[suffix] if v != "hidden")]
        views.persist()
        views = MatrixViews(views.preferences)
        views.observe_scopes({key: values | {"new"} for key, values in scopes.items()})
        for suffix in scopes:
            self.assertIn("new", views.active["selected"+suffix])
            self.assertNotIn("hidden", views.active["selected"+suffix])
        views.active["selectedDeployments"].remove("new")
        views.observe_scopes({key: values | {"new"} for key, values in scopes.items()})
        self.assertNotIn("new", views.active["selectedDeployments"])

    def test_long_matrix_names_have_room_for_bold_wrapped_text_and_filter_order(self):
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QFontMetrics
        resource = copy.deepcopy(self.deployment)
        resource["metadata"]["name"] = "kube-controller-manager-k8s-lab-1"
        self.window.snapshot = {"c": {"name": "Lab", "namespaces": {"ns": {"deployments": [resource], "pods": [], "replica_sets": []}}}}
        self.window.preferences["deployment_width"] = 150
        self.window.render_matrix()
        table = self.window.matrix
        font = table.font(); font.setBold(True)
        required = QFontMetrics(font).boundingRect(QRect(0, 0, table.columnWidth(0)-18, 10000),
                    Qt.TextFlag.TextWordWrap, resource["metadata"]["name"]).height()+6
        self.assertGreaterEqual(table.rowHeight(0)-table.group_offset(0), required)
        self.assertEqual([title for title, button in self.window.scope_buttons.values()], ["Кластер", "Namespace", "Deployment"])

    def test_settings_copy_host_login_and_substrings_without_editing_connections(self):
        self.window.store.put_cluster("Lab", "https://example.test:6443", "admin@k8s.lab", CONFIG)
        self.api.access_summary.return_value = {"level": "Администратор", "detail": ""}
        observed = []
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            table = next(table for table in dialog.findChildren(ResourceList) if table.property("clusterSettings"))
            for column, value in ((1, "https://example.test:6443"), (2, "admin@k8s.lab")):
                entry = table.topLevelItem(0)
                table.setCurrentItem(entry, column)
                table.setFocus()
                QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(QApplication.clipboard().text(), value)
                rect = table.visualRect(table.model().index(0, column))
                QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
                QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
                self.app.processEvents()
                editor = next(editor for editor in table.findChildren(QLineEdit) if editor.isVisible())
                self.assertTrue(editor.isReadOnly())
                editor.setSelection(0, 5)
                QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(QApplication.clipboard().text(), value[:5])
                QTest.keyClicks(editor, "unwanted-change")
                self.assertEqual(editor.text(), value)
                QTest.keyClick(editor, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertEqual(entry.text(column), value)
            def choose(menu, position):
                for action in menu.actions():
                    action.trigger()
                    observed.append((action.text(), QApplication.clipboard().text()))
            with patch("quaber.resource_list.QMenu.exec_", new=choose):
                table._copy_menu(table.visualRect(table.model().index(0, 1)).center())
            dialog.accept()
        with patch("quaber.ui.run_dialog", side_effect=inspect):
            self.window.clusters_dialog()
        self.assertEqual(observed, [("Копировать хост", "https://example.test:6443"), ("Копировать логин", "admin@k8s.lab")])
        self.assertEqual(self.window.store.clusters()[0]["username"], "admin@k8s.lab")

    def test_unchanged_visible_summary_preserves_selection_on_real_poll(self):
        panel = self.open_panel("pod")
        view = panel.details
        cursor = view.document().find("app-pod")
        view.setTextCursor(cursor)
        changed = copy.deepcopy(panel.resource)
        changed["metadata"]["resourceVersion"] = "new-version"
        self.api.resource.side_effect = None
        self.api.resource.return_value = changed
        with patch.object(view, "setHtml", wraps=view.setHtml) as rendered:
            panel.refresh_object()
            self.wait()
        rendered.assert_not_called()
        self.assertEqual(view.textCursor().selectedText(), "app-pod")
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(QApplication.clipboard().text(), "app-pod")
        self.assertEqual(panel.live_resource["metadata"]["resourceVersion"], "new-version")

    def test_changed_summary_waits_for_selection_then_shows_latest_server_values(self):
        panel = self.open_panel("pod")
        view = panel.details
        view.setTextCursor(view.document().find("app-pod"))
        for phase in ("Pending", "Failed"):
            resource = copy.deepcopy(panel.resource)
            resource["status"]["phase"] = phase
            panel._adopt_live(resource)
            self.app.processEvents()
            self.assertEqual(view.textCursor().selectedText(), "app-pod")
            self.assertIn("Running", view.toPlainText())
            self.assertEqual(panel.context_values["Статус"].text(), phase)
        cursor = view.textCursor()
        cursor.clearSelection()
        view.setTextCursor(cursor)
        self.app.processEvents()
        self.assertIn("Failed", view.toPlainText())
        self.assertNotIn("Pending", view.toPlainText())
        self.assertNotIn("Running", view.toPlainText())

    def test_summary_refresh_keeps_scroll_and_reverts_obsolete_pending_updates(self):
        panel = self.open_panel("pod")
        resource = copy.deepcopy(panel.resource)
        resource["metadata"]["labels"] = {f"long-label-{i:03}": "value" for i in range(100)}
        panel._adopt_live(resource)
        self.app.processEvents()
        view = panel.details
        view.verticalScrollBar().setValue(120)
        resource = copy.deepcopy(resource)
        resource["metadata"]["labels"]["last-label"] = "new-value"
        panel._adopt_live(resource)
        self.assertEqual(view.verticalScrollBar().value(), 120)
        view.setTextCursor(view.document().find("app-pod"))
        original = copy.deepcopy(resource)
        resource["status"]["phase"] = "Pending"
        panel._adopt_live(resource)
        panel._adopt_live(original)
        cursor = view.textCursor()
        cursor.clearSelection()
        view.setTextCursor(cursor)
        self.app.processEvents()
        self.assertIn("Running", view.toPlainText())
        self.assertNotIn("Pending", view.toPlainText())

    def test_header_resize_handles_are_visible_and_manual_width_survives_refresh(self):
        table = ResourceList()
        self.addCleanup(table.close)
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Хост", "Namespace", "Версия"])
        table.setRowCount(1)
        for column, value in enumerate(("https://lab.test:6443", "quota-demo", "1")):
            table.setItem(0, column, QTableWidgetItem(value))
        table.resize(800, 200)
        table.resizeColumnsToContents()
        table.show()
        header = table.header()
        def mouse(event_type, point, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
            event = QMouseEvent(event_type, QPointF(point), QPointF(header.viewport().mapToGlobal(point)),
                                button, buttons, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(header.viewport(), event)
            self.app.processEvents()
        for theme in ("light", "dark"):
            apply_palette(table, theme == "light")
            table.setStyleSheet(stylesheet(theme == "light"))
            self.app.processEvents()
            mouse(QEvent.Type.MouseMove, QPoint(10, 10))
            edge = table.columnWidth(0)
            point = QPoint(edge-1, header.height()//2)
            plain = header.viewport().grab().toImage()
            self.assertNotEqual(plain.pixelColor(point), plain.pixelColor(point-QPoint(5, 0)))
            mouse(QEvent.Type.MouseMove, point)
            active = header.viewport().grab().toImage()
            self.assertNotEqual(active.pixelColor(point), plain.pixelColor(point), theme)
        old_width = table.columnWidth(0)
        point = QPoint(old_width-1, header.height()//2)
        mouse(QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        mouse(QEvent.Type.MouseMove, point+QPoint(55, 0), buttons=Qt.MouseButton.LeftButton)
        mouse(QEvent.Type.MouseButtonRelease, point+QPoint(55, 0), Qt.MouseButton.LeftButton)
        new_width = table.columnWidth(0)
        self.assertGreater(new_width, old_width)
        table.setItem(0, 0, QTableWidgetItem("https://short"))
        table.resizeColumnsToContents()
        self.assertEqual(table.columnWidth(0), new_width)

    def test_matrix_search_filters_workload_names_without_matching_child_pod_names(self):
        self.window.snapshot = {"c": {"name": "Lab", "namespaces": {"ns": {
            "deployments": [self.deployment], "pods": [self.pod], "replica_sets": [
                {"metadata": {"uid": "rs", "ownerReferences": [{"kind": "Deployment", "uid": "d", "controller": True}]}}]}}}}
        self.window.search.setText("app")
        self.window.render_matrix()
        self.assertIn("app", self.window._row_names)
        self.window.search.setText("app-pod")
        self.window.render_matrix()
        self.assertNotIn("app", self.window._row_names)
        self.assertEqual(self.window.search.placeholderText(), "Имя Deployment…")
