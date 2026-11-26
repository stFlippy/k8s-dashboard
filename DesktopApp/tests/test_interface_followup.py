"""UI behavior regressions for the OpenShift interface follow-up."""
import copy
import threading
import time
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
from PyQt5.QtCore import QPoint, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractItemView, QLineEdit
from test_ux import ShellFixture, Deferred


class InterfaceFollowupTests(ShellFixture, unittest.TestCase):
    def test_startup_matrix_shows_core_before_optional_requests_finish(self):
        from test_storage import CONFIG
        record = self.window.store.put_cluster("Fixture", "https://example.test:6443", "alice", CONFIG)
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.api.namespaces.return_value = ["ns"]
        self.window.views.active.update(selectedClusterIds=[record['id']], selectedNamespaces=['ns'])
        def route(cid, kind, namespace):
            core = {'deployment': [self.deployment], 'pod': [self.pod],
                    'replicaset': self.api.replica_sets.return_value}
            if kind in core:
                return copy.deepcopy(core[kind])
            gate.wait(3)
            return []
        self.api.list_resources.side_effect = route
        self.api.all_namespace_resources.side_effect = lambda cid, kind: route(cid, kind, 'ns')
        from quaber.ui import MainWindow
        self.window.refresh = MainWindow.refresh.__get__(self.window, MainWindow)
        self.window._load_initial_matrix()
        deadline = time.monotonic()+2
        while self.window.matrix.rowCount() == 0 and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertGreater(self.window.matrix.rowCount(), 0)
        self.assertTrue(self.window.loading_matrix)
        self.assertTrue(self.window.snapshot[record["id"]]["namespaces"]["ns"]["_loading"])
        for kind in ('deployment','pod','replicaset'):
            self.assertEqual(sum(call.args[1] == kind for call in self.api.all_namespace_resources.call_args_list),1)
        gate.set()
        self.wait()
        self.assertNotIn("_loading", self.window.snapshot[record["id"]]["namespaces"]["ns"])

    def test_collapsed_navigation_and_full_workspace_resource(self):
        self.window._set_sidebar_collapsed(True)
        QTest.qWait(25)
        self.assertEqual(self.window.sidebar.width(), 56)
        sidebar_x = self.window.sidebar.mapTo(self.window, QPoint()).x()
        workspace_x = self.window.workspace.mapTo(self.window, QPoint()).x()
        self.assertLessEqual(workspace_x-sidebar_x, 58)
        self.assertEqual(self.window.sidebar_toggle.text(), ">>")
        for button in self.window.nav_buttons:
            self.assertEqual(button.text(), "")
            self.assertFalse(button.icon().isNull())
            self.assertTrue(button.toolTip())
        self.assertFalse(self.window.brand_name.isVisible())
        panel = self.open_panel("deployment")
        self.assertEqual(panel.geometry(), self.window.workspace.rect())
        self.window._set_sidebar_collapsed(False)
        QTest.qWait(25)
        self.assertEqual(self.window.sidebar.width(), 240)
        self.assertEqual(panel.geometry(), self.window.workspace.rect())
        self.assertFalse(any(button.text() == "Обновить объект" for button in panel.findChildren(type(panel.yaml_save))))

    def test_group_header_is_merged_noninteractive_and_blank_cells_cannot_edit(self):
        scope = {"deployments": [self.deployment], "pods": [self.pod], "replica_sets": []}
        self.window.snapshot = {"c": {"name": "Production", "namespaces": {"ns": scope, "empty": {"deployments": [], "pods": [], "replica_sets": []}}}}
        self.window.render_matrix()
        matrix = self.window.matrix
        point = QPoint(matrix.columnWidth(0)+10, matrix.rowViewportPosition(0)+12)
        QTest.mouseMove(matrix.viewport(), point)
        self.assertEqual(matrix.hovered, (-1, -1))
        with patch.object(self.window, "open_resource") as opened:
            QTest.mouseDClick(matrix.viewport(), Qt.MouseButton.LeftButton, pos=point)
            opened.assert_not_called()
            cell = matrix.visualRect(matrix.model().index(0, 2))
            QTest.mouseDClick(matrix.viewport(), Qt.MouseButton.LeftButton,
                             pos=QPoint(cell.center().x(), cell.top()+matrix.GROUP_HEIGHT+12))
        self.assertEqual(matrix.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
        self.assertNotEqual(matrix.state(), QAbstractItemView.State.EditingState)
        self.assertEqual(matrix.findChildren(QLineEdit), [])
        image = matrix.group_headers.grab().toImage()
        x = matrix.columnWidth(0)
        self.assertEqual(image.pixelColor(x-1, 12), image.pixelColor(x+1, 12))

    def test_filter_checkboxes_keep_popup_open(self):
        from quaber.widgets import MultiSelectMenu
        menu = MultiSelectMenu(self.window)
        self.addCleanup(menu.deleteLater)
        actions = [menu.addAction(label) for label in ("one", "two")]
        for action in actions:
            action.setCheckable(True)
        menu.popup(self.window.mapToGlobal(QPoint(300, 150)))
        QTest.qWait(20)
        for action in actions:
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
            self.assertTrue(menu.isVisible())
            self.assertTrue(action.isChecked())
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        self.assertFalse(menu.isVisible())

    def test_yaml_reload_is_blue_when_changed_and_save_is_right_of_search(self):
        panel = self.open_panel("deployment")
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        changed = copy.deepcopy(panel.resource)
        changed["metadata"]["resourceVersion"] = "2"
        panel._adopt_live(changed)
        self.app.processEvents()
        self.assertEqual(panel.yaml_reload.text(), "Обновить")
        self.assertTrue(panel.yaml_reload.property("primary"))
        reload_index = panel.yaml_editor.toolbar.indexOf(panel.yaml_reload)
        self.assertIs(panel.yaml_editor.toolbar.itemAt(reload_index+1).widget(), panel.yaml_notice)
        search_index = panel.yaml_editor.toolbar.indexOf(panel.yaml_editor.search_button)
        self.assertIs(panel.yaml_editor.toolbar.itemAt(search_index+1).widget(), panel.yaml_save)
        self.deployment = changed
        panel.reload_yaml()
        self.wait()
        self.assertFalse(panel.yaml_reload.property("primary"))

    def test_logs_reopen_with_autoscrolling_and_previous_uses_api_flag(self):
        panel = self.open_panel("pod")
        panel.tabs.setCurrentWidget(panel.log_view)
        QTest.qWait(20)
        self.assertTrue(panel.log_view.follow.isChecked())
        panel.log_view.pause_follow()
        panel.tabs.setCurrentIndex(0)
        panel.tabs.setCurrentWidget(panel.log_view)
        QTest.qWait(20)
        self.assertTrue(panel.log_view.follow.isChecked())
        self.assertEqual(panel.log_view.export_button.text(), "Сохранить log…")
        self.pod.setdefault('status',{})['containerStatuses'] = [dict(name='app',restartCount=1,containerID='synthetic://2')]
        panel.log_view.update_resource(self.pod)
        self.wait()
        self.assertEqual(panel.log_view.previous.availability,'AVAILABLE')
        panel.log_view.previous.setChecked(True)
        self.wait()
        self.api.logs.assert_called_with("c", "ns", "app-pod", "app", True, 1000)

    def test_terminal_navigation_targets_the_selected_pod_and_uid(self):
        from test_storage import CONFIG
        record = self.window.store.put_cluster('Production', 'https://example.test:6443', 'alice', CONFIG)
        record['id'] = 'c'
        self.window.store.save_clusters([record])
        second = copy.deepcopy(self.pod)
        second["metadata"].update(name="other-pod", uid="22222222-2222-2222-2222-222222222222")
        pods = {self.pod["metadata"]["name"]: self.pod, "other-pod": second}
        self.api.namespaces.return_value = ["ns"]
        self.api.pods.return_value = list(pods.values())
        self.api.replica_sets.return_value = [{"metadata": {"uid": "rs", "ownerReferences": [{"kind": "Deployment", "uid": "d", "controller": True}]}}]
        self.api.resource.side_effect = lambda cid, kind, ns, name: self.deployment if kind == "deployment" else pods[name]
        self.api.store.operation.side_effect = lambda *args, **kwargs: nullcontext()
        self.api.execute_pod.return_value = ("checked", "")
        self.window.open_resource("c", "pod", "ns", "app-pod", "Terminal")
        self.wait()
        old = self.window._resource_panel
        self.window.set_read_only(False)
        index = old.nav_pod.findData("other-pod")
        old.nav_pod.setCurrentIndex(index)
        old.nav_pod.activated.emit(index)
        old.console_input.setText("date")
        old.run_console()
        self.api.execute_pod.assert_not_called()
        self.wait()
        current = self.window._resource_panel
        self.assertEqual(current._opened_identity, ("c", "pod", "ns", "other-pod", second["metadata"]["uid"]))
        self.assertEqual(current.nav_pod.currentData(), "other-pod")
        self.assertTrue(self.window.preferences["read_only"])
        self.window.set_read_only(False)
        current.console_input.setText("printf checked")
        with patch("quaber.ui.confirm") as confirmation:
            current.run_console()
            self.wait()
        confirmation.assert_not_called()
        self.assertEqual(self.api.execute_pod.call_count, 1)
        call = self.api.execute_pod.call_args
        self.assertEqual(call.args, ('c', 'ns', 'other-pod', second['metadata']['uid'], 'app', 'printf checked'))
        from quaber.write_guard import WritePermit
        self.assertIsInstance(call.kwargs['permit'], WritePermit)
        self.assertEqual(call.kwargs['permit'].intent.action, 'exec')
        self.assertEqual(call.kwargs['permit'].intent.identity, current._opened_identity)
        from quaber.container_instance import capture_instance
        from quaber.write_guard import parameter_digest
        instance = capture_instance(second,'app')
        self.assertEqual(call.kwargs['expected_instance'],instance)
        self.assertEqual(call.kwargs['permit'].intent.parameters,parameter_digest(
            {'container':'app','command':'printf checked','instance':instance.parameters()}))

    def test_old_poll_cannot_replace_resource_after_write(self):
        panel = self.open_panel("deployment")
        queue = Deferred()
        panel.background.background = queue
        old = copy.deepcopy(panel.resource)
        panel.reload_deployment_pods()
        changed = copy.deepcopy(old)
        changed["metadata"]["resourceVersion"] = "2"
        panel._updated(changed, notify=False)
        queue.finish((old, [self.pod], [], {"known": True, "controllers": []}))
        self.assertEqual(panel.resource["metadata"]["resourceVersion"], "2")
        self.assertEqual(panel.version, "2")

    def test_hpa_blocks_manual_scaling_like_the_reference_console(self):
        self.api.scaling_policy.return_value = {"known": True, "controllers": ["app-hpa"]}
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        self.assertFalse(panel.replica_up.isEnabled())
        self.assertIn("app-hpa", panel.replica_up.toolTip())
        panel._apply_scale(copy.deepcopy(panel.resource), 2)
        self.api.scale.assert_not_called()

    def test_empty_events_are_explicit_and_changed_card_reports_update(self):
        panel = self.open_panel("pod")
        events = next(panel.tabs.widget(i) for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == "События")
        panel.tabs.setCurrentWidget(events)
        self.wait()
        self.assertEqual(events.rowCount(), 0)
        self.assertEqual(events.empty_label.text(), "Событий нет.")
        self.assertTrue(events.empty_label.isVisible())
        changed = copy.deepcopy(panel.resource)
        changed['metadata']['resourceVersion'] = 'next-version'
        panel._adopt_live(changed)
        self.assertIn("карточка обновлена", self.window.statusBar().currentMessage())


class InterfaceDataTests(unittest.TestCase):
    def test_diff_keeps_original_numbers_and_aligns_replacements(self):
        from quaber.diff_view import aligned_diff
        rows = aligned_diff("--- a\n+++ b\n@@ -10,3 +10,4 @@\n keep\n-old\n+new\n+added\n tail")
        self.assertEqual(rows, [(10, "keep", 10, "keep", False), (11, "old", 11, "new", True),
                                (None, "", 12, "added", True), (12, "tail", 13, "tail", False)])

    def test_rollout_rings_use_uid_and_revision_not_name_prefix(self):
        from quaber.pod_summary import deployment_rings
        dep = {"metadata": {"uid": "d"}, "spec": {"replicas": 3}}
        def replica(uid, revision, replicas):
            return {"metadata": {"uid": uid, "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                     "ownerReferences": [{"uid": "d", "controller": True}]}, "spec": {"replicas": replicas}}
        def pod(uid, state):
            return {"metadata": {"ownerReferences": [{"uid": uid, "controller": True}]}, "status": {"phase": state}}
        old, new = replica("old", 1, 3), replica("new", 2, 1)
        pods = [pod("old", "Running"), pod("new", "Pending")]
        result = deployment_rings(dep, pods, [new, old])
        self.assertTrue(result["rolling"])
        self.assertEqual(result["previous"], [pods[0]])
        self.assertEqual(result["current"], [pods[1]])
        old["spec"]["replicas"] = 0
        self.assertFalse(deployment_rings(dep, [pods[1]], [new, old])["rolling"])
