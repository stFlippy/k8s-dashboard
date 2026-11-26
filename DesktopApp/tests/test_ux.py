"""Offline acceptance tests for navigation, Pod actions and log reading."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt5.QtCore import Qt, QPoint, QTimer
from PyQt5.QtGui import QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget, QMessageBox

from quaber.pod_logs import PodLogs
from quaber.storage import UserStore
from quaber.ui import MainWindow
from quaber.preferences import PreferenceWriter


from helpers_background import DeferredBackend as Deferred


from helpers_log_ui import LogFixture

class LogUXTests(LogFixture, unittest.TestCase):


    def test_old_success_and_error_cannot_leak_across_sources(self):
        self.view.container.setCurrentText("sidecar")
        self.bg.finish("old app logs")
        self.assertEqual(self.view.output.toPlainText(), "")
        self.app.processEvents()
        self.bg.finish("sidecar logs")
        self.assertEqual(self.view.output.toPlainText(), "sidecar logs")
        self.view.load()
        self.view.previous.set_availability('AVAILABLE')
        self.view.previous.setChecked(True)
        self.assertEqual(self.view.output.toPlainText(), 'sidecar logs')
        self.assertEqual(self.view._display_source, ('sidecar', False, 1000))
        self.assertEqual(self.view.source, ('sidecar', True, 1000))
        self.bg.finish(failure="stale current error")
        self.assertTrue(self.view.error_label.isHidden())
        self.app.processEvents()
        self.bg.finish(failure="previous unavailable")
        self.assertFalse(self.view.error_label.isHidden())
        self.assertEqual(self.view.output.toPlainText(), 'sidecar logs')
        self.assertEqual(self.view._display_source, ('sidecar', False, 1000))
        self.assertIn('Новый источник не загружен', self.view.status.text())
        self.view.load()
        self.bg.finish("previous recovered")
        self.assertTrue(self.view.error_label.isHidden())
        self.assertEqual(self.view.output.toPlainText(), "previous recovered")
        self.assertEqual(self.view._display_source, ('sidecar', True, 1000))

    def test_slow_response_is_displayed_even_if_next_poll_is_due(self):
        self.view.load()  # a second poll arrives before the first finishes
        self.bg.finish("first response")
        self.assertEqual(self.view.output.toPlainText(), "first response")
        self.app.processEvents()
        self.assertEqual(len(self.bg.jobs), 1)
        self.bg.finish("second response")
        self.assertEqual(self.view.output.toPlainText(), "second response")

    def test_search_exact_utf16_positions_next_previous_and_polling(self):
        self.bg.finish("😀 пример\nERROR one\nINFO another\nERROR two")
        QTest.keyClicks(self.view.search, "ERROR")
        QTest.qWait(220)
        self.assertEqual(self.view.output.textCursor().selectedText(), "ERROR")
        self.assertEqual(self.view.count.text(), "1/2")
        self.view.find_next()
        self.assertEqual(self.view.count.text(), "2/2")
        self.view.find_next(True)
        self.assertEqual(self.view.count.text(), "1/2")
        from quaber.polling import poll_interval
        self.assertEqual(poll_interval(self.view.timer), 5000)
        self.assertTrue(5000 <= self.view.timer.interval() <= 6000)
        self.assertFalse(hasattr(self.view, "polling"))
        self.assertFalse(self.view.follow.isChecked())
        self.view.update_search(select=False)
        self.view.find_next(True)
        self.assertEqual(self.view.count.text(), "2/2")

    def test_reading_keeps_selection_and_receives_pending_snapshot(self):
        self.bg.finish("one\ntwo\nthree")
        cursor = self.view.output.textCursor()
        cursor.setPosition(4)
        cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
        self.view.output.setTextCursor(cursor)
        self.view.pause_follow()
        self.view.load()
        self.bg.finish("one\ntwo\nthree")
        self.assertEqual(self.view.output.textCursor().selectedText(), "two")
        self.view.load()
        self.bg.finish("two\nthree\nfour")
        self.assertEqual(self.view.output.textCursor().selectedText(), "two")
        self.assertEqual(self.view.pending_text, "two\nthree\nfour")
        from quaber.polling import poll_interval
        self.assertEqual(poll_interval(self.view.timer), 5000)
        self.assertTrue(5000 <= self.view.timer.interval() <= 6000)
        self.assertFalse(hasattr(self.view, "polling"))
        self.view.resume_follow()
        self.assertEqual(self.view.output.toPlainText(), "two\nthree\nfour")
        self.assertIsNone(self.view.pending_text)

    def test_export_downloads_full_server_log_and_names_displayed_source(self):
        self.bg.finish("visible snapshot")
        self.view.pause_follow()
        self.view.load()
        self.bg.finish("newer snapshot")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.log"
            def download(cid, ns, pod, container, previous, destination, **kwargs):
                Path(destination).write_text('complete server log', encoding='utf-8')
                return len(b'complete server log')
            self.owner.api.download_logs.side_effect = download
            with patch("quaber.pod_logs.QFileDialog.getSaveFileName", return_value=(str(path), "")) as dialog:
                self.view.export()
            self.bg.finish(execute=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "complete server log")
            self.assertEqual(self.view.output.toPlainText(), 'visible snapshot')
            self.assertIn("pod-app-current-", dialog.call_args.args[2])

    def test_fixed_tail_and_follow_can_resume_without_disabling_polling(self):
        self.owner.api.logs.return_value = "first\n" + "long line "*2000 + "\nlast"
        self.bg.finish(execute=True)
        self.owner.api.logs.assert_called_once_with("c", "ns", "pod", "app", False, 1000)
        for removed in ("tail", "wrap", "polling", "to_end"):
            self.assertFalse(hasattr(self.view, removed))
        self.view.pause_follow()
        self.view.load()
        self.bg.finish("new\nlast")
        self.assertIsNotNone(self.view.pending_text)
        self.view.follow.setChecked(True)
        self.assertEqual(self.view.output.toPlainText(), "new\nlast")
        self.assertEqual(self.view.follow.text(), "autoscrolling")
        scroll = self.view.output.verticalScrollBar()
        self.assertEqual(scroll.value(), scroll.maximum())
        self.assertEqual(self.view.output.lineWrapMode(), self.view.output.LineWrapMode.NoWrap)

    def test_large_full_log_offers_stream_download_and_reverts_to_tail(self):
        from quaber.kubernetes import LogTooLarge
        self.bg.finish("initial tail")
        self.owner.api.logs.side_effect = LogTooLarge("large fixture")
        self.view.expanded.setChecked(True)
        with tempfile.TemporaryDirectory() as folder:
            destination = str(Path(folder)/"full.log")
            with patch("quaber.pod_logs.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),\
                 patch("quaber.pod_logs.QFileDialog.getSaveFileName", return_value=(destination, "")):
                self.bg.finish(execute=True)
            self.assertFalse(self.view.expanded.isChecked())
            self.assertEqual(self.view.source, ("app", False, 1000))
            self.owner.api.download_logs.return_value = 9000000
            self.bg.finish(execute=True)  # the file transfer, not a full-text display
            self.owner.api.download_logs.assert_called_once_with("c", "ns", "pod", "app", False, destination,
                                                                 cancel=self.view._download_cancel)
            self.assertFalse(self.view._download_cancel.is_set())
            self.assertIn("Полный лог сохранён", self.view.download_status.text())
            self.owner.api.logs.side_effect = None
            self.bg.finish("tail restored")
            self.assertEqual(self.view.output.toPlainText(), "tail restored")


class ShellFixture:
    def assert_bound_call(self, method, arguments, action, kind):
        from quaber.write_guard import WritePermit, parameter_digest
        self.assertEqual(method.call_count,1)
        call = method.call_args
        self.assertEqual(call.args,arguments)
        self.assertEqual(set(call.kwargs),{'permit','expected_version'} if action=='delete' else {'permit'})
        permit = call.kwargs['permit']
        self.assertIsInstance(permit,WritePermit)
        cid, namespace, name = arguments[:3]
        if action == 'scale':
            uid = arguments[4]
            parameters = {'replicas':arguments[3],'resourceVersion':arguments[5]}
        elif action == 'yaml':
            uid = arguments[3]['metadata']['uid']
            parameters = arguments[3]
        else:
            uid = arguments[3]
            if action == 'delete':
                self.assertEqual(call.kwargs['expected_version'],'1')
                parameters = {'mode':arguments[4],'resourceVersion':'1'}
            else:
                parameters = None
        self.assertEqual(permit.intent.identity,(cid,kind,namespace,name,uid))
        self.assertEqual(permit.intent.action,action)
        self.assertEqual(permit.intent.parameters,parameter_digest(parameters))
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = UserStore(Path(self.temp.name) / "UserData")
        with patch("quaber.ui.locate_kubectl", return_value=Path("unused.exe")),\
             patch.object(MainWindow, "refresh"), patch.object(MainWindow, "_prompt_expired_login"):
            self.window = MainWindow(store, Path(self.temp.name))
            self.addCleanup(self.cleanup_window)
            self.window.timer.stop()
            self.window.auth_timer.stop()
            self.app.processEvents()
        self.window.refresh = Mock()
        self.window._prompt_expired_login = Mock()
        from quaber.kubernetes import Kubernetes
        # Constrain method names without masquerading as a real Kubernetes
        # instance (which selects its physical collector implementation).
        self.api = Mock(spec=[name for name in dir(Kubernetes) if not name.startswith('_')] + ['cache'])
        self.api.cache = Mock()
        def cluster(cid):
            records = store.clusters()
            return copy.deepcopy(next((c for c in records if c['id'] == cid), {
                'id': cid, 'name': 'Production', 'enabled': True, 'server': 'https://synthetic.invalid',
                'username': 'synthetic', 'authMode': 'kubeconfig', 'encryptedKubeconfig': 'synthetic'}))
        self.api.cluster.side_effect = cluster
        self.api.is_cluster_administrator.return_value = True
        self.api.request.return_value = {'status': {'allowed': True}}
        self.deployment = {"metadata": {"name": "app", "uid": "d", "resourceVersion": "1"},
            "spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
        self.pod = {"metadata": {"name": "app-pod", "uid": "11111111-1111-1111-1111-111111111111", "namespace": "ns", 'resourceVersion':'1',
            "ownerReferences": [{"kind": "ReplicaSet", "name": "app-rs", "uid": "rs", "controller": True}]},
            "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]}, "status": {"phase": "Running",
                'containerStatuses':[dict(name=name,containerID='synthetic://'+name+'-1',restartCount=0,
                                          state={'running':{'startedAt':'2026-11-20T00:00:00Z'}}) for name in ('app','sidecar')]}}
        self.api.resource.side_effect = lambda c, kind, ns, name: self.deployment if kind == "deployment" else self.pod
        self.api.pods.return_value = [self.pod]
        self.api.deployments.return_value = [self.deployment]
        self.api.replica_sets.return_value = [{"metadata": {"uid": "rs", "ownerReferences": [{"controller": True, "uid": "d"}]}}]
        self.api.events.return_value = []
        self.api.logs.return_value = "logs"
        self.api.metrics.return_value = {"available": False}
        self.api.metrics_history.return_value = {'source': 'synthetic', 'series': {}}
        self.api.metrics_many.return_value = {}
        self.api.execute_pod.return_value = ('ok', '')
        self.api.scaling_policy.return_value = {'known': True, 'controllers': []}
        self.api.proxy = Mock()
        self.api.store = Mock()
        self.api.authorization = Mock()
        self.api.authorization.signature.side_effect = lambda c: (c.get('encryptedKubeconfig'), c.get('encryptedPassword'), '')
        self.window.api = self.api
        self.window.resize(1100, 700)
        self.window.show()
        self.identity = ("c", "pod", "ns", "app-pod", self.pod["metadata"]["uid"])

    def cleanup_window(self):
        self.window._pod_operations.clear()
        for panel in getattr(self.window, "_resource_stack", []):
            panel.yaml_editor.editor.document().setModified(False)
            panel.pending_operation = False
        self.window.close()
        self.window._cleanup_thread.join(5)
        from quaber.shutdown import release_window
        release_window(self.window)
        self.app.processEvents()

    def wait(self):
        self.app.processEvents()  # Start reads scheduled by showEvent before checking for idle.
        deadline = time.monotonic() + 3
        while self.window.background.running and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.window.background.running)
        QTest.qWait(10)

    def open_panel(self, kind):
        self.window.open_resource("c", kind, "ns", "app" if kind == "deployment" else "app-pod")
        self.wait()
        panel = self.window._resource_panel
        for timer in panel.findChildren(QTimer):
            timer.stop()
        return panel

    def ready_yaml(self, panel):
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        deadline = time.monotonic() + 3
        while not panel._yaml_ready and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(panel._yaml_ready)

class ShellUXTests(ShellFixture, unittest.TestCase):
    def test_mouse_back_dirty_cancel_and_alt_left_same_navigation(self):
        parent = self.open_panel("deployment")
        child = self.open_panel("pod")
        child.yaml_editor.editor.document().setModified(True)
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            QTest.mouseClick(child, Qt.MouseButton.BackButton, pos=QPoint(10, 10))
        self.assertIs(self.window._resource_panel, child)
        child.yaml_editor.editor.document().setModified(False)
        QTest.mouseClick(child, Qt.MouseButton.BackButton, pos=QPoint(10, 10))
        self.assertIs(self.window._resource_panel, parent)
        self.window.activateWindow()
        parent.setFocus()
        QTest.qWait(10)
        QTest.keyClick(parent, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        self.assertIsNone(self.window._resource_panel)
        self.window.tabs.setCurrentIndex(1)
        self.window.tabs.setCurrentIndex(2)
        self.window.go_back()
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.window.go_back()
        self.assertEqual(self.window.tabs.currentIndex(), 0)

    def test_admin_button_requires_math_and_settings_do_not_duplicate_it(self):
        from PyQt5.QtWidgets import QToolBar, QCheckBox, QDialog
        mode = self.window.shell_actions.mode
        self.open_panel("deployment")
        self.assertFalse(mode.isChecked())
        with patch("quaber.ui.confirm", return_value=False):
            mode.click()
        self.assertFalse(mode.isChecked())
        self.assertTrue(self.window.preferences["read_only"])
        with patch("quaber.ui.confirm", return_value=True) as confirm:
            mode.click()
            self.wait()
            self.assertFalse(self.window.preferences["read_only"])
            self.assertTrue(self.window.sidebar.property("writeEnabled"))
            mode.click()
            self.assertEqual(confirm.call_count, 1)
        self.assertFalse(mode.isChecked())
        self.assertFalse(self.window.sidebar.property("writeEnabled"))
        self.assertEqual(self.window.findChildren(QToolBar), [])
        observations = []
        def inspect():
            dialog = QApplication.activeModalWidget()
            observations.extend(c.text() for c in dialog.findChildren(QCheckBox))
            dialog.accept()
        QTimer.singleShot(10, inspect)
        self.window.clusters_dialog()
        self.assertFalse(any("Только" in label for label in observations))

    def test_pod_menu_has_requested_actions_without_redundant_buttons(self):
        from PyQt5.QtWidgets import QPushButton
        panel = self.open_panel("deployment")
        menu = self.window.resource_actions.menu(self.identity)
        labels = [a.text() for a in menu.actions()]
        for label in ("Открыть POD", "Terminal", "Логи", "События", "Открыть YAML", "Скопировать расположение", "Удалить Pod…"):
            self.assertIn(label, labels)
        for label in ("Открыть обзор", "Команды в контейнере", "Скопировать имя", "Разрешить изменения…"):
            self.assertNotIn(label, labels)
        self.assertFalse(any(b.text().startswith("Действия") for b in panel.findChildren(QPushButton)))
        menu.deleteLater()

    def test_location_picker_buttons_capture_the_target(self):
        from PyQt5.QtWidgets import QPushButton, QComboBox
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {"c": {"name": "Production"}}
        self.window.matrix_data = {(0, column): ("c", namespace, "app", self.deployment, [])
                                   for column, namespace in ((1, "one"), (2, "two"))}
        observations = []
        def choose():
            dialog = QApplication.activeModalWidget()
            observations.append(dialog.findChildren(QComboBox))
            next(b for b in dialog.findChildren(QPushButton) if b.text() == "Production / two").click()
        QTimer.singleShot(10, choose)
        with patch.object(self.window, "open_resource") as opened:
            self.window._matrix_click(0, 0, None)
            opened.assert_called_once_with("c", "deployment", "two", "app", expected_uid="d")
        self.assertEqual(observations, [[]])

    def test_replica_fact_and_refresh_periods_are_explicit(self):
        from quaber.polling import poll_interval
        from PyQt5.QtWidgets import QLabel
        from quaber.ui import MetricsWidget
        self.deployment["spec"]["replicas"] = 5
        self.deployment["status"] = {"replicas": 6, "readyReplicas": 3, "availableReplicas": 3}
        panel = self.open_panel("deployment")
        self.assertEqual(panel.context_values["Pods"].text(), "6/5")
        self.assertFalse(any(label.text() == "Реплики факт./запланировано" for label in panel.findChildren(QLabel)))
        self.assertEqual(panel.deployment_period.text(), "Автообновление: 1 сек")
        self.assertEqual(poll_interval(panel.deployment_timer),1000)
        self.assertTrue(1000 <= panel.deployment_timer.interval() <= 1200)
        panel._go_back()
        pod = self.open_panel("pod")
        metrics = pod.findChild(MetricsWidget)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.refresh_period.text(), "Автообновление: 5 сек")

    def test_compact_relogin_can_save_then_remove_a_password(self):
        from quaber.ui import LoginGroupsDialog
        from test_storage import CONFIG
        record = self.window.store.put_cluster("Fixture", "https://example.test:6443", "alice", CONFIG)
        for remember in (True, False):
            dialog = LoginGroupsDialog(self.window, [{"username": "alice", "clusters": [record]}])
            self.addCleanup(dialog.deleteLater)
            self.assertLess(dialog.height(), 300)
            dialog.passwords[0].setText("fixture-relogin-secret")
            dialog.remember_passwords[0].setChecked(remember)
            with patch("quaber.ui.login", return_value=CONFIG), patch("quaber.ui.locate_kubectl", return_value=Path("unused.exe")):
                dialog.submit()
                self.wait()
            record = self.window.store.clusters()[0]
            self.assertEqual(bool(record.get("encryptedPassword")), remember)
            if remember:
                self.assertEqual(self.window.store.saved_password(record), "fixture-relogin-secret")

    def test_yaml_poll_preserves_scroll_and_draft_until_explicit_reload(self):
        self.deployment["metadata"]["annotations"] = {f"key-{i:04}": "fixture" for i in range(900)}
        panel = self.open_panel("deployment")
        self.ready_yaml(panel)
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        editor = panel.yaml_editor.editor
        cursor = editor.textCursor()
        cursor.setPosition(editor.document().findBlockByNumber(400).position())
        editor.setTextCursor(cursor)
        QTest.qWait(20)
        scroll = editor.verticalScrollBar()
        position = scroll.value()
        before = editor.toPlainText()
        changed = copy.deepcopy(panel.resource)
        changed["metadata"]["resourceVersion"] = "2"
        changed["spec"]["replicas"] = 3
        panel._adopt_live(changed)
        self.app.processEvents()
        self.assertEqual(editor.toPlainText(), before)
        self.assertEqual(scroll.value(), position)
        self.assertFalse(panel.yaml_notice.isHidden())
        self.assertEqual(panel.version, "1")
        self.window.set_read_only(False)
        editor.insertPlainText("# draft\n")
        draft = editor.toPlainText()
        with patch("quaber.ui.confirm", return_value=False):
            panel.reload_yaml()
        self.assertEqual(editor.toPlainText(), draft)
        with patch("quaber.ui.confirm", return_value=True):
            self.deployment = changed
            panel.reload_yaml()
        self.wait()
        self.assertEqual(panel.version, "2")
        self.assertIn("replicas: 3", editor.toPlainText())
        self.assertTrue(panel.yaml_notice.isHidden())

    def test_operations_capture_live_version_without_rebasing_yaml(self):
        panel = self.open_panel("deployment")
        changed = copy.deepcopy(panel.resource)
        changed["metadata"]["resourceVersion"] = "2"
        panel._adopt_live(changed)
        self.assertEqual(panel.version, "1")
        self.assertEqual(panel.yaml_base_resource["metadata"]["resourceVersion"], "1")
        self.window.set_read_only(False)
        self.api.restart.return_value = changed
        with patch("quaber.ui.confirm", return_value=True), patch.object(panel, "_updated"):
            panel.restart()
            self.wait()
        self.assert_bound_call(self.api.restart,("c", "ns", "app", "d", "2"),'restart','deployment')

    def test_write_mode_locks_after_idle_or_view_change(self):
        self.window.set_read_only(False)
        timer = self.window.shell_actions.idle_timer
        self.assertEqual(timer.interval(), 60000)
        timer.setInterval(35)
        timer.start()
        QTest.qWait(120)
        self.assertTrue(self.window.preferences["read_only"])
        timer.setInterval(60000)
        self.window.set_read_only(False)
        view = self.window.views.add("Other view")
        index = self.window.view_bar.addTab(view["name"])
        self.window.view_bar.setCurrentIndex(index)
        self.assertTrue(self.window.preferences["read_only"])

    def test_only_readonly_unlock_uses_mathematics(self):
        from quaber.ui import confirm
        with patch("quaber.ui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),\
             patch("quaber.ui.QInputDialog.getInt") as maths:
            self.assertTrue(confirm(self.window, "Операция", "Продолжить?"))
            maths.assert_not_called()
        with patch("quaber.ui.random.randint", side_effect=[12, 33]),\
             patch("quaber.ui.QInputDialog.getInt", return_value=(45, True)) as maths:
            self.assertTrue(confirm(self.window, "Разблокировать", "Продолжить?", arithmetic=True))
            maths.assert_called_once()

    def test_log_refresh_button_removed_and_brand_assets_are_loaded(self):
        from PyQt5.QtWidgets import QLabel
        panel = self.open_panel("pod")
        self.assertFalse(hasattr(panel.log_view, "refresh_button"))
        brand = self.window.sidebar.findChild(QLabel, "brand")
        self.assertFalse(brand.pixmap().isNull())
        self.assertFalse(self.window.windowIcon().pixmap(32, 32).isNull())

    def test_matrix_groups_workloads_and_cluster_labels_are_optional(self):
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {"c": {"name": "Production", "namespaces": {"ns": {
            "deployments": [self.deployment], "pods": [self.pod], "replica_sets": [{"metadata": {
                "name": "app-rs", "uid": "rs", "ownerReferences": [{"controller": True, "kind": "Deployment", "name": "app", "uid": "d"}]}}],
            "cron_jobs": [{"kind": "CronJob", "metadata": {"name": "nightly", "uid": "cron"}, "spec": {}}]}}}}
        self.window.preferences["show_cluster_names"] = False
        self.window.render_matrix()
        self.assertEqual(self.window.matrix.horizontalHeaderItem(1).text(), "ns")
        self.assertEqual(self.window._row_names, ["app", "CronJob/nightly"])
        self.assertIn(1, self.window.matrix.group_rows)
        self.assertEqual(self.window.matrix.item(1, 1).text(), "Ожидание запуска")
        self.window.preferences["show_cluster_names"] = True
        self.window.render_matrix()
        self.assertEqual(self.window.matrix.horizontalHeaderItem(1).text(), "Production\nns")
        with patch.object(self.window, "open_resource") as opened:
            self.window._matrix_click(1, 0, None)
            opened.assert_called_once_with("c", "cronjob", "ns", "nightly", expected_uid="cron")

    def test_yaml_find_is_an_overlay_and_save_is_next_to_editor(self):
        panel = self.open_panel("deployment")
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        self.app.processEvents()
        editor = panel.yaml_editor.editor
        self.assertTrue(panel.yaml_editor.search_panel.isHidden())
        original_geometry = editor.geometry()
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertTrue(panel.yaml_editor.search_panel.isVisible())
        self.assertEqual(editor.geometry(), original_geometry)
        QTest.keyClick(panel.yaml_editor.search, Qt.Key.Key_Escape)
        self.assertTrue(panel.yaml_editor.search_panel.isHidden())
        self.assertTrue(panel.yaml_save.isVisible())
        self.assertFalse(panel.yaml_save.isEnabled())
        self.window.set_read_only(False)
        editor.insertPlainText("# draft\n")
        self.assertIn("Ctrl+S", panel.yaml_draft_state.text())
        with patch.object(panel, "save_yaml") as saved:
            self.window.shell_actions.save()
            saved.assert_called_once()
        editor.document().setModified(False)
        self.assertEqual(panel.yaml_draft_state.text(), "")

    def test_replica_buttons_use_same_guarded_operation_and_disabled_hint(self):
        panel = self.open_panel("deployment")
        self.assertFalse(panel.replica_up.isEnabled())
        with patch("quaber.tooltips.QToolTip.showText") as hint:
            QTest.mouseClick(panel.replica_up, Qt.MouseButton.LeftButton)
            self.assertTrue(hint.called)
        self.assertIn("режим администратора", hint.call_args.args[1])
        self.api.scale.assert_not_called()
        self.window.set_read_only(False)
        with patch("quaber.ui.confirm", return_value=True), patch.object(panel, "_updated"):
            QTest.mouseClick(panel.replica_up, Qt.MouseButton.LeftButton)
            self.api.scale.assert_not_called()
            panel._commit_scale_draft()
            self.wait()
        self.assert_bound_call(self.api.scale,("c", "ns", "app", 2, "d", "1"),'scale','deployment')
        panel.pod_donut.set_pods([self.pod, {"status": {"phase": "Pending"}}])
        self.assertEqual(panel.pod_donut.counts, {"Running": 1, "Pending": 1})
        from PyQt5.QtWidgets import QLabel
        self.assertEqual(sum(label.text() == "Deployment details" for label in panel.findChildren(QLabel)), 1)
        self.assertFalse(panel.details.toPlainText().startswith("app\n"))

    def test_fixed_sidebar_brand_contrast_and_russian_buttons(self):
        from PyQt5.QtWidgets import QLabel
        from PyQt5.QtGui import QFontInfo
        self.assertEqual(self.window.sidebar.minimumWidth(), 240)
        self.assertEqual(self.window.sidebar.maximumWidth(), 240)
        self.assertEqual(self.window.sidebar.findChild(QLabel, "brandName").text(), "Quaber")
        self.assertEqual(QFontInfo(self.window.sidebar.findChild(QLabel, "brandName").font()).family(), "Roboto")
        self.window._select_theme("light")
        image = self.window.brand_logo.pixmap().toImage()
        opaque = next(image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width())
                      if image.pixelColor(x, y).alpha() > 240)
        self.assertLess(opaque.lightness(), 100)
        self.window.set_read_only(False)
        image = self.window.brand_logo.pixmap().toImage()
        opaque = next(image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width())
                      if image.pixelColor(x, y).alpha() > 240)
        self.assertGreater(opaque.lightness(), 240)
        box = QMessageBox(self.window)
        self.addCleanup(box.deleteLater)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        self.assertEqual(box.button(QMessageBox.StandardButton.Yes).text(), "Да")
        self.assertEqual(box.button(QMessageBox.StandardButton.Cancel).text(), "Отмена")

    def test_matrix_group_captions_do_not_shift_clicks_or_grow_on_refresh(self):
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {"c": {"name": "Production", "namespaces": {"ns": {
            "deployments": [self.deployment], "pods": [self.pod], "replica_sets": [{"metadata": {
                "name": "app-rs", "uid": "rs", "ownerReferences": [{"controller": True, "kind": "Deployment", "name": "app", "uid": "d"}]}}],
            "cron_jobs": [{"kind": "CronJob", "metadata": {"name": "nightly", "uid": "cron"}, "spec": {}}]}}}}
        self.window.render_matrix()
        matrix = self.window.matrix
        self.assertTrue(matrix.verticalHeader().isHidden())
        self.assertEqual(matrix.horizontalHeaderItem(0).text(), "")
        self.assertEqual(matrix.row_kinds, ["Deployment", "CronJob"])
        self.assertEqual(matrix.item(1, 0).text(), "nightly")
        heights = [matrix.rowHeight(i) for i in range(2)]
        self.window.render_matrix()
        self.assertEqual([matrix.rowHeight(i) for i in range(2)], heights)
        rect = matrix.visualItemRect(matrix.item(0, 1))
        with patch.object(self.window, "open_resource") as opened:
            QTest.mouseClick(matrix.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.center().x(), rect.top()+12))
            opened.assert_not_called()
            QTest.mouseClick(matrix.viewport(), Qt.MouseButton.LeftButton,
                            pos=QPoint(rect.center().x(), rect.top()+matrix.GROUP_HEIGHT+12))
            opened.assert_called_once_with("c", "pod", "ns", "app-pod", expected_uid=self.pod["metadata"]["uid"])

    def test_settings_sections_keep_connection_controls_above_list(self):
        from PyQt5.QtWidgets import QPushButton, QWidget
        from quaber.resource_list import ResourceList
        def inspect(dialog):
            dialog.show()
            dialog.sections.setCurrentRow(2)
            self.app.processEvents()
            clusters = dialog.findChild(QWidget, "settingsClusters")
            general = dialog.findChild(QWidget, "settingsGeneral")
            table = clusters.findChild(ResourceList)
            button = next(b for b in clusters.findChildren(QPushButton) if b.text().startswith("Добавить"))
            self.assertLess(button.mapTo(dialog, QPoint()).y(), table.mapTo(dialog, QPoint()).y())
            self.assertTrue(clusters.isVisible())
            self.assertFalse(general.isVisible())
            dialog.sections.setCurrentRow(0)
            self.assertTrue(general.isVisible())
            self.assertFalse(clusters.isVisible())
            dialog.close()
            dialog.deleteLater()
            return 0
        with patch("quaber.ui.run_dialog", side_effect=inspect):
            self.window.clusters_dialog()

    def test_location_buttons_elide_without_losing_identity(self):
        from quaber.resource_actions import choose_location
        from quaber.widgets import ElidedButton
        from PyQt5.QtWidgets import QDialog
        identity = ("c", "deployment", "ns", "app", "d")
        label = "namespace-" + "very-long-"*70
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            button = dialog.findChild(ElidedButton)
            self.assertEqual(button.full_text, label)
            self.assertIn("…", button.text())
            self.assertLess(dialog.width(), 600)
            button.click()
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, "exec_", inspect):
            self.assertEqual(choose_location(self.window, [(label, identity)]), identity)

    def test_namespace_setting_also_controls_location_labels(self):
        scope = {"deployments": [self.deployment], "pods": [], "replica_sets": []}
        self.window.store.clusters = lambda: [dict(id=cid, name=data["name"], enabled=True, server="https://synthetic.invalid", username="fixture") for cid, data in self.window.snapshot.items()]
        self.window.snapshot = {"c1": {"name": "One", "namespaces": {"ns-one": scope}},
                                "c2": {"name": "Two", "namespaces": {"ns-two": scope}}}
        for show_names in (False, True):
            self.window.preferences["show_cluster_names"] = show_names
            self.window.render_matrix()
            with patch("quaber.resource_actions.choose_location", return_value=None) as choose:
                self.window._matrix_click(0, 0, None)
            labels = [entry[0] for entry in choose.call_args.args[1]]
            self.assertEqual(labels, ["One / ns-one", "Two / ns-two"] if show_names else ["ns-one", "ns-two"])

    def test_hovering_name_highlights_only_its_row_and_names_can_be_reordered(self):
        from quaber.widgets import PodMatrix
        from PyQt5.QtCore import QEvent, QPointF
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtWidgets import QTableWidgetItem
        matrix = PodMatrix()
        self.addCleanup(matrix.deleteLater)
        matrix.setRowCount(2)
        matrix.setColumnCount(2)
        matrix.row_kinds = ["Deployment", "Deployment"]
        matrix.setItem(0, 0, QTableWidgetItem("first"))
        matrix.setItem(1, 0, QTableWidgetItem("second"))
        matrix.resize(400, 240)
        matrix.show()
        QTest.qWait(30)
        matrix.sync_frozen()
        image = matrix.frozen.viewport().grab().toImage()
        other_y = matrix.rowViewportPosition(1)+matrix.rowHeight(1)//2
        color = image.pixelColor(4, other_y)
        matrix._set_hover((0, 0))
        self.app.processEvents()
        self.assertEqual(matrix.frozen.viewport().grab().toImage().pixelColor(4, other_y), color)
        origin = QPoint(25, matrix.rowViewportPosition(0)+matrix.group_offset(0)+10)
        target = QPoint(25, other_y)
        activated = []
        matrix.activatedAt.connect(lambda *args: activated.append(args))
        QTest.mousePress(matrix.frozen.viewport(), Qt.MouseButton.LeftButton, pos=origin)
        QApplication.sendEvent(matrix.frozen.viewport(), QMouseEvent(QEvent.Type.MouseMove, QPointF(target),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        QTest.mouseRelease(matrix.frozen.viewport(), Qt.MouseButton.LeftButton, pos=target)
        self.assertEqual(matrix.verticalHeader().logicalIndex(0), 1)
        self.assertEqual(activated, [])
        matrix.close()

    def test_resource_confirmation_has_contrasting_target_and_yes_no(self):
        from quaber.confirmations import ResourceConfirmation
        from quaber.themes import write_background
        from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QFrame, QLineEdit, QPlainTextEdit, QCheckBox
        for light in (False, True):
            self.window._select_theme("light" if light else "dark")
            target = "Deployment · Production / ns / app<&>"
            dialog = ResourceConfirmation(self.window, "Изменить реплики", target, "Реплики: 7 → 5", "--- before\n+++ after\n@@ -1 +1 @@\n-replicas: 7\n+replicas: 5")
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.target_label.text(), target)
            self.assertEqual(dialog.target_label.textFormat(), Qt.TextFormat.PlainText)
            self.assertIn(write_background(light), dialog.findChild(QFrame, "operationTarget").styleSheet())
            self.assertEqual(dialog.findChildren(QLineEdit), [])
            yes = dialog.buttons.button(QDialogButtonBox.StandardButton.Yes)
            no = dialog.buttons.button(QDialogButtonBox.StandardButton.No)
            self.assertEqual((yes.text(), no.text()), ("Да", "Нет"))
            self.assertTrue(no.isDefault())
            from quaber.diff_view import DiffView
            self.assertFalse(dialog.findChild(DiffView).isHidden())
            self.assertEqual(dialog.findChildren(QCheckBox), [])
            self.window.preferences["read_only"] = False
            self.window.shell_actions.idle_timer.start()
            QTimer.singleShot(0, yes.click)
            self.assertEqual(dialog.exec_(), QDialog.DialogCode.Accepted)
            QTimer.singleShot(0, no.click)
            self.assertEqual(dialog.exec_(), QDialog.DialogCode.Rejected)

    def test_scale_arrows_need_no_confirmation_and_keep_readonly_guard(self):
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        for value in (0, 50):
            self.window.set_read_only(False)
            self.api.scale.reset_mock()
            with patch("quaber.ui.confirm", return_value=True) as decision,\
                 patch("quaber.ui.QInputDialog.getText", side_effect=AssertionError("unexpected typing")),\
                 patch.object(panel, "_updated"):
                panel._apply_scale(copy.deepcopy(panel.resource), value)
                self.wait()
            decision.assert_not_called()
            self.assert_bound_call(self.api.scale,("c", "ns", "app", value, "d", "1"),'scale','deployment')
        self.api.scale.reset_mock()
        self.window.set_read_only(True)
        with patch("quaber.ui.confirm") as decision, patch("quaber.ui.error"):
            panel._apply_scale(copy.deepcopy(panel.resource), 0)
            self.wait()
        decision.assert_not_called()
        self.api.scale.assert_not_called()

    def test_yaml_has_one_target_confirmation_with_optional_diff(self):
        import yaml
        panel = self.open_panel("deployment")
        self.ready_yaml(panel)
        self.window.set_read_only(False)
        edited = copy.deepcopy(panel.resource)
        edited["spec"]["replicas"] = 3
        panel.yaml_editor.setPlainText(yaml.safe_dump(edited))
        with patch("quaber.ui.confirm", return_value=True) as decision,\
             patch("quaber.ui.QInputDialog.getText", side_effect=AssertionError("unexpected typing")),\
             patch("quaber.ui.run_dialog") as separate_preview, patch.object(panel, "_updated"):
            panel.save_yaml()
            self.wait()
        separate_preview.assert_not_called()
        decision.assert_called_once()
        self.assertIn("Production / ns / app", decision.call_args.kwargs["target"])
        self.assertIn("replicas: 3", decision.call_args.kwargs["details"])
        self.assertEqual(self.api.replace.call_count,1)
        call = self.api.replace.call_args
        self.assertEqual(call.args,("c", "deployment", "ns", "app", edited, "1"))
        from quaber.write_guard import WritePermit, parameter_digest
        self.assertIsInstance(call.kwargs['permit'],WritePermit)
        self.assertEqual(call.kwargs['permit'].intent.identity,('c','deployment','ns','app','d'))
        self.assertEqual(call.kwargs['permit'].intent.parameters,parameter_digest(edited))

    def test_both_pod_removal_modes_only_need_yes_or_no(self):
        from quaber.resource_actions import confirm_pod_removal
        for controlled in (False, True):
            pod = copy.deepcopy(self.pod)
            if not controlled:
                pod["metadata"].pop("ownerReferences")
            with patch("quaber.resource_actions.confirm_change", return_value=True) as decision:
                mode = confirm_pod_removal(self.window, "Production / ns / app-pod", pod)
            self.assertEqual(mode, "restart" if controlled else "delete")
            decision.assert_called_once()
            self.assertEqual(decision.call_args.args[2], "Pod · Production / ns / app-pod")
            with patch("quaber.resource_actions.confirm_change", return_value=False):
                self.assertIsNone(confirm_pod_removal(self.window, "Production / ns / app-pod", pod))

    def test_changed_panel_target_cannot_receive_a_confirmed_mutation(self):
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        def switch_target(*args, **kwargs):
            panel.name = "another-deployment"
            return True
        with patch("quaber.ui.confirm", side_effect=switch_target), patch("quaber.ui.error") as failure:
            panel.restart()
            self.wait()
        self.api.restart.assert_not_called()
        failure.assert_called_once()
        self.assertEqual(panel.operation_label(), "Deployment · Production / ns / app")
        panel.name = "app"

    def test_queued_mutation_rechecks_opened_uid_and_readonly(self):
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        original_uid = panel.resource["metadata"]["uid"]
        queue = Deferred()
        panel.background.background = queue
        for changed in ("uid", "readonly"):
            with patch("quaber.ui.confirm", return_value=True):
                panel.restart()
            self.assertTrue(panel.pending_operation)
            if changed == "uid":
                panel.resource["metadata"]["uid"] = "replacement"
            else:
                self.window.set_read_only(True)
            with patch("quaber.ui.error") as failure:
                queue.finish(execute=True)
            failure.assert_called_once()
            self.api.restart.assert_not_called()
            panel.resource["metadata"]["uid"] = original_uid
            self.window.set_read_only(False)

    def test_one_confirmed_change_relocks_after_success_failure_or_queue_rejection(self):
        panel = self.open_panel("deployment")
        for fails in (False, True):
            self.window.set_read_only(False)
            self.api.restart.reset_mock()
            self.api.restart.side_effect = RuntimeError("fixture failure") if fails else None
            self.api.restart.return_value = copy.deepcopy(panel.resource)
            with patch("quaber.ui.confirm", return_value=True), patch.object(panel, "_updated"), patch("quaber.ui.error"):
                panel.restart()
                self.wait()
            self.api.restart.assert_called_once()
            self.assertTrue(self.window.preferences["read_only"])
            self.assertFalse(self.window.shell_actions.mode.isChecked())
            self.assertTrue(panel.yaml_editor.editor.isReadOnly())
            self.assertIsNone(self.window._write_operation)
        self.window.set_read_only(False)
        with patch("quaber.ui.confirm", return_value=False):
            panel.restart()
        self.assertFalse(self.window.preferences["read_only"])
        reject = Mock()
        reject.submit.return_value = False
        panel.background = reject
        with patch("quaber.ui.error") as failure:
            panel._mutate(Mock(), Mock())
            reject.submit.call_args.args[2]("late failure callback")
        failure.assert_called_once()
        self.assertTrue(self.window.preferences["read_only"])
        self.assertFalse(panel.pending_operation)
        self.assertIsNone(self.window._write_operation)

    def test_yaml_and_configmap_are_copyable_but_not_editable_when_readonly(self):
        from quaber.ui import ResourceDialog
        for kind in ("deployment", "configmap"):
            resource = copy.deepcopy(self.deployment) if kind == "deployment" else {
                "kind": "ConfigMap", "metadata": {"name": "settings", "namespace": "ns", "uid": "cm", "resourceVersion": "1"},
                "data": {"key": "original"}}
            self.window.set_read_only(True)
            panel = ResourceDialog(self.window, self.api, self.window.background, "c", kind, "ns", resource["metadata"]["name"], resource)
            self.window._resource_stack = [panel]
            self.window._resource_panel = panel
            panel.show()
            panel.tabs.setCurrentWidget(panel.yaml_editor)
            self.ready_yaml(panel)
            self.app.processEvents()
            for timer in panel.findChildren(QTimer):
                timer.stop()
            editor = panel.yaml_editor.editor
            before = editor.toPlainText()
            self.assertTrue(editor.isReadOnly())
            editor.selectAll()
            QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(QApplication.clipboard().text(), before)
            QApplication.clipboard().setText("injected")
            QTest.keyClick(editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            QTest.keyClick(editor, Qt.Key.Key_Backspace)
            QTest.keyClicks(editor, "not-allowed")
            self.assertEqual(editor.toPlainText(), before)
            self.window.set_read_only(False)
            self.assertFalse(editor.isReadOnly())
            QTest.keyClicks(editor, "draft")
            draft = editor.toPlainText()
            self.assertNotEqual(draft, before)
            self.window.set_read_only(True)
            QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(editor.toPlainText(), before)
            self.assertFalse(editor.document().isModified())
            self.assertFalse(editor.document().isUndoAvailable())
            editor.selectAll()
            QTest.keyClick(editor, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(QApplication.clipboard().text(), before)
            editor.document().setModified(False)
            panel.close()
            panel.deleteLater()
            self.window._resource_stack = []
            self.window._resource_panel = None
            self.wait()

    def test_terminal_commands_keep_access_without_extending_one_minute(self):
        from contextlib import nullcontext
        panel = self.open_panel("pod")
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        self.assertEqual(panel.terminal_timer.interval(), 60000)
        self.assertTrue(panel.terminal_timer.isActive())
        remaining = panel.terminal_timer.remainingTime()
        self.api.store.operation.side_effect = lambda *args, **kwargs: nullcontext()
        self.api.execute_pod.return_value = ("output", "")
        with patch("quaber.ui.confirm", return_value=True) as decision:
            for command in ("date", "uptime"):
                panel.console_input.setText(command)
                panel.run_console()
                self.wait()
                self.assertFalse(self.window.preferences["read_only"])
        decision.assert_not_called()
        self.assertEqual(self.api.execute_pod.call_count, 2)
        self.assertLess(panel.terminal_timer.remainingTime(), remaining)
        panel.terminal_timer.setInterval(35)
        panel.terminal_timer.start()
        QTest.keyClicks(panel.console_input, "activity")
        QTest.qWait(100)
        self.assertTrue(self.window.preferences["read_only"])
        self.assertEqual(panel.console_unlocked_until, 0)
        self.assertFalse(panel.terminal_timer.isActive())

    def test_leaving_terminal_tab_or_resource_immediately_relocks(self):
        panel = self.open_panel("pod")
        self.ready_yaml(panel)
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        panel.tabs.setCurrentWidget(panel.yaml_editor)
        self.assertTrue(self.window.preferences["read_only"])
        self.assertTrue(panel.yaml_editor.editor.isReadOnly())
        self.assertFalse(panel.terminal_timer.isActive())
        self.window.set_read_only(False)
        self.assertFalse(panel.yaml_editor.editor.isReadOnly())
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.assertTrue(panel.terminal_timer.isActive())
        panel.hide()
        self.assertTrue(self.window.preferences["read_only"])
        self.assertFalse(panel.terminal_timer.isActive())
        panel.show()

    def test_expired_terminal_keeps_command_and_sends_nothing(self):
        panel = self.open_panel("pod")
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        panel.console_input.setText("date")
        panel._lock_terminal_access()
        with patch("quaber.ui.confirm") as decision, patch("quaber.ui.error"):
            panel.run_console()
        decision.assert_not_called()
        self.api.execute_pod.assert_not_called()
        self.assertEqual(panel.console_input.text(), "date")
        self.assertTrue(self.window.preferences["read_only"])
        self.assertEqual(panel.console_unlocked_until, 0)

    def test_one_permission_cannot_start_two_operations_or_unlock_while_busy(self):
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        queue = Deferred()
        panel.background.background = queue
        operation = Mock()
        panel._mutate(operation, Mock())
        self.assertIsNotNone(self.window._write_operation)
        self.assertIsNone(self.window.begin_write_operation())
        with patch.object(self.window.background, "submit") as second:
            self.window.resource_actions.delete_pod(self.identity)
        second.assert_not_called()
        self.window.set_read_only(True)
        self.window.set_read_only(False)
        self.assertTrue(self.window.preferences["read_only"])
        with patch("quaber.ui.error"):
            queue.finish(execute=True)
        operation.assert_not_called()
        self.assertIsNone(self.window._write_operation)
        self.window.set_read_only(False)
        self.assertFalse(self.window.preferences["read_only"])

    def test_optional_rbac_error_does_not_discard_current_core_scope(self):
        from quaber.kubernetes import AggregateListForbidden
        from test_storage import CONFIG
        record = self.window.store.put_cluster("Fixture", "https://example.test:6443", "alice", CONFIG)
        self.api.namespaces.return_value = ["ns"]
        def optional(cid, kind, namespace):
            if kind == "statefulset":
                raise PermissionError("fixture RBAC")
            return {'deployment':[self.deployment], 'pod':[self.pod],
                    'replicaset':self.api.replica_sets.return_value}.get(kind, [])
        self.api.list_resources.side_effect = optional
        self.api.all_namespace_resources.side_effect = AggregateListForbidden('fixture scoped-only access')
        self.window.refresh_matrix()
        self.wait()
        scope = self.window.snapshot[record["id"]]["namespaces"]["ns"]
        self.assertEqual(scope["pods"], [self.pod])
        self.assertFalse(scope["_stale"])
        self.assertIn("statefulset", scope["_type_errors"])
        self.assertIn("fixture RBAC", self.window.last_updated.toolTip())

    def test_lock_shortcut_prevents_exec_and_preserves_input(self):
        panel = self.open_panel("pod")
        self.ready_yaml(panel)
        self.window.set_read_only(False)
        self.assertFalse(panel.yaml_editor.editor.isReadOnly())
        panel.console_input.setText("date")
        self.window.activateWindow()
        panel.setFocus()
        QTest.qWait(10)
        QTest.keyClick(panel, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(self.window.preferences["read_only"])
        self.assertTrue(panel.yaml_editor.editor.isReadOnly())
        self.assertTrue(all(not button.isEnabled() for button in panel.write_controls))
        with patch("quaber.ui.error"), patch("quaber.ui.confirm") as confirmation:
            panel.run_console()
        confirmation.assert_not_called()
        self.assertEqual(panel.console_input.text(), "date")
        self.assertEqual(panel.console_unlocked_until, 0)
        self.api.execute_pod.assert_not_called()
        panel.container.setCurrentText("sidecar")
        self.assertEqual(panel.console_container.currentText(), "app")
        self.wait()

    def test_other_operation_does_not_discard_yaml_without_consent(self):
        panel = self.open_panel("deployment")
        self.window.set_read_only(False)
        panel.yaml_editor.editor.insertPlainText("# unsaved\n")
        draft = panel.yaml_editor.toPlainText()
        with patch("quaber.ui.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel),\
             patch("quaber.ui.confirm") as confirmation:
            panel.restart()
        confirmation.assert_not_called()
        self.api.restart.assert_not_called()
        self.assertEqual(panel.yaml_editor.toPlainText(), draft)

    def test_deployment_single_click_selects_and_context_owns_identity(self):
        panel = self.open_panel("deployment")
        panel.tabs.setCurrentIndex(next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == "Pods"))
        self.app.processEvents()
        table = panel.pod_table
        entry = table.topLevelItem(0)
        with patch.object(self.window, "open_resource") as opened,\
             patch.object(self.window.resource_actions, "show") as show:
            position = table.visualItemRect(entry).center()
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=position)
            opened.assert_not_called()
            self.assertEqual(table.currentRow(), 0)
            table.customContextMenuRequested.emit(position)
            self.assertEqual(show.call_args.args[0], self.identity)
            menu = self.window.resource_actions.menu(self.identity)
            entry.setData(1, Qt.ItemDataRole.UserRole, ("replacement", "new-uid"))
            next(a for a in menu.actions() if a.text() == "Логи").trigger()
            opened.assert_called_once_with("c", "pod", "ns", "app-pod", "Логи", expected_uid=self.identity[-1])
            menu.deleteLater()

    def test_readonly_and_replaced_pod_never_delete(self):
        bg = Deferred()
        with patch.object(self.window, "background", bg):
            menu = self.window.resource_actions.menu(self.identity)
            action = next(a for a in menu.actions() if a.text() == "Удалить Pod…")
            self.assertFalse(action.isEnabled())
            self.window.resource_actions.delete_pod(self.identity)
            self.assertFalse(bg.jobs)
            menu.deleteLater()
            self.window.set_read_only(False)
            self.window.resource_actions.delete_pod(self.identity)
            changed = copy.deepcopy(self.pod)
            changed["metadata"]["uid"] = "replacement"
            with patch("quaber.resource_actions.QMessageBox.warning"),\
                 patch("quaber.resource_actions.confirm_pod_removal") as confirm:
                bg.finish(changed)
            confirm.assert_not_called()
            self.api.delete_pod.assert_not_called()
            self.assertFalse(self.window._pod_operations)

    def test_lock_during_preflight_or_before_worker_prevents_delete(self):
        bg = Deferred()
        with patch.object(self.window, "background", bg):
            self.window.set_read_only(False)
            self.window.resource_actions.delete_pod(self.identity)
            self.window.set_read_only(True)
            with patch("quaber.resource_actions.confirm_pod_removal") as confirm:
                bg.finish(self.pod)
            confirm.assert_not_called()
            self.assertFalse(self.window._pod_operations)
            self.window.set_read_only(False)
            self.window.resource_actions.delete_pod(self.identity)
            with patch("quaber.resource_actions.confirm_pod_removal", return_value="restart"):
                bg.finish(self.pod)
            self.window.set_read_only(True)
            with patch("quaber.resource_actions.QMessageBox.warning"):
                bg.finish(execute=True)
            self.api.delete_pod.assert_not_called()
            self.assertFalse(self.window._pod_operations)

    def test_cancel_and_successful_menu_delete_and_no_retry_on_failure(self):
        bg = Deferred()
        self.window.set_read_only(False)
        with patch.object(self.window, "background", bg):
            self.window.resource_actions.delete_pod(self.identity)
            with patch("quaber.resource_actions.confirm_pod_removal", return_value=None):
                bg.finish(self.pod)
            self.assertFalse(bg.jobs)
            self.assertFalse(self.window._pod_operations)
            self.window.resource_actions.delete_pod(self.identity)
            with patch("quaber.resource_actions.confirm_pod_removal", return_value="restart"):
                bg.finish(self.pod)
            bg.finish(execute=True)
            self.assert_bound_call(self.api.delete_pod,("c", "ns", "app-pod", self.identity[-1], "restart"),'delete','pod')
            self.api.invalidate_resource.assert_called_once_with('c','pod','ns','app-pod')
            self.assertFalse(self.window._pod_operations)
            self.api.delete_pod.reset_mock()
            self.assertTrue(self.window.preferences["read_only"])
            self.api.delete_pod.side_effect = RuntimeError("PDB refuses eviction")
            self.window.set_read_only(False)
            self.window.resource_actions.delete_pod(self.identity)
            with patch("quaber.resource_actions.confirm_pod_removal", return_value="restart"):
                bg.finish(self.pod)
            with patch("quaber.resource_actions.QMessageBox.warning"):
                bg.finish(execute=True)
            self.assertTrue(self.window.preferences["read_only"])
            self.assertEqual(self.api.delete_pod.call_count, 1)
            self.assertFalse(bg.jobs)
            self.assertFalse(self.window._pod_operations)


class PreferenceUXTests(unittest.TestCase):
    def test_transient_windows_file_lock_does_not_lose_setting(self):
        saved = threading.Event()
        store = Mock()
        store.preferences.return_value = {}
        attempts = []
        def write(value):
            attempts.append(value)
            if len(attempts) == 1:
                raise PermissionError("file temporarily open for reading")
            saved.set()
        store.save_preferences.side_effect = write
        writer = PreferenceWriter(store)
        try:
            writer.save({"theme": "light"})
            self.assertTrue(saved.wait(2), "temporary lock lost the pending setting")
            self.assertEqual(attempts, [{"theme": "light"}, {"theme": "light"}])
        finally:
            writer.close()
            writer.thread.join(2)
            writer.deleteLater()


if __name__ == "__main__":
    unittest.main()
