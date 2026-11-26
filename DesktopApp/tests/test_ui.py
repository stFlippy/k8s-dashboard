from qt_helpers import label_text
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PyQt5.QtCore import QTimer, Qt, QPoint
from PyQt5.QtWidgets import QApplication, QWidget, QDialog, QComboBox, QProgressBar
from PyQt5.QtTest import QTest

from quaber.storage import UserStore
from quaber.ui import Background, ClusterDialog, LoginGroupsDialog, MainWindow, ResourceDialog
from test_storage import CONFIG
from quaber.widgets import CodeEditor, NamespaceHeader, StatusDelegate
from quaber.kubernetes import ResourceNotFoundError
from qt_helpers import drag_move


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = UserStore(self.root / "UserData")
        self.parent = QWidget()
        self.parent.background = Background(self.parent)
        self.addCleanup(self.parent.deleteLater)
        self.client = self.root / ("kubectl.exe" if os.name == "nt" else "kubectl")
        self.client.write_bytes(b"fixture")
        self.client.chmod(0o700)

    def wait_for(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), "Qt callback did not finish")

    def make_window(self):
        from helpers_window import create_window
        window = create_window(self,self.store,self.root)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        return window

    def dialog(self, cluster=None):
        dialog = ClusterDialog(self.parent, self.store, self.root, cluster)
        if cluster is None:
            dialog.name.setText("Fixture")
            dialog.server.setText("https://example.test:6443")
            from helpers_accounts import select_account
            select_account(dialog)
        dialog.kubectl_path.setText(str(self.client))
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_check_is_in_worker_callbacks_in_gui_and_gui_keeps_running(self):
        dialog = self.dialog()
        release = threading.Event()
        worker_threads = []
        callback_threads = []
        ticks = []
        gui_thread = threading.get_ident()

        def slow_login(*_):
            worker_threads.append(threading.get_ident())
            release.wait(3)
            return CONFIG

        dialog.status.linkActivated.connect(lambda _: callback_threads.append(threading.get_ident()))
        original_set = dialog.status.setText

        def set_status(text):
            callback_threads.append(threading.get_ident())
            original_set(text)

        try:
            with patch("quaber.ui.login", side_effect=slow_login), patch("quaber.ui.test_connection", return_value=2), patch.object(dialog.status, "setText", side_effect=set_status):
                dialog.connect_cluster(False)
                QTimer.singleShot(20, lambda: ticks.append(True))
                QTest.qWait(80)
                self.assertTrue(ticks)
                self.assertTrue(dialog.busy)
                release.set()
                self.wait_for(lambda: not dialog.busy)
                self.assertIn("namespaces: 2", dialog.status.text())
        finally:
            release.set()
            self.wait_for(lambda: not self.parent.background.running)
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], gui_thread)
        self.assertTrue(callback_threads)
        self.assertEqual(set(callback_threads), {gui_thread})

    def test_missing_path_prompts_for_executable_and_test_does_not_save(self):
        dialog = self.dialog()
        dialog.kubectl_path.clear()
        with patch.dict(os.environ, {"PATH": ""}), patch("quaber.ui.QFileDialog.getOpenFileName", return_value=(str(self.client), "")) as choose, patch("quaber.ui.login", return_value=CONFIG), patch("quaber.ui.test_connection", return_value=2):
            dialog.connect_cluster(False)
            self.wait_for(lambda: not dialog.busy)
            choose.assert_called_once()
            self.assertEqual(dialog.kubectl_path.text(), str(self.client))
            self.assertEqual(self.store.clusters(), [])

    def test_save_prefills_password_and_opt_out_removes_it(self):
        dialog = self.dialog()
        from helpers_accounts import select_account
        account = select_account(dialog, remember=True)
        with patch("quaber.ui.login", return_value=CONFIG):
            dialog.connect_cluster(True)
            self.wait_for(lambda: not dialog.busy)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        cluster = self.store.clusters()[0]
        self.assertNotIn("kubectlPath", cluster)
        self.assertEqual(self.store.preferences()["kubectl_path"], str(self.client.resolve()))
        self.assertEqual(self.store.saved_password(cluster), "fixture-private-password")
        self.assertNotIn("fixture-private-password", self.store.clusters_file.read_text())
        edit = self.dialog(cluster)
        self.assertEqual(edit.account.currentData(), account['id'])
        self.assertFalse(hasattr(edit, 'password'))
        self.store.put_account('alice', 'alice', account_id=account['id'], remember_password=False)
        edit.cluster = self.store.clusters()[0]
        with patch("quaber.ui.login", return_value=CONFIG):
            edit.connect_cluster(True)
            self.wait_for(lambda: not edit.busy)
        self.assertNotIn("encryptedPassword", self.store.clusters()[0])

    def test_failed_check_reenables_form_and_does_not_save(self):
        dialog = self.dialog()
        with patch("quaber.ui.login", side_effect=RuntimeError("Connection failed")):
            dialog.connect_cluster(False)
            self.wait_for(lambda: not dialog.busy)
        self.assertTrue(dialog.buttons.isEnabled())
        self.assertTrue(dialog.form_widget.isEnabled())
        self.assertEqual(self.store.clusters(), [])
        self.assertFalse(hasattr(dialog, 'password'))

    def test_group_login_reuses_saved_password_and_each_cluster_client(self):
        self.parent.store, self.parent.app_dir = self.store, self.root
        self.store.save_preferences({"kubectl_path": str(self.client)})
        first = self.store.put_cluster("First", "https://example.test:6443", "alice", CONFIG,
                                       password="saved-fixture-password", remember_password=True, kubectl_path=str(self.client))
        second = self.store.put_cluster("Second", first["server"], "alice", CONFIG)
        group = LoginGroupsDialog(self.parent, [{"username": "alice", "clusters": [first]}])
        self.addCleanup(group.deleteLater)
        self.assertEqual(group.passwords[0].text(), "saved-fixture-password")
        self.assertFalse(group.remember_passwords[0].isChecked())
        group.remember_passwords[0].setChecked(True)
        group.passwords[0].setText("updated-fixture-password")
        with patch("quaber.ui.login", return_value=CONFIG) as login:
            group.submit()
            self.wait_for(lambda: not group.busy)
            self.assertEqual(login.call_args.args[0], self.client.resolve())
        self.assertEqual(self.store.saved_password(self.store.clusters()[0]), "updated-fixture-password")
        mixed = LoginGroupsDialog(self.parent, [{"username": "alice", "clusters": [first, second]}])
        self.addCleanup(mixed.deleteLater)
        self.assertEqual(mixed.passwords[0].text(), "")

    def test_main_window_starts_with_isolated_empty_data(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        window.close()

    def test_left_navigation_theme_and_hideable_log(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        window.show()
        QTest.qWait(40)
        self.assertIsNone(window.findChild(QWidget, "topbar"))
        self.assertLess(window.sidebar.mapTo(window, QPoint()).x(), window.workspace.mapTo(window, QPoint()).x())
        with patch.object(window, "refresh"):
            window.nav_buttons[2].click()
            self.assertEqual(window.tabs.currentIndex(), 2)
        self.assertFalse(hasattr(window, "theme_selector"))
        window._select_theme("light")
        self.wait_for(lambda: self.store.preferences().get("theme") == "light")
        self.assertEqual(window.palette().window().color().name(), "#ffffff")
        self.assertIn("#005FB8", window.styleSheet())
        window._select_theme("dark")
        self.assertEqual(window.palette().window().color().name(), "#151b24")
        self.assertFalse(window.log_panel.isVisible())
        window.log_action.trigger()
        QTest.qWait(20)
        self.assertTrue(window.log_panel.isVisible())
        self.assertIs(window.tabs.currentWidget(), window.log_panel)
        self.assertFalse(window.log_panel.isWindow())
        self.assertTrue(window.nav_buttons[window.tabs.indexOf(window.log_panel)].isChecked())
        window.nav_buttons[0].click()
        self.assertFalse(window.log_panel.isVisible())
        window.log_action.trigger()
        self.assertTrue(window.log_panel.isVisible())
        self.assertEqual(window.log_action.shortcut().toString(), "Ctrl+J")
        self.assertGreater(len(window.log_panel.output.toPlainText()), 0)
        window.close()

    def test_theme_is_available_in_settings_and_resource_lists_keep_identity(self):
        from quaber.resource_list import ResourceList
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        observed = []
        def edit_settings():
            dialog = window.findChildren(QDialog)[-1]
            selector = dialog.findChild(QComboBox, "settingsTheme")
            observed.append(selector is not None)
            selector.setCurrentIndex(1)
            observed.append(bool(dialog.findChild(ResourceList)))
            dialog.accept()
        QTimer.singleShot(20, edit_settings)
        window.clusters_dialog()
        self.assertEqual(observed, [True, True])
        self.wait_for(lambda: self.store.preferences().get("theme") == "light")
        window.close()

    def test_view_color_covers_canvas_and_updates_each_tab(self):
        from quaber.view_tabs import blended_color, color_icon
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        window._set_view_color("#3b82f6")
        expected = blended_color("#151B24", "#3b82f6")
        self.assertEqual(window.matrix.palette().base().color().name(), expected)
        self.assertIn("QWidget#matrixCanvas", window.matrix_page.styleSheet())
        self.assertEqual(window.view_bar.colors[0], "#3b82f6")
        self.wait_for(lambda: self.store.preferences().get("views", [{}])[0].get("color") == "#3b82f6")
        self.assertFalse(color_icon("#3b82f6", "#151b24").isNull())
        window._set_view_color("transparent")
        self.assertEqual(window.matrix.palette().base().color().name(), "#151b24")
        window.close()

    def test_quota_values_are_outside_bars_and_unknown_is_not_zero(self):
        from quaber.quota_card import QuotaCard, quota_percent
        self.assertEqual(quota_percent("500m", "2"), 25)
        self.assertAlmostEqual(quota_percent("320Mi", "1536Mi"), 100*320/1536)
        self.assertIsNone(quota_percent("3", "0"))
        self.assertIsNone(quota_percent("bad", "10"))
        card = QuotaCard("Fixture", "payments", {"metadata": {"name": "quota"},
                         "status": {"used": {"limits.cpu": "500m", "pods": "12"}, "hard": {"limits.cpu": "2", "pods": "10"}}})
        self.addCleanup(card.deleteLater)
        bars = card.findChildren(QProgressBar)
        self.assertEqual([bar.value() for bar in bars], [250, 1000])
        self.assertTrue(all(not bar.isTextVisible() for bar in bars))
        self.assertEqual(bars[1].property("level"), "critical")

    def test_resource_panel_follows_workspace_when_log_and_sidebar_resize(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        window.show()
        QTest.qWait(40)
        resource = {"metadata": {"name": "settings", "namespace": "payments"}, "data": {}}
        with patch.object(window.api, "resource", return_value=resource), patch.object(window.api, "cluster", return_value={"name": "Fixture"}):
            window.open_resource("cluster", "configmap", "payments", "settings")
            self.wait_for(lambda: not window.background.running)
        panel = window._resource_panel
        self.assertIs(panel.parent(), window.workspace)
        self.assertIs(panel.controller, window)
        self.assertEqual(panel.height(), window.workspace.height())
        previous_height = panel.height()
        window.resize(window.width()+20, window.height())
        QTest.qWait(30)
        self.assertEqual(panel.height(), previous_height)
        self.assertEqual(panel.height(), window.workspace.height())
        window.shell_splitter.setSizes([250, 900])
        QTest.qWait(30)
        self.assertEqual(window._resource_overlay.geometry(), window.workspace.rect())
        self.assertLessEqual(panel.width(), window.workspace.width())
        panel.close()
        window.close()

    def test_saved_password_renews_automatically_without_form(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        cluster = self.store.put_cluster("Expired", "https://example.test:6443", "alice", CONFIG,
                                         password="saved-fixture-password", remember_password=True)
        with patch("quaber.authorization.locate_kubectl", return_value=self.client), patch("quaber.authorization.login", return_value=CONFIG) as login, patch("quaber.ui.LoginGroupsDialog") as form, patch.object(window, "refresh"):
            window._prompt_expired_login()
            self.wait_for(lambda: not window._auth_busy)
            login.assert_called_once()
            form.assert_not_called()
        self.assertNotEqual(self.store.clusters()[0]["encryptedKubeconfig"], cluster["encryptedKubeconfig"])
        window.close()

    def test_missing_password_prompts_once_and_cancel_does_not_loop(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        self.store.put_cluster("Expired", "https://example.test:6443", "alice", CONFIG)
        with patch("quaber.ui.LoginGroupsDialog") as form, patch("quaber.authorization.login") as login, patch.object(window, "refresh"):
            form.return_value.exec.return_value = QDialog.DialogCode.Rejected
            window._prompt_expired_login()
            self.wait_for(lambda: not window._auth_busy)
            window._prompt_expired_login()
            form.assert_called_once()
            login.assert_not_called()
        window.close()

    def test_matrix_hover_follows_frozen_column_and_clears_on_scroll(self):
        from quaber.widgets import PodMatrix
        from PyQt5.QtCore import QEvent
        from PyQt5.QtWidgets import QTableWidgetItem
        matrix = PodMatrix()
        self.addCleanup(matrix.deleteLater)
        matrix.resize(400, 200)
        matrix.setColumnCount(3)
        matrix.setRowCount(30)
        matrix.setItem(0, 1, QTableWidgetItem("pod"))
        from quaber.matrix_presentation import PodVisual
        matrix._cell_keys[(0, 1)] = ("c", "ns", "app")
        matrix._cell_visuals[("c", "ns", "app")] = (PodVisual(
            ("c", "pod", "ns", "pod", "uid"), "Running", "#43B694", False, False, "pod"),)
        matrix.sync_frozen()
        matrix.show()
        QTest.qWait(20)
        self.assertEqual(matrix.frozen.viewport().mapTo(matrix, QPoint()), matrix.viewport().geometry().topLeft())
        self.assertEqual(matrix.frozen.width(), matrix.columnWidth(0))
        for row in range(matrix.rowCount()):
            self.assertEqual(matrix.frozen.rowHeight(row), matrix.rowHeight(row))
        from quaber.widgets import pod_rects
        point = pod_rects(matrix.visualItemRect(matrix.item(0, 1)), 1)[0].center()
        QTest.mouseMove(matrix.viewport(), point)
        self.assertEqual(matrix.hovered, (0, 1))
        self.assertEqual(matrix.hovered_pod, 0)
        QTest.mouseMove(matrix.frozen.viewport(), QPoint(10, 10))
        self.assertEqual(matrix.hovered, (0, 0))
        matrix._set_hover((0, 1))
        matrix.verticalScrollBar().setValue(1)
        self.assertEqual(matrix.hovered, (-1, -1))
        self.assertLessEqual(matrix.frozen.height(), sum(matrix.rowHeight(row) for row in range(matrix.rowCount())))
        self.assertFalse(matrix.showGrid())
        self.assertFalse(matrix.frozen.showGrid())
        matrix.close()

    def test_full_filter_hit_area_tabs_and_metric_scale(self):
        from quaber.widgets import FullCheckBox
        from quaber.ui import MetricChart
        from quaber.view_tabs import ViewTabs
        box = FullCheckBox("Running")
        box.resize(180, 36)
        box.show()
        QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=QPoint(175, 18))
        self.assertTrue(box.isChecked())
        box.close()
        tabs = ViewTabs()
        tabs.addTab("A full view name")
        self.assertGreaterEqual(tabs.tabSizeHint(0).width(), tabs.fontMetrics().horizontalAdvance(tabs.tabText(0)) + 24)
        chart = MetricChart("CPU", "m")
        chart.resize(400, 160)
        chart.set_data([10, None, 30, 120], request=40, limit=100)
        self.assertEqual(chart.maximum(), 100)
        chart.show()
        QTest.mouseMove(chart, QPoint(round(chart.sample_x(2)), 80))
        self.assertEqual(chart.hover_index, 2)
        chart.grab()
        chart.close()

    def test_resource_list_blank_space_clears_selection(self):
        from quaber.resource_list import ResourceList
        from PyQt5.QtWidgets import QTableWidgetItem
        table = ResourceList()
        table.resize(400, 300)
        table.setColumnCount(1)
        table.setRowCount(1)
        table.setItem(0, 0, QTableWidgetItem("cluster"))
        table.selectRow(0)
        table.show()
        QTest.qWait(10)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(30, 220))
        self.assertFalse(table.selectedItems())
        self.assertEqual(table.currentRow(), -1)
        table.close()

    def test_matrix_resize_from_cell_boundary(self):
        from quaber.widgets import PodMatrix
        matrix = PodMatrix()
        matrix.resize(500, 250)
        matrix.setColumnCount(3)
        matrix.setRowCount(3)
        matrix.setColumnWidth(0, 130)
        matrix.show()
        QTest.qWait(10)
        edge = matrix.columnWidth(0)
        point = QPoint(edge, 12)
        QTest.mousePress(matrix.viewport(), Qt.MouseButton.LeftButton, pos=point)
        drag_move(matrix.viewport(), point + QPoint(24, 0))
        QTest.mouseRelease(matrix.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(24, 0))
        self.assertEqual(matrix.columnWidth(0), 154)
        self.assertIsNone(matrix._resize_drag)
        matrix.close()

    def test_closed_deployment_discards_inflight_results(self):
        release = threading.Event()
        resource = {"metadata": {"name": "demo", "uid": "uid", "namespace": "default"}, "spec": {"replicas": 1}, "status": {}}
        api = Mock()
        api.cluster.return_value = {"name": "fixture"}
        api.resource.side_effect = lambda *_: (release.wait(2), resource)[1]
        api.pods.return_value = []
        api.replica_sets.return_value = []
        api.events.return_value = []
        panels = []
        try:
            for _ in range(8):
                panel = ResourceDialog(self.parent, api, self.parent.background, "cluster", "deployment", "default", "demo", resource)
                panels.append(panel)
                panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                panel.show()
                self.assertEqual(panel.tabs.tabText(0), "Обзор")
                panel.close()
                self.assertFalse(panel.background.active)
            release.set()
            self.wait_for(lambda: not self.parent.background.running)
            QTest.qWait(20)
        finally:
            release.set()

    def test_matrix_counters_respect_scope_but_include_hidden_statuses(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        window.timer.stop()
        QTest.qWait(30)
        deployments = [{"metadata": {"name": name, "uid": name}, "spec": {"replicas": 1}} for name in ("first", "second")]
        pods = [{"metadata": {"name": name+"-pod", "ownerReferences": [{"controller": True, "uid": name, "kind": "Deployment"}]}, "status": {"phase": "Running"}} for name in ("first", "second")]
        window.store.clusters = lambda: [dict(id="cluster", name="Fixture", enabled=True)]
        window.snapshot = {"cluster": {"name": "Fixture", "namespaces": {"payments": {"deployments": deployments, "pods": pods, "replica_sets": []}}}}
        window.render_matrix()  # Discover objects before the operator hides an existing row.
        window.views.active["selectedDeployments"] = ["first"]
        window.render_matrix()
        self.assertEqual(label_text(window.summary_label), "1  Total Pods")
        self.assertEqual(window.status_filters["Running"].text(), "Running  1")
        window.status_filters["Running"].setChecked(False)
        self.assertEqual(label_text(window.summary_label), "1  Total Pods")
        self.assertEqual(window.status_filters["Running"].text(), "Running  1")
        window.close()

    def test_pod_navigation_logs_and_back(self):
        api = Mock()
        api.cluster.return_value = {"name": "Fixture"}
        api.namespaces.return_value = ["payments"]
        deployment = {"metadata": {"name": "checkout", "uid": "deployment"}}
        api.deployments.return_value = [deployment]
        api.replica_sets.return_value = [{"metadata": {"uid": "rs", "ownerReferences": [{"controller": True, "kind": "Deployment", "uid": "deployment"}]}}]
        pods = [{"metadata": {"name": name, "labels": {"app": "checkout"}, "ownerReferences": [{"controller": True, "kind": "ReplicaSet", "uid": "rs"}]}, "spec": {"containers": [{"name": "app"}]}, "status": {"phase": "Running"}} for name in ("checkout-a", "checkout-b")]
        api.pods.return_value = pods
        api.events.return_value = []
        api.logs.return_value = "\n".join(str(n) for n in range(1500))
        self.parent.store = Mock()
        self.parent.store.clusters.return_value = [{"id": "cluster", "name": "Fixture", "enabled": True}]
        self.parent.open_resource = Mock()
        panel = ResourceDialog(self.parent, api, self.parent.background, "cluster", "pod", "payments", "checkout-a", pods[0])
        self.addCleanup(panel.deleteLater)
        panel.show()
        panel.log_timer.stop()
        self.wait_for(lambda: not self.parent.background.running)
        self.assertEqual(panel.nav_deployment.currentData(), "checkout")
        self.assertEqual(panel.nav_pod.count(), 2)
        self.assertEqual(panel.log_view.fragment_label.text(), "Последние 1000 строк")
        api.logs.assert_not_called()
        panel.tabs.setCurrentWidget(panel.log_view)
        self.wait_for(lambda: not self.parent.background.running)
        api.logs.assert_called_with("cluster", "payments", "checkout-a", "app", False, 1000)
        panel.tabs.setCurrentIndex(next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == "Логи"))
        QTest.qWait(30)
        scroll = panel.logs.verticalScrollBar()
        self.assertEqual(scroll.value(), scroll.maximum())
        QTest.keyClick(panel.logs, Qt.Key.Key_PageUp)
        from quaber.polling import poll_interval
        self.assertEqual(poll_interval(panel.log_timer), 5000)
        self.assertTrue(5000 <= panel.log_timer.interval() <= 6000)
        self.assertFalse(panel.follow_logs.isChecked())
        panel.all_logs.setChecked(True)
        self.wait_for(lambda: not self.parent.background.running)
        api.logs.assert_called_with("cluster", "payments", "checkout-a", "app", False, None)
        panel.nav_pod.setCurrentIndex(1)
        panel.nav_pod.activated.emit(1)
        self.parent.open_resource.assert_called_with("cluster", "pod", "payments", "checkout-b", initial_tab="Логи", expected_uid=None)
        panel.back_target = ("cluster", "deployment", "payments", "checkout")
        panel._go_back()
        self.parent.open_resource.assert_called_with("cluster", "deployment", "payments", "checkout")

    def test_full_log_request_omits_server_tail_limit(self):
        from quaber.kubernetes import Kubernetes
        api = Kubernetes.__new__(Kubernetes)
        api.request = Mock(return_value="log")
        api.logs("c", "ns", "p", "app", tail=None)
        query = api.request.call_args.args[1]
        self.assertNotIn("tailLines", query)
        self.assertIn("limitBytes=4194305", query)
        api.logs("c", "ns", "p", "app")
        self.assertIn("tailLines=1000", api.request.call_args.args[1])

    def test_overlay_close_is_void_and_nested_cards_preserve_parent(self):
        import sys
        from PyQt5.QtWidgets import QMessageBox
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        window.resize(1450, 900)
        window.show()
        QTest.qWait(30)
        deployment = {"metadata": {"name": "checkout", "uid": "d"}, "spec": {"replicas": 1}, "status": {}}
        pod = {"metadata": {"name": "checkout-pod", "uid": "p"}, "spec": {"containers": [{"name": "app"}]}, "status": {"phase": "Running"}}
        api = Mock()
        api.resource.side_effect = lambda c, kind, ns, name: deployment if kind == "deployment" else pod
        api.cluster.return_value = {"name": "Fixture"}
        api.pods.return_value = []
        api.replica_sets.return_value = []
        api.events.return_value = []
        api.logs.return_value = "line"
        window.api = api
        with patch.object(sys, "excepthook", side_effect=sys.__excepthook__) as unhandled:
            for _ in range(8):
                window.open_resource("c", "deployment", "ns", "checkout")
                self.wait_for(lambda: not window.background.running)
                QTest.mouseClick(window._resource_overlay, Qt.MouseButton.LeftButton, pos=QPoint(10, 200))
                self.assertIsNone(window._dismiss_resource_event(None))
                QTest.qWait(10)
                self.assertFalse(window._resource_stack)
            unhandled.assert_not_called()
        window.open_resource("c", "deployment", "ns", "checkout")
        self.wait_for(lambda: not window.background.running)
        parent = window._resource_panel
        parent.yaml_editor.editor.insertPlainText("# draft\n")
        draft = parent.yaml_editor.toPlainText()
        window.open_resource("c", "pod", "ns", "checkout-pod")
        self.wait_for(lambda: not window.background.running)
        child = window._resource_panel
        self.assertEqual(window._resource_stack, [parent, child])
        self.assertFalse(parent.isWindow())
        self.assertFalse(child.isWindow())
        self.assertIs(parent.parentWidget(), window.workspace)
        self.assertIs(child.parentWidget(), window.workspace)
        self.assertIs(parent.window(), window)
        self.assertIs(child.window(), window)
        self.assertFalse(parent.isVisible())
        self.assertEqual(child.x(), parent.x())
        self.assertEqual(parent.yaml_editor.toPlainText(), draft)
        child.yaml_editor.editor.insertPlainText("# unsaved\n")
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            child._go_back()
        self.assertIs(window._resource_panel, child)
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard) as warning:
            child._go_back()
            warning.assert_called_once()
        self.assertIs(window._resource_panel, parent)
        self.assertNotEqual(parent.yaml_editor.toPlainText(), draft)
        self.assertFalse(parent.yaml_editor.editor.document().isModified())
        with patch("quaber.ui.QMessageBox.warning") as warning:
            window.nav_buttons[2].click()
        warning.assert_not_called()
        self.assertIsNone(window._resource_panel)
        self.assertEqual(window.tabs.currentIndex(), 2)
        window.close()

    def test_editor_search_counts_and_wraps(self):
        editor = CodeEditor()
        self.addCleanup(editor.deleteLater)
        editor.setPlainText("first\nneedle\nneedle")
        editor.search.setText("needle")
        editor.find_match()
        self.assertEqual(editor.count.text(), "1/2")
        editor.find_match()
        self.assertEqual(editor.count.text(), "2/2")
        editor.find_match()
        self.assertEqual(editor.count.text(), "1/2")
        self.assertEqual(editor.numbers.toPlainText(), "1\n2\n3")

    def test_matrix_has_individual_pods_and_direct_click(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        pod = {"metadata": {"name": "checkout-pod", "namespace": "payments", "uid": "33333333-3333-3333-3333-333333333333",
                            "resourceVersion": "pod:1", "ownerReferences": [{"kind": "Deployment", "controller": True, "uid": "deployment"}]},
               "status": {"phase": "Running"}}
        window.store.clusters = lambda: [dict(id="cluster", name="Fixture", enabled=True)]
        window.snapshot = {"cluster": {"name": "Development", "namespaces": {"payments": {
            "deployments": [{"metadata": {"name": "checkout", "uid": "deployment"}, "spec": {"replicas": 1}, "status": {"availableReplicas": 1}}],
            "pods": [pod], "replica_sets": []}}}}
        window.render_matrix()
        window.show()
        QTest.qWait(30)
        self.assertIsInstance(window.matrix.horizontalHeader(), NamespaceHeader)
        cell = window.matrix.item(0, 1)
        self.assertEqual(window.matrix.cell_visuals(0, 1)[0].identity[3], "checkout-pod")
        self.assertNotIn("Pod", cell.text())
        rect = window.matrix.visualItemRect(cell)
        with patch.object(window, "open_resource") as opened:
            QTest.mouseClick(window.matrix.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.center().x(), rect.top()+window.matrix.group_offset(0)+12))
            opened.assert_called_once_with("cluster", "pod", "payments", "checkout-pod", expected_uid=pod['metadata']['uid'])
        window.close()

    def test_deployment_pods_have_live_metrics_age_and_stable_identity(self):
        from datetime import datetime, timezone, timedelta
        deployment = {"metadata": {"name": "checkout", "namespace": "payments", "uid": "deployment", "resourceVersion": "1"},
                      "spec": {"replicas": 1}, "status": {"availableReplicas": 1}}
        pod = {"metadata": {"name": "checkout-original", "uid": "pod-uid", "creationTimestamp": (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),
                            "ownerReferences": [{"controller": True, "uid": "deployment", "kind": "Deployment"}]},
               "status": {"phase": "Running", "podIP": "127.0.0.1"}}
        api = Mock()
        api.cluster.return_value = {"name": "Fixture"}
        api.resource.return_value = deployment
        api.pods.return_value = [pod]
        api.replica_sets.return_value = []
        api.events.return_value = []
        api.metrics.return_value = {"available": True, "containers": [{"name": "app", "cpu": {"usageCores": .25, "limitCores": 1}, "memory": {"usageBytes": 128, "limitBytes": 256}}]}
        api.metrics_many.return_value = {('checkout-original','pod-uid'): api.metrics.return_value}
        self.parent.open_resource = Mock()
        panel = ResourceDialog(self.parent, api, self.parent.background, "cluster", "deployment", "payments", "checkout", deployment)
        self.addCleanup(panel.deleteLater)
        self.wait_for(lambda: not self.parent.background.running)
        self.assertEqual(panel.pod_table.item(0, 2).text(), "10 мин")
        api.metrics.assert_not_called()
        panel.show()
        panel.tabs.setCurrentIndex(next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == 'Pods'))
        self.wait_for(lambda: not self.parent.background.running)
        self.assertEqual(panel.pod_table.item(0, 5).text(), "25%")
        self.assertEqual(panel.pod_table.item(0, 6).text(), "50%")
        self.assertIsInstance(panel.pod_table.itemDelegateForColumn(0), StatusDelegate)
        panel.pod_table.item(0, 1).setText("display-text-is-not-an-identity")
        panel._open_deployment_pod(0, 1)
        self.parent.open_resource.assert_called_once_with("cluster", "pod", "payments", "checkout-original", expected_uid="pod-uid")
        api.pods.return_value = []
        panel.reload_deployment_pods()
        self.wait_for(lambda: not self.parent.background.running)
        self.assertEqual(panel.pod_table.rowCount(), 0)
        panel.close()

    def test_vanished_pod_refreshes_without_raw_error_dialog(self):
        window = self.make_window()
        self.addCleanup(window.deleteLater)
        window.timer.stop()
        QTest.qWait(30)
        cluster = self.store.put_cluster("Fixture", "https://example.test:6443", "alice", CONFIG)
        with patch.object(window.api, "resource", side_effect=ResourceNotFoundError("gone")), patch.object(window, "refresh") as refresh, patch("quaber.ui.error") as popup:
            window.open_resource(cluster['id'], "pod", "payments", "old-pod")
            self.wait_for(lambda: not window.background.running)
            refresh.assert_called_once()
            popup.assert_not_called()
            self.assertIn("больше не существует", window.statusBar().currentMessage())
        window.close()


if __name__ == "__main__":
    unittest.main()
