import copy
import csv
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt6.QtCore import QPoint, QPointF, Qt, QEvent
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest

from quaber.inventory import APP_LABEL, CSV_FIELDS, inventory_record, collect_inventory, compare_versions, write_inventory
from quaber.ui import MetricChart
from test_ux import ShellFixture, Deferred
from test_storage import CONFIG


def deployment(name="orders", version="1", branch="main"):
    return {"metadata": {"name": name, "labels": {APP_LABEL: "orders", "app.kubernetes.io/version": version, "GIT_BRANCH": branch}},
            "spec": {"template": {"metadata": {"labels": {}}, "spec": {
                "containers": [{"name": "app", "image": "registry/app:1"}],
                "initContainers": [{"name": "init", "image": "registry/init:2"}]}}}}


def snapshot(cid, name="orders", version="1", branch="main"):
    return {"cluster_id": cid, "cluster_name": cid, "server": f"https://{cid}.test:6443",
            "namespaces": ["ns-"+cid],
            "captured_at": datetime.now(timezone.utc).isoformat(), "namespace_count": 1, "errors": [],
            "records": [inventory_record(deployment(name, version, branch), "Deployment", "ns-"+cid)]}


class InventoryTests(unittest.TestCase):
    def test_alias_precedence_matches_batch_script_and_includes_init_images(self):
        resource = deployment()
        resource["metadata"]["labels"].pop("GIT_BRANCH")
        resource["metadata"]["labels"]["vtb.ru/branch"] = "outer"
        resource["spec"]["template"]["metadata"]["labels"] = {"GIT_BRANCH": "inner", "STARTED_BY": "user"}
        record = inventory_record(resource, "Deployment", "test")
        self.assertEqual(record["branch"], "outer")
        self.assertEqual(record["deployed_by"], "user")
        self.assertEqual(record["images"], ["registry/app:1", "registry/init:2"])

    def test_cronjob_falls_back_through_job_and_pod_metadata(self):
        cron = {"metadata": {"name": "cleanup"}, "spec": {"jobTemplate": {
            "metadata": {"labels": {"GIT_BRANCH": "job", "vtb.ru/started_by": "runner"}},
            "spec": {"template": deployment(version="2")["spec"]["template"]}}}}
        cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "pod-version"
        record = inventory_record(cron, "CronJob", "test")
        self.assertEqual((record["branch"], record["deployed_by"], record["version"]), ("job", "runner", "pod-version"))

    def test_collector_keeps_successful_types_and_reports_forbidden_scopes(self):
        api = Mock()
        def read(cid, path):
            if path == "/api/v1/namespaces":
                return [{"metadata": {"name": "test"}}]
            if path.endswith("/deployments"):
                return [deployment()]
            raise PermissionError("Forbidden cronjobs")
        api.list_pages.side_effect = read
        result = collect_inventory(api, {"id": "c", "name": "Cluster", "server": "https://api.test"})
        self.assertEqual(result["namespace_count"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertIn("test / CronJob", result["errors"][0])
        api.request.assert_not_called()

    def test_csv_format_unicode_quoting_and_log_without_credentials(self):
        data = snapshot("c", branch='release,"test"')
        data["records"][0]["deployed_by"] = "Владимир"
        data["encryptedKubeconfig"] = "credential-must-not-leak"
        data["errors"] = ["other / CronJob: Forbidden"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"снимок.csv"
            _, log = write_inventory(path, [data])
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(tuple(reader.fieldnames), CSV_FIELDS)
                rows = list(reader)
            self.assertEqual(rows[0]["branch"], 'release,"test"')
            self.assertEqual(rows[0]["deployed by"], "Владимир")
            self.assertIn("НЕПОЛНЫЙ СНИМОК", Path(log).read_text(encoding="utf-8"))
            self.assertNotIn("credential-must-not-leak", path.read_text(encoding="utf-8-sig")+Path(log).read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                write_inventory(Path(folder)/"bad.log", [data])

    def test_versions_match_application_label_across_resource_names_and_namespaces(self):
        first, second = snapshot("a", "orders-blue"), snapshot("b", "orders-green")
        rows, skipped = compare_versions([first, second], ["app.kubernetes.io/version"])
        self.assertEqual((rows[0]["service"], rows[0]["status"], skipped), ("orders", "Совпадает", []))
        second["records"][0]["labels"]["app.kubernetes.io/version"] = "2"
        self.assertEqual(compare_versions([first, second], ["GIT_BRANCH"])[0][0]["status"], "Совпадает")
        self.assertEqual(compare_versions([first, second], ["GIT_BRANCH", "app.kubernetes.io/version"])[0][0]["status"], "Различается")

    def test_missing_labels_partial_reads_and_duplicate_versions_are_explicit(self):
        first, second = snapshot("a"), snapshot("b")
        second["records"][0]["labels"].pop(APP_LABEL)
        rows, skipped = compare_versions([first, second], ["app.kubernetes.io/version"])
        self.assertEqual(rows[0]["status"], "Отсутствует")
        self.assertEqual(len(skipped), 1)
        second = snapshot("b")
        second["records"][0]["labels"].pop("app.kubernetes.io/version")
        self.assertEqual(compare_versions([first, second], ["app.kubernetes.io/version"])[0][0]["status"], "Различается")
        second = snapshot("b")
        second["errors"] = ["Forbidden another namespace"]
        self.assertEqual(compare_versions([first, second], ["app.kubernetes.io/version"])[0][0]["status"], "Неполные данные")
        second["records"] = []
        self.assertEqual(compare_versions([first, second], ["app.kubernetes.io/version"])[0][0]["cells"]["b"][0], "Нет данных")
        second = snapshot("b")
        second["records"].append(inventory_record(deployment("another", "2"), "Deployment", "another-ns"))
        self.assertEqual(compare_versions([first, second], ["app.kubernetes.io/version"])[0][0]["status"], "Различается")


class NewPageTests(ShellFixture, unittest.TestCase):
    def clusters(self):
        return [self.window.store.put_cluster(name, f"https://{name}.test:6443", "user",
                    CONFIG.replace("https://example.test:6443", f"https://{name}.test:6443")) for name in ("a", "b")]

    def cache(self, clusters):
        for cluster in clusters:
            data = snapshot(cluster["id"])
            data.update(cluster_name=cluster["name"], server=cluster["server"])
            self.window.inventory.snapshots[cluster["id"]] = (self.window.inventory.signature(cluster), time.monotonic(), data)

    def test_new_pages_are_readonly_and_export_uses_only_selected_clusters(self):
        clusters = self.clusters()
        self.cache(clusters)
        for cluster in clusters:
            data = self.window.inventory.get(cluster["id"])
            data["records"][0]["deployed_by"] = "Владимир"
            data["records"].append(inventory_record({"metadata": {"name": "nightly"}, "spec": {"jobTemplate": {
                "metadata": {"labels": {"GIT_BRANCH": "cron-branch", "STARTED_BY": "pipeline"}},
                "spec": {"template": {"metadata": {"labels": {"app.kubernetes.io/version": "3"}}, "spec": {
                    "containers": [{"image": "registry/nightly:3"}]}}}}}}, "CronJob", "jobs"))
        self.window.navigate_workspace(self.window.tabs.indexOf(self.window.dumps_page))
        page = self.window.dumps_page
        self.assertEqual(page.table.rowCount(), 4)
        page.clusters.selected = {clusters[0]["id"]}
        page.refresh_data()
        self.assertEqual(page.table.rowCount(), 2)
        self.assertEqual(page.table.columnCount(), 8)
        self.assertEqual([page.table.item(1, column).text() for column in range(8)],
                         [clusters[0]["server"], "jobs", "CronJob", "nightly", "registry/nightly:3", "cron-branch", "pipeline", "3"])
        self.assertFalse(self.window.shell_actions.mode.isEnabled())
        self.assertTrue(page.export_button.isEnabled())
        with tempfile.TemporaryDirectory() as folder, patch("quaber.inventory_pages.QFileDialog.getSaveFileName", return_value=(str(Path(folder)/"dump.csv"), "CSV")):
            page.export()
            self.wait()
            with (Path(folder)/"dump.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["HOST"], clusters[0]["server"])
            self.assertEqual(rows[0]["CONTAINER_IMAGES"], "registry/app:1;registry/init:2")
            self.assertEqual(rows[0]["deployed by"], "Владимир")
            self.assertEqual([[page.table.item(row, column).text() for column in range(8)] for row in range(2)],
                             [[row[key] for key in CSV_FIELDS] for row in rows])
            self.assertFalse((Path(folder)/"dump.log").exists())
        self.api.scale.assert_not_called()

    def test_versions_page_exposes_arbitrary_labels_and_marks_actual_differences(self):
        clusters = self.clusters()
        self.cache(clusters)
        self.window.navigate_workspace(self.window.tabs.indexOf(self.window.versions_page))
        page = self.window.versions_page
        self.app.processEvents()
        self.assertEqual(page.label_keys, ["app.kubernetes.io/version"])
        self.assertIn("GIT_BRANCH", page.available_labels)
        self.assertEqual(page.table.item(0, 1).text(), "Совпадает")
        self.window.inventory.get(clusters[1]["id"])["records"][0]["labels"]["GIT_BRANCH"] = "release"
        page.label_keys = ["GIT_BRANCH"]
        page.render()
        self.assertEqual(page.table.item(0, 1).text(), "Различается")
        self.assertFalse(self.window.shell_actions.mode.isEnabled())
        self.assertEqual([button.text() for button in self.window.nav_buttons][-2:], ["Dumps", "Сверка версий"])
        with patch.object(page, "refresh_data") as refreshed:
            self.window.shell_actions.refresh()
        refreshed.assert_called_once_with(force=True)
        for data in page.snapshots():
            for record in data["records"]:
                record["labels"] = {APP_LABEL: record["labels"][APP_LABEL]}
        page.label_keys = None
        page.render()
        self.assertEqual(page.table.item(0, 1).text(), "Нет меток версий")

    def test_comparison_colors_explain_matches_differences_and_unknowns_in_both_themes(self):
        clusters = self.clusters()
        self.cache(clusters)
        self.window.navigate_workspace(self.window.tabs.indexOf(self.window.versions_page))
        page = self.window.versions_page
        second = self.window.inventory.get(clusters[1]["id"])
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                second["records"][0]["labels"]["app.kubernetes.io/version"] = "1"
                self.window._select_theme(theme)
                for column in range(page.table.columnCount()):
                    color = page.table.item(0, column).data(Qt.ItemDataRole.ForegroundRole)
                    self.assertGreater(color.green(), color.red())
                self.assertIn("одинаковые", page.table.item(0, 1).data(Qt.ItemDataRole.ToolTipRole))
                second["records"][0]["labels"]["app.kubernetes.io/version"] = "2"
                page.render()
                self.assertEqual(page.table.item(0, 1).text(), "Различается")
                for column in range(page.table.columnCount()):
                    color = page.table.item(0, column).data(Qt.ItemDataRole.ForegroundRole)
                    self.assertGreater(color.red(), color.green())
                self.assertIn("различаются", page.table.item(0, 1).data(Qt.ItemDataRole.ToolTipRole))
                second["errors"] = ["Forbidden"]
                page.render()
                self.assertEqual(page.table.item(0, 1).text(), "Неполные данные")
                self.assertIsNone(page.table.item(0, 1).data(Qt.ItemDataRole.ForegroundRole))
                self.assertIn("ошибки", page.table.item(0, 1).data(Qt.ItemDataRole.ToolTipRole))
                second["errors"] = []
        second["records"] = []
        page.render()
        self.assertEqual(page.table.item(0, 1).text(), "Отсутствует")
        color = page.table.item(0, 3).data(Qt.ItemDataRole.ForegroundRole)
        self.assertGreater(color.red(), color.green())

    def test_latest_inventory_generation_wins(self):
        cluster = self.clusters()[0]
        deferred = Deferred()
        service = self.window.inventory
        with patch.object(self.window, "background", deferred):
            service.refresh({cluster["id"]})
            service.refresh({cluster["id"]}, force=True)
            deferred.finish(snapshot(cluster["id"], version="old"))
            self.assertIsNone(service.get(cluster["id"]))
            deferred.finish(snapshot(cluster["id"], version="new"))
        self.assertEqual(service.get(cluster["id"])["records"][0]["version"], "new")


class AdministratorPolicyTests(ShellFixture, unittest.TestCase):
    def test_reader_or_unconfirmed_user_cannot_activate_mode(self):
        self.api.is_cluster_administrator.return_value = False
        self.open_panel("deployment")
        mode = self.window.shell_actions.mode
        self.assertFalse(mode.isEnabled())
        self.assertIn("ограничения прав", mode.toolTip())
        with patch("quaber.ui.confirm") as confirm:
            mode.click()
        confirm.assert_not_called()
        self.assertTrue(self.window.preferences["read_only"])
        self.api.is_cluster_administrator.side_effect = ConnectionError("offline")
        self.window.admin_access.check("c", force=True)
        self.wait()
        self.assertFalse(mode.isEnabled())
        self.assertIn("Не удалось подтвердить", mode.toolTip())
        self.api.is_cluster_administrator.side_effect = None
        self.api.is_cluster_administrator.return_value = True
        self.window.admin_access.check("c", force=True)
        self.wait()
        self.assertTrue(mode.isEnabled())
        self.assertNotIn("недоступен", mode.toolTip())

    def test_no_edits_means_disabled_mode_and_configmap_yaml_is_editable_context(self):
        self.assertFalse(self.window.shell_actions.mode.isEnabled())
        panel = self.open_panel("configmap")
        self.assertFalse(self.window.shell_actions.mode.isEnabled())
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        self.wait()
        self.assertTrue(self.window.shell_actions.mode.isEnabled())
        with patch("quaber.ui.confirm", return_value=True):
            self.window.shell_actions.mode.click()
            self.wait()
        self.assertFalse(self.window.preferences["read_only"])
        panel.tabs.setCurrentIndex(0)
        self.assertTrue(self.window.preferences["read_only"])
        self.assertFalse(self.window.shell_actions.mode.isEnabled())

    def test_rights_are_rechecked_after_confirmation_and_before_mutation(self):
        panel = self.open_panel("deployment")
        self.assertTrue(self.window.shell_actions.mode.isEnabled())
        self.api.is_cluster_administrator.return_value = False
        with patch("quaber.ui.confirm", return_value=True):
            self.window.shell_actions.mode.click()
            self.wait()
        self.assertTrue(self.window.preferences["read_only"])
        self.window.set_read_only(False)  # worker policy is independent of the UI state
        with patch("quaber.ui.error"):
            panel._apply_scale(copy.deepcopy(panel.resource), 2)
            self.wait()
        self.api.scale.assert_not_called()
        self.assertTrue(self.window.preferences["read_only"])

    def test_delayed_activation_cannot_unlock_a_different_card(self):
        panel = self.open_panel("deployment")
        deferred = Deferred()
        target = self.window.admin_access.target()
        with patch.object(self.window, "background", deferred):
            self.window.admin_access.activate(target)
            self.window._resource_request += 1
            deferred.finish((True, ""))
        self.assertTrue(self.window.preferences["read_only"])

    def test_cursor_always_snaps_to_nearest_available_point(self):
        chart = MetricChart("CPU", "m")
        self.addCleanup(chart.close)
        chart.resize(600, 170)
        chart.set_data([10, None, 20])
        chart.show()
        def move(x):
            point = QPoint(x, 80)
            event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(chart.mapToGlobal(point)),
                                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(chart, event)
        move(65)
        self.assertEqual(chart.hover_index, 0)
        move(chart.width()-17)
        self.assertEqual(chart.hover_index, 2)
        move(round(chart.sample_x(1)))
        self.assertIn(chart.hover_index, (0, 2))

    def test_local_mode_revocation_during_rights_check_prevents_write(self):
        panel = self.open_panel("deployment")
        panel.background = deferred = Deferred()
        self.window.set_read_only(False)
        self.api.is_cluster_administrator.side_effect = lambda cid: (self.window.set_read_only(True), True)[1]
        panel._apply_scale(copy.deepcopy(panel.resource), 2)
        with patch("quaber.ui.error"):
            deferred.finish(execute=True)
        self.api.scale.assert_not_called()
        self.assertTrue(self.window.preferences["read_only"])
