import copy
import io
import time
import unittest
import urllib.error
from contextlib import nullcontext
from unittest.mock import Mock, patch
from PyQt5.QtCore import QPoint, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QPushButton, QMessageBox

from quaber.access import scope_access, server_forbidden
from quaber.container_selection import preferred_container
from quaber.kubernetes import Kubernetes
from quaber.metric_history import MetricHistory
from quaber.resource_list import ResourceList
from quaber.ui import MetricsWidget
from test_ux import ShellFixture, Deferred


class AccessAndHistoryTests(unittest.TestCase):
    def test_period_change_recovers_hidden_samples_and_retention_is_bounded(self):
        history = MetricHistory()
        key = ("c", "ns", "pod", "uid")
        for timestamp in range(0, 1851, 5):
            history.append(key, [{"name": "app", "cpu": {"usageCores": timestamp}}], timestamp)
        self.assertEqual(history.window(key, "app", 60, 1850)[0][0], 1790)
        self.assertEqual(history.window(key, "app", 300, 1850)[0][0], 1550)
        self.assertEqual(history.window(key, "app", 1800, 1850)[0][0], 50)
        self.assertEqual(history.window(("c", "ns", "pod", "different-uid"), "app", 1800, 1850), [])

    def test_istio_default_is_limited_to_two_containers(self):
        for names, expected in ((["istio-proxy", "app"], "app"), (["app", "ISTIO-sidecar"], "app"),
                                (["istio-proxy"], "istio-proxy"), (["first", "other"], "first"),
                                (["istio-proxy", "a", "b"], "istio-proxy")):
            self.assertEqual(preferred_container(names), expected)

    def test_access_rules_handle_wildcards_named_resources_and_incomplete_results(self):
        self.assertEqual(scope_access({"resourceRules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]})[0], "Чтение")
        self.assertEqual(scope_access({"resourceRules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"], "resourceNames": ["app"]}]})[0], "Чтение")
        result = scope_access({"resourceRules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]})
        self.assertEqual(result[0], "Редактирование")  # namespace rights cannot prove cluster admin
        named = scope_access({"resourceRules": [{"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["update"], "resourceNames": ["app"]}]})
        self.assertIn("отдельные имена", named[1])
        self.assertEqual(scope_access({"resourceRules": [], "incomplete": True})[0], "Не определён")
        self.assertEqual(scope_access({})[0], "Не определён")

    def api(self):
        api = Kubernetes.__new__(Kubernetes)
        api.request = Mock()
        api.namespaces = Mock(return_value=["team"])
        return api

    def test_admin_requires_actual_wildcard_access_and_unknown_is_not_readonly(self):
        api = self.api()
        api.request.return_value = {"status": {"allowed": True}}
        self.assertEqual(api.access_summary("c")["level"], "Администратор")
        api.request.assert_called_once()
        api.request.side_effect = PermissionError("review forbidden")
        self.assertEqual(api.access_summary("c")["level"], "Не определён")

    def test_namespace_access_summary_reports_scoped_rights(self):
        api = self.api()
        api.request.side_effect = [{"status": {"allowed": False}},
            {"status": {"resourceRules": [{"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["update"]}]}},
            {"status": {"resourceRules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]}}]
        profile = api.access_summary("c", ["dev", "prod"])
        self.assertEqual(profile["level"], "Редактирование (частично)")
        self.assertIn("dev: Редактирование", profile["detail"])
        self.assertIn("prod: Чтение", profile["detail"])
        self.assertTrue(all(call.args[2] == "POST" for call in api.request.call_args_list))

    def test_only_forbidden_writes_trigger_administrator_lock(self):
        store, proxy = Mock(), Mock()
        store.clusters.return_value = [{"id": "c", "name": "Fixture", "enabled": True}]
        proxy.url.return_value = "http://127.0.0.1"
        api = Kubernetes(store, proxy)
        api.access_denied = Mock()
        api.opener = Mock()
        for method, path, lock in (("GET", "/api/v1/pods", False),
            ("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", False),
            ("PATCH", "/apis/apps/v1/namespaces/team/deployments/app", True)):
            api.opener.open.side_effect = urllib.error.HTTPError("http://local", 403, "Forbidden", {}, io.BytesIO(b"forbidden"))
            api.access_denied.reset_mock()
            with self.assertRaises(PermissionError):
                api.request("c", path, method, {} if method != "GET" else None)
            self.assertEqual(api.access_denied.called, lock)
        self.assertTrue(server_forbidden("Error from server (Forbidden): pods/exec is forbidden"))
        self.assertFalse(server_forbidden("application: forbidden operation"))


class NextInterfaceTests(ShellFixture, unittest.TestCase):
    def test_same_sidebar_button_closes_nested_cards_with_discard_cancel(self):
        parent = self.open_panel("deployment")
        child = self.open_panel("pod")
        child.yaml_editor.editor.document().setModified(True)
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            self.window.nav_buttons[0].click()
        self.assertIs(self.window._resource_panel, child)
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Discard):
            self.window.nav_buttons[0].click()
        self.assertIsNone(self.window._resource_panel)
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        self.assertEqual(self.window._resource_stack, [])

    def test_configmap_details_share_the_header_left_margin(self):
        panel = self.open_panel("configmap")
        self.app.processEvents()
        self.assertEqual(panel.details.mapTo(panel, QPoint()).x(), 18)
        self.assertLessEqual(panel.context_values["Namespace"].parentWidget().x(), 390)

    def test_hover_covers_white_and_alternate_rows_in_both_themes(self):
        from quaber.ui import item
        panel = self.open_panel("deployment")
        table = panel.pod_table
        panel.tabs.setCurrentWidget(table.parentWidget())
        table.setRowCount(2)
        for row in range(2):
            for column in range(7):
                table.setItem(row, column, item("Running" if column == 0 else f"row {row}"))
        for theme in ("light", "dark"):
            self.window._select_theme(theme)
            table.clearSelection()
            colors = []
            for row in range(2):
                rect = table.visualItemRect(table.topLevelItem(row))
                QTest.mouseMove(table.viewport(), QPoint(130, rect.center().y()))
                self.app.processEvents()
                image = table.viewport().grab().toImage()
                color = image.pixelColor(table.viewport().width()-5, rect.center().y())
                colors.append(color.name())
                self.assertNotEqual(color, table.palette().base().color())
            self.assertEqual(colors[0], colors[1])

    def test_metric_periods_recover_old_samples_and_istio_selection_is_only_initial(self):
        self.pod["spec"]["containers"] = [{"name": "istio-proxy"}, {"name": "app"}]
        panel = self.open_panel("pod")
        self.assertEqual(panel.log_view.container.currentText(), "app")
        self.assertEqual(panel.console_container.currentText(), "app")
        metrics = panel.findChild(MetricsWidget)
        panel.tabs.setCurrentWidget(metrics)
        self.wait()
        metrics.timer.stop()
        self.assertEqual([metrics.period.itemData(i) for i in range(metrics.period.count())], [60, 300, 600, 1800])
        self.assertEqual(metrics.period.currentData(), 60)
        value = {"available": True, "containers": [{"name": "istio-proxy", "cpu": {"usageCores": 1}}, {"name": "app", "cpu": {"usageCores": 2}}]}
        self.api.metrics.return_value = value
        metrics.load(); self.wait()
        self.assertEqual(metrics.container.currentText(), "app")
        metrics.history_store.series.clear()
        now = time.time()
        metrics.history_store.append(metrics.history_identity, value["containers"], now-150)
        metrics.history_store.append(metrics.history_identity, value["containers"], now-10)
        metrics.render()
        self.assertEqual(len(metrics.cpu.values), 1)
        metrics.period.setCurrentIndex(1)
        self.assertEqual(len(metrics.cpu.values), 2)
        self.assertAlmostEqual(metrics.cpu.sample_x(1), metrics.cpu.width()-16-(metrics.cpu.width()-80)*10/300, delta=2)
        metrics.container.setCurrentText("istio-proxy")
        metrics.load(); self.wait()
        self.assertEqual(metrics.container.currentText(), "istio-proxy")

    def test_admin_button_uniform_icons_and_matrix_controls(self):
        self.assertIsInstance(self.window.shell_actions.mode, QPushButton)
        self.assertEqual(self.window.shell_actions.mode.text(), "Режим администратора")
        self.assertFalse(self.window.shell_actions.mode.isChecked())
        buttons = [*self.window.nav_buttons, self.window.settings_button, self.window.shell_actions.mode, self.window.shell_actions.help_button]
        self.assertEqual({button.height() for button in buttons}, {44})
        self.assertEqual({button.iconSize().width() for button in buttons}, {22})
        self.assertEqual(self.window.deploy_mode.parentWidget().objectName(), "refreshStrip")
        self.assertFalse(any(button.text() == "Только проблемы" for button in self.window.findChildren(QPushButton)))
        self.assertIn("Перезапущенные", self.window.recent.text())
        with patch("quaber.ui.confirm", return_value=True):
            self.open_panel("deployment")
            self.window.shell_actions.mode.click()
            self.wait()
        self.assertTrue(self.window.shell_actions.mode.isChecked())
        self.window.access_denied.emit("c", "PATCH", "/resource")
        self.app.processEvents()
        self.assertTrue(self.window.preferences["read_only"])
        self.assertFalse(self.window.shell_actions.mode.isChecked())

    def test_settings_show_effective_rights(self):
        from test_storage import CONFIG
        record = self.window.store.put_cluster("Fixture", "https://example.test:6443", "user", CONFIG)
        self.api.access_summary.return_value = {"level": "Чтение", "detail": "team: Просмотр Pod"}
        observations = []
        def inspect():
            dialog = QApplication.activeModalWidget()
            table = next(table for table in dialog.findChildren(ResourceList) if table.property("clusterSettings"))
            if table.item(0, 6).text() == "Проверка…":
                QTimer.singleShot(10, inspect)
                return
            observations.append((table.item(0, 6).text(), table.topLevelItem(0).toolTip(6)))
            dialog.accept()
        QTimer.singleShot(10, inspect)
        self.window.clusters_dialog()
        self.assertEqual(observations, [("Чтение", "team: Просмотр Pod")])

    def test_terminal_forbidden_turns_admin_mode_off(self):
        panel = self.open_panel("pod")
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        panel.background = deferred = Deferred()
        self.api.store.operation.return_value = nullcontext()
        self.api.proxy.execute.side_effect = RuntimeError('Error from server (Forbidden): pods/exec is forbidden: User "reader" cannot create resource')
        panel.console_input.setText("true")
        panel.run_console()
        deferred.finish(execute=True)
        self.app.processEvents()
        self.assertTrue(self.window.preferences["read_only"])
        self.assertIn("Нет доступа к Terminal", panel.console_output.toPlainText())
