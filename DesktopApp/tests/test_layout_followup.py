"""Matrix sizing, resource layouts and explicit replica draft application."""
import copy
import unittest
from unittest.mock import Mock, patch
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QLabel, QHeaderView

from quaber.quota_card import QuotaCard
from quaber.themes import write_background
from quaber.widgets import MultiSelectMenu
from test_ux import ShellFixture, Deferred
from qt_helpers import drag_move


class LayoutFollowupTests(ShellFixture, unittest.TestCase):
    def matrix(self):
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {"c": {"name": "Production", "namespaces": {
            "ns": {"deployments": [self.deployment], "pods": [self.pod], "replica_sets": []},
            "other": {"deployments": [self.deployment], "pods": [], "replica_sets": []}}}}
        self.window.render_matrix()
        self.app.processEvents()
        return self.window.matrix

    def test_pod_cells_cannot_resize_and_legacy_sizes_are_ignored(self):
        self.window.views.active["columnWidths"] = {"c/ns": 200}
        self.window.views.active["rowHeights"] = {"app": 500}
        matrix = self.matrix()
        widths = [matrix.columnWidth(i) for i in range(matrix.columnCount())]
        heights = [matrix.rowHeight(i) for i in range(matrix.rowCount())]
        self.assertEqual(widths[1:], [48, 48])
        self.assertLess(heights[0], 500)
        edge = QPoint(matrix.columnWidth(0)+48, matrix.GROUP_HEIGHT+12)
        self.assertIsNone(matrix._resize_edge(edge))
        with patch.object(self.window, "open_resource"):
            QTest.mousePress(matrix.viewport(), Qt.MouseButton.LeftButton, pos=edge)
            drag_move(matrix.viewport(), edge+QPoint(30, 15))
            QTest.mouseRelease(matrix.viewport(), Qt.MouseButton.LeftButton, pos=edge+QPoint(30, 15))
        self.assertEqual(widths, [matrix.columnWidth(i) for i in range(matrix.columnCount())])
        self.assertEqual(heights, [matrix.rowHeight(i) for i in range(matrix.rowCount())])
        for column in range(1, matrix.columnCount()):
            self.assertEqual(matrix.horizontalHeader().sectionResizeMode(column), QHeaderView.ResizeMode.Fixed)

    def test_name_boundary_and_header_bottom_resize_independently(self):
        matrix = self.matrix()
        width = matrix.columnWidth(0)
        point = QPoint(width-1, matrix.GROUP_HEIGHT+12)
        QTest.mousePress(matrix.viewport(), Qt.MouseButton.LeftButton, pos=point)
        drag_move(matrix.viewport(), point+QPoint(30, 0))
        QTest.mouseRelease(matrix.viewport(), Qt.MouseButton.LeftButton, pos=point+QPoint(30, 0))
        self.assertEqual(matrix.columnWidth(0), width+30)
        self.assertEqual(self.window.preferences["deployment_width"], width+30)
        header = matrix.horizontalHeader()
        height = header.height()
        point = QPoint(matrix.columnWidth(0)+20, height-2)
        QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
        drag_move(header.viewport(), point+QPoint(0, 24))
        QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=point+QPoint(0, 24))
        self.assertEqual(header.height(), height+24)
        self.assertEqual(self.window.preferences["header_height"], height+24)
        self.assertEqual(matrix.columnWidth(0), width+30)

    def test_navigation_grid_and_ring_position(self):
        panel = self.open_panel("deployment")
        self.window.resize(1600, 900)
        self.app.processEvents()
        boxes = (panel.nav_cluster, panel.nav_namespace, panel.nav_deployment)
        self.assertTrue(panel.nav_pod.isHidden())
        widths = [box.width() for box in boxes]
        self.assertLessEqual(max(widths)-min(widths), 1)
        self.assertGreater(widths[0], 280)
        for previous, current in zip(boxes, boxes[1:]):
            self.assertLessEqual(current.x()-previous.geometry().right(), 14)
        self.assertEqual(boxes[0].x(), 18)
        self.assertLess(panel.pod_donut.mapTo(panel, QPoint()).y(), panel.details.mapTo(panel, QPoint()).y())
        self.assertLess(panel.pod_donut.mapTo(panel, QPoint()).x(), panel.width()/3)
        self.assertEqual(panel.details.frameWidth(), 0)

    def test_yaml_reload_state_and_save_to_right_of_search(self):
        panel = self.open_panel("deployment")
        self.assertFalse(panel.yaml_reload.isEnabled())
        self.assertTrue(panel.yaml_notice.isHidden())
        toolbar = panel.yaml_editor.toolbar
        self.assertIs(toolbar.itemAt(0).widget(), panel.yaml_reload)
        self.assertIs(toolbar.itemAt(1).widget(), panel.yaml_notice)
        search = toolbar.indexOf(panel.yaml_editor.search_button)
        self.assertIs(toolbar.itemAt(search+1).widget(), panel.yaml_save)
        changed = copy.deepcopy(panel.resource)
        changed["metadata"]["resourceVersion"] = "2"
        panel._adopt_live(changed)
        self.assertTrue(panel.yaml_reload.isEnabled())
        self.assertTrue(panel.yaml_reload.property("primary"))
        self.assertFalse(panel.yaml_notice.isHidden())
        self.deployment = changed
        panel.reload_yaml()
        self.wait()
        self.assertFalse(panel.yaml_reload.isEnabled())
        self.assertFalse(panel.yaml_reload.property("primary"))
        self.assertTrue(panel.yaml_notice.isHidden())

    def test_warning_icon_and_access_header_contrast_in_both_themes(self):
        panel = self.open_panel("deployment")
        for theme in ("light", "dark"):
            self.window._select_theme(theme)
            for writable in (False, True):
                self.window.set_read_only(not writable)
                self.app.processEvents()
                image = self.window.shell_actions.mode.grab().toImage()
                colors = {image.pixelColor(x, y).name().upper() for x in range(image.width()) for y in range(image.height())}
                self.assertIn("#FFD45C" if writable else "#DF0028", colors)
                self.assertEqual(panel.property("writeEnabled"), writable)
                self.assertEqual(panel.tabs.tabBar().property("writeEnabled"), writable)
                if writable:
                    background = QColor(write_background(theme == "light")).name()
                    self.assertEqual(panel.header_widget.grab().toImage().pixelColor(2, 2).name(), background)
                    self.assertEqual(panel.navigation_bar.grab().toImage().pixelColor(2, 2).name(), background)
                    bar = panel.tabs.tabBar().geometry()
                    self.assertEqual(panel.tabs.grab().toImage().pixelColor(panel.tabs.width()-3, bar.center().y()).name(), background)

    def test_selected_white_and_alternating_rows_in_both_themes(self):
        from quaber.ui import item
        panel = self.open_panel("deployment")
        table = panel.pod_table
        panel.tabs.setCurrentWidget(table.parentWidget())
        self.wait()
        table.setRowCount(2)
        for row in range(2):
            for column, value in enumerate(("Running", f"pod-{row}", "1 мин", "10.0.0.1", "0", "0%", "3%")):
                table.setItem(row, column, item(value))
        for theme in ("light", "dark"):
            self.window._select_theme(theme)
            colors = []
            for row in range(2):
                table.selectRow(row)
                panel.nav_cluster.setFocus()
                self.app.processEvents()
                rect = table.visualItemRect(table.topLevelItem(row))
                image = table.viewport().grab().toImage()
                color = image.pixelColor(table.viewport().width()-4, rect.center().y())
                colors.append(color.name())
                self.assertNotEqual(color, table.palette().base().color())
                self.assertEqual(image.pixelColor(2, rect.center().y()).name(), color.name())
            self.assertEqual(colors[0], colors[1])

    def test_multiselect_indicator_is_inset_and_click_keeps_menu_open(self):
        self.window._select_theme("light")
        menu = MultiSelectMenu(self.window)
        self.addCleanup(menu.deleteLater)
        action = menu.addAction("DaemonSet/kube-flannel")
        action.setCheckable(True)
        action.setChecked(True)
        menu.popup(self.window.mapToGlobal(QPoint(400, 180)))
        self.app.processEvents()
        image = menu.grab().toImage()
        rect = menu.actionGeometry(action)
        xs = [x for y in range(rect.top(), rect.bottom()+1) for x in range(rect.left(), min(rect.left()+40, image.width()))
              if image.pixelColor(x, y).name().upper() == "#005FB8"]
        self.assertTrue(xs)
        self.assertGreaterEqual(min(xs), 8)
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertFalse(action.isChecked())
        self.assertTrue(menu.isVisible())
        menu.close()

    def test_quota_title_contains_context(self):
        quota = QuotaCard("Production", "orders", {"metadata": {"name": "namespace-quota"}})
        self.addCleanup(quota.deleteLater)
        title = quota.findChild(QLabel, "quotaTitle")
        self.assertEqual(title.text(), "quota: Production / orders")
        self.assertIn("namespace-quota", title.toolTip())
        self.assertIsNone(quota.findChild(QLabel, "quotaContext"))
        self.assertEqual(self.window.quotas_page.findChild(QLabel, "sectionTitle").text(), "Квоты ресурсов · использовано / лимит")


class ReplicaBatchTests(ShellFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.panel = self.open_panel("deployment")
        self.ready_yaml(self.panel)
        self.panel.tabs.setCurrentIndex(0)
        self.actual = copy.deepcopy(self.panel.resource)
        self.actual["spec"]["replicas"] = 3
        self.panel._adopt_live(copy.deepcopy(self.actual))
        self.panel.background.background = self.deferred = Deferred()
        self.panel.reload_deployment_pods = Mock()
        self.api.resource.side_effect = lambda *args: copy.deepcopy(self.actual)
        def scale(cid, namespace, name, value, uid, version, *, permit):
            permit.check((cid, 'deployment', namespace, name, uid), 'scale')
            permit.check_parameters({'replicas': value, 'resourceVersion': version})
            self.assertEqual((cid, namespace, name, uid, version), ("c", "ns", "app", "d", self.actual["metadata"]["resourceVersion"]))
            self.actual["spec"]["replicas"] = value
            self.actual["metadata"]["resourceVersion"] = str(int(version)+1)
            return copy.deepcopy(self.actual)
        self.api.scale.side_effect = scale
        self.window.set_read_only(False)

    def click(self, delta):
        QTest.mouseClick(self.panel.replica_up if delta > 0 else self.panel.replica_down, Qt.MouseButton.LeftButton)

    def commit(self):
        with patch('quaber.ui.confirm', return_value=True):
            self.panel.scale_apply.click()
        self.assertEqual(len(self.deferred.jobs), 1)
        self.deferred.finish(execute=True)

    def test_three_down_then_separate_five_up_apply_only_final_values(self):
        for _ in range(3):
            self.click(-1)
        self.assertEqual(self.panel._scaling_target, 0)
        self.api.scale.assert_not_called()
        self.assertEqual(self.deferred.jobs, [])
        self.assertEqual(self.panel.resource["spec"]["replicas"], 3)
        self.commit()
        self.assertEqual(self.actual["spec"]["replicas"], 0)
        self.assertTrue(self.window.preferences["read_only"])
        self.panel._displayed_pods = []
        self.window.set_read_only(False)
        for _ in range(5):
            self.click(1)
        self.assertEqual(self.actual["spec"]["replicas"], 0)
        self.commit()
        self.assertEqual([call.args[3] for call in self.api.scale.call_args_list], [0, 5])

    def test_five_up_two_down_keep_draft_and_apply_one_net_change(self):
        for _ in range(5):
            self.click(1)
        QTest.qWait(250)
        self.assertFalse(self.panel.scale_delay_timer.isActive())
        self.click(-1)
        self.click(-1)
        self.assertFalse(self.panel.scale_delay_timer.isActive())
        self.assertFalse(self.panel.scale_countdown_timer.isActive())
        self.assertIn("3 → 6", self.panel.scale_apply.text())
        self.assertEqual(self.deferred.jobs, [])
        self.api.scale.assert_not_called()
        self.commit()
        self.assertEqual([call.args[3] for call in self.api.scale.call_args_list], [6])

    def test_even_expired_legacy_timer_sends_nothing_until_explicit_apply(self):
        self.click(1)
        QTest.qWait(100)
        self.assertEqual(self.deferred.jobs, [])
        self.panel.scale_delay_timer.start(30)
        QTest.qWait(90)
        self.assertEqual(len(self.deferred.jobs), 0)
        self.assertFalse(self.panel.pending_operation)
        self.api.scale.assert_not_called()
        self.panel.scale_apply.click()
        self.assertEqual(len(self.deferred.jobs), 1)
        self.assertTrue(self.panel.pending_operation)
        self.assertFalse(self.panel.replica_up.isEnabled())
        self.panel.scale_by(1)
        self.assertEqual(len(self.deferred.jobs), 1)
        self.deferred.finish(execute=True)
        self.assertEqual([call.args[3] for call in self.api.scale.call_args_list], [4])

    def test_readonly_and_tab_change_cancel_uncommitted_clicks(self):
        self.click(1)
        self.assertIsNone(self.window.begin_write_operation())
        self.assertFalse(self.panel.restart_button.isEnabled())
        self.assertTrue(self.panel.yaml_editor.editor.isReadOnly())
        self.window.set_read_only(True)
        self.panel._commit_scale_draft()
        self.assertEqual(self.deferred.jobs, [])
        self.assertIsNone(self.panel._scaling_target)
        self.window.set_read_only(False)
        self.click(1)
        self.panel.tabs.setCurrentWidget(self.panel.yaml_editor)
        self.panel._commit_scale_draft()
        self.assertEqual(self.deferred.jobs, [])
        self.assertTrue(self.window.preferences["read_only"])
        self.api.scale.assert_not_called()

    def test_status_only_version_change_requires_review_and_keeps_draft(self):
        self.click(1)
        self.actual["metadata"]["resourceVersion"] = "2"
        self.panel._commit_scale_draft()
        with patch("quaber.ui.error") as warning:
            self.deferred.finish(execute=True)
        warning.assert_called_once()
        self.api.scale.assert_not_called()
        self.assertEqual(self.panel._scale_snapshot['metadata']['resourceVersion'], '1')
        self.assertEqual(self.panel._scaling_target, 4)
        self.assertTrue(self.window.preferences["read_only"])

    def test_net_zero_and_changed_target_do_not_write(self):
        self.click(1)
        self.click(-1)
        self.panel._commit_scale_draft()
        self.assertEqual(self.deferred.jobs, [])
        self.assertTrue(self.window.preferences["read_only"])
        self.window.set_read_only(False)
        self.click(1)
        self.panel.name = "different"
        self.panel._commit_scale_draft()
        self.assertEqual(self.deferred.jobs, [])
        self.api.scale.assert_not_called()

    def test_readonly_before_worker_blocks_write_and_restart_rollout_remains_one_action(self):
        self.click(1)
        self.panel._commit_scale_draft()
        self.window.set_read_only(True)
        with patch("quaber.ui.error"):
            self.deferred.finish(execute=True)
        self.api.scale.assert_not_called()
        self.assertEqual(self.panel.restart_button.text(), "Restart rollout")
        self.panel._cancel_scale_draft()
        self.window.set_read_only(False)
        self.api.restart.return_value = copy.deepcopy(self.panel.resource)
        with patch("quaber.ui.confirm", return_value=True):
            self.panel.restart_button.click()
            self.deferred.finish(execute=True)
        self.assert_bound_call(self.api.restart,("c", "ns", "app", "d", "1"),'restart','deployment')
        self.assertTrue(self.window.preferences["read_only"])
