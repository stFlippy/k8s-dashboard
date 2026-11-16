import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtCore import QLockFile, QTimer, Qt
from PyQt6.QtTest import QTest
from quaber.storage import UserStore
from quaber.kubernetes import Kubernetes
from quaber.authorization import ClusterAuthorization, AuthorizationRequired
from quaber.views import MatrixViews
from quaber.safety import resource_changes, validate_resources
from quaber.cache import SnapshotCache
from quaber.ui import MainWindow, Background, ClusterDialog, ResourceDialog, BoundedConsole
from quaber.widgets import pod_status
from quaber.preferences import PreferenceWriter
from test_storage import CONFIG


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = UserStore(self.root / "UserData")
        self.cluster = self.store.put_cluster("A", "https://example.test:6443", "alice", CONFIG,
                     password="fixture-private-password", remember_password=True)
        self.proxy = Mock()
        self.api = Kubernetes(self.store, self.proxy)

    def wait(self, predicate):
        deadline = time.monotonic()+5
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def window(self):
        # Avoid automatic login against the fixture endpoint.
        with patch("quaber.ui.locate_kubectl", return_value=Path("fixture.exe")):
            window = MainWindow(self.store, self.root)
        window.timer.stop()
        window.auth_timer.stop()
        window.api = self.api
        window.proxy = self.proxy
        window._prompt_expired_login = lambda: None
        def cleanup():
            window.close()
            worker = getattr(window, "_cleanup_thread", None)
            if worker is not None:
                worker.join(5)
            window.deleteLater()
        self.addCleanup(cleanup)
        return window

    def test_dpapi_key_migrates_and_reopens_without_changing_ciphertext(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI")
        from quaber.windows_secrets import PREFIX, transform
        self.assertTrue(self.store.key_file.read_bytes().startswith(PREFIX))
        old = self.cluster["encryptedKubeconfig"]
        self.assertEqual(UserStore(self.store.root).decrypt(old), self.store.decrypt(old))
        # Legacy portable key migrates atomically without re-encrypting records.
        self.store.key_file.write_bytes(self.store.key)
        migrated = UserStore(self.store.root)
        self.assertEqual(migrated.decrypt(old), self.store.decrypt(old))
        self.assertEqual(transform(migrated.key_file.read_bytes()[len(PREFIX):], True), self.store.key)

    def test_renewal_cannot_overwrite_bob_with_alice_token(self):
        def login(*args):
            self.store.put_cluster("A", self.cluster["server"], "bob", CONFIG.replace("a.b.c", "bob-token"), cluster_id=self.cluster["id"])
            return CONFIG
        auth = ClusterAuthorization(self.store, self.proxy)
        with patch("quaber.authorization.locate_kubectl", return_value=Path("fixture.exe")), patch("quaber.authorization.login", side_effect=login):
            with self.assertRaises(AuthorizationRequired):
                auth.renew(self.cluster["id"], self.cluster["encryptedKubeconfig"])
        current = self.store.clusters()[0]
        self.assertEqual(current["username"], "bob")
        self.assertIn("bob-token", self.store.decrypt(current["encryptedKubeconfig"]))
        self.proxy.close.assert_not_called()

    def test_manual_commit_compares_exact_snapshot(self):
        updated = self.store.put_cluster("A", self.cluster["server"], "bob", CONFIG, cluster_id=self.cluster["id"])
        with self.assertRaises(ValueError):
            self.store.put_cluster("A", self.cluster["server"], "alice", CONFIG, cluster_id=self.cluster["id"], expected=self.cluster)
        self.assertEqual(self.store.clusters()[0], updated)

    def test_scale_and_restart_reject_recreated_deployment(self):
        self.api.resource = Mock(return_value={"metadata": {"uid": "replacement", "resourceVersion": "2"}})
        self.api.request = Mock()
        for action, args in ((self.api.scale, (2, "original", "1")), (self.api.restart, ("original", "1"))):
            with self.assertRaises(RuntimeError):
                action(self.cluster["id"], "ns", "deployment", *args)
        self.api.request.assert_not_called()

    def test_eviction_honors_pdb_and_never_falls_back_to_delete(self):
        uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.api.resource = Mock(return_value={"metadata": {"uid": uid, "ownerReferences": [{"controller": True, "kind": "Job"}]}})
        self.api.request = Mock(side_effect=RuntimeError("PDB rejected"))
        with self.assertRaises(RuntimeError):
            self.api.delete_pod(self.cluster["id"], "ns", "pod", uid, "restart")
        call = self.api.request.call_args
        self.assertTrue(call.args[1].endswith("/eviction"))
        self.assertEqual(call.args[2], "POST")
        self.assertEqual(call.args[3]["deleteOptions"]["preconditions"]["uid"], uid)
        self.assertEqual(self.api.request.call_count, 1)
        self.assertEqual([r["phase"] for r in self.store.recent_audit()], ["intent", "unknown"])

    def test_scale_uses_scale_subresource_dryrun_and_correlated_audit(self):
        self.api.resource = Mock(return_value={"metadata": {"uid": "uid", "resourceVersion": "1"}, "spec": {"replicas": 3}})
        self.api.request = Mock(return_value={})
        self.api.scale(self.cluster["id"], "ns", "deployment", 0, "uid", "1")
        self.assertTrue(self.api.request.call_args_list[0].args[1].endswith("/scale?dryRun=All"))
        self.assertTrue(self.api.request.call_args_list[1].args[1].endswith("/scale"))
        records = self.store.recent_audit()
        self.assertEqual(records[0]["operationId"], records[1]["operationId"])
        self.assertEqual(records[0]["oldReplicas"], 3)
        self.assertEqual(records[0]["newReplicas"], 0)
        self.assertEqual(records[1]["phase"], "success")

    def test_typed_resources_and_explicit_limit_removal(self):
        with self.assertRaises(ValueError):
            validate_resources({"limits": {"memory": "512m"}})
        with self.assertRaises(ValueError):
            validate_resources({"requests": {"cpu": "2"}, "limits": {"cpu": "100m"}})
        original = {"limits": {"memory": "512Mi"}}
        with self.assertRaises(ValueError):
            resource_changes(original, {("limits", "memory"): ""})
        self.assertEqual(resource_changes(original, {("limits", "memory"): ""}, True), {})
        self.assertEqual(original["limits"]["memory"], "512Mi")

    def test_invalid_import_is_atomic_and_bad_persisted_view_recovers(self):
        preferences = {}
        views = MatrixViews(preferences)
        before = copy.deepcopy(views.views)
        with self.assertRaises(ValueError):
            views.import_data({"format": "quaber-matrix-views", "version": 1, "views": [{"name": "bad", "filters": None}]}, [])
        self.assertEqual(views.views, before)
        self.store.save_preferences({"views": [{"name": "bad", "filters": None}], "header_height": "invalid"})
        recovered = self.store.preferences()
        self.assertEqual(recovered["views"][0]["name"], "Основная")
        self.assertNotIn("header_height", recovered)

    def test_console_literal_html_and_single_line_limit(self):
        console = BoundedConsole()
        self.addCleanup(console.deleteLater)
        console.append("<b>fixture output</b>")
        self.assertIn("<b>", console.toPlainText())
        for _ in range(5):
            console.append("x"*300000)
        self.assertLessEqual(console.document().characterCount(), 500000)

    def test_redaction_registry_expires_and_has_capacity(self):
        from quaber import app_logging as logs
        with patch.object(logs.time, "monotonic", return_value=100):
            for index in range(200):
                logs.register_secret(f"fixture-secret-{index}")
            self.assertLessEqual(len(logs._secrets), logs.MAX_SECRETS)
            self.assertEqual(logs.redact("fixture-secret-199"), "[REDACTED]")
        with patch.object(logs.time, "monotonic", return_value=100+logs.SECRET_TTL+1):
            logs.redact("prune")
            self.assertEqual(len(logs._secrets), 0)

    def test_cache_single_flight_and_bounded_retention(self):
        cache = SnapshotCache(capacity=2)
        counter = []
        def load():
            counter.append(True)
            time.sleep(.04)
            return {"items": []}
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: cache.get("node", load), range(5)))
        self.assertEqual(len(counter), 1)
        cache.get("second", lambda: "ok")
        cache.get("third", lambda: "ok")
        self.assertEqual(len(cache.entries), 2)

    def test_lists_paginate_and_reject_repeated_continue(self):
        self.api.request = Mock(side_effect=[{"items": [1], "metadata": {"continue": "next"}}, {"items": [2]}])
        self.assertEqual(self.api.list_pages(self.cluster["id"], "/api/list"), [1, 2])
        self.assertIn("continue=next", self.api.request.call_args.args[1])
        self.api.request = Mock(return_value={"items": [], "metadata": {"continue": "loop"}})
        with self.assertRaises(ValueError):
            self.api.list_pages(self.cluster["id"], "/api/list")

    def test_readiness_and_crashloop_are_not_healthy_running(self):
        self.assertEqual(pod_status({"status": {"phase": "Running", "containerStatuses": [{"ready": False}]}}), "Pending")
        self.assertEqual(pod_status({"status": {"phase": "Running", "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}), "Failed")

    def test_configmap_row_uses_immutable_identity(self):
        window = self.window()
        window._render_resources("configmap", [{"metadata": {"name": "shared", "uid": "uid"}}], ("original", "old-ns", "Old Cluster"))
        with patch.object(window, "open_resource") as opened:
            window.open_resource_row("configmap", 0)
            opened.assert_called_once_with("original", "configmap", "old-ns", "shared", expected_uid="uid")
        window._invalidate_resource_list("configmap")
        with patch.object(window, "open_resource") as opened:
            window.open_resource_row("configmap", 0)
            opened.assert_not_called()

    def test_cancel_cluster_clears_password_and_settings_do_not_accumulate(self):
        window = self.window()
        dialog = ClusterDialog(window, self.store, self.root, self.cluster)
        self.assertTrue(dialog.password.text())
        dialog.reject()
        self.assertEqual(dialog.password.text(), "")
        dialog.deleteLater()
        for _ in range(3):
            QTimer.singleShot(10, lambda: next(d for d in window.findChildren(QDialog) if d.isVisible()).reject())
            window.clusters_dialog()
            QTest.qWait(20)
        self.assertEqual(len(window.findChildren(QDialog)), 0)

    def test_background_has_capacity_and_stop_rejects_new_work(self):
        background = Background()
        release = threading.Event()
        delivered = []
        try:
            for _ in range(60):
                background.submit(lambda: release.wait(2), lambda _: delivered.append(True))
            self.assertLessEqual(len(background.running), 48)
            background.stop()
            self.assertFalse(background.submit(lambda: True))
        finally:
            release.set()
            background.pool.waitForDone()
            QTest.qWait(20)
        self.assertEqual(delivered, [])

    def test_background_coalesces_to_latest_request(self):
        background = Background()
        release = threading.Event()
        executed, delivered = [], []
        def first():
            executed.append(0)
            release.wait(2)
            return 0
        try:
            background.submit(first, delivered.append, key="scope")
            for value in range(1, 20):
                background.submit(lambda value=value: (executed.append(value), value)[1], delivered.append, key="scope")
            self.assertEqual(len(background.running), 1)
            release.set()
            self.wait(lambda: len(delivered) == 2)
            self.assertEqual(executed, [0, 19])
            self.assertEqual(delivered, [0, 19])
        finally:
            release.set()
            background.stop()
            background.pool.waitForDone()

    def test_navigation_failure_restores_actual_target_without_retry_loop(self):
        window = self.window()
        api = Mock()
        api.cluster.return_value = {"name": "A"}
        api.namespaces.return_value = ["ns"]
        api.deployments.return_value = [{"metadata": {"name": "deployment", "uid": "uid"}}]
        api.replica_sets.return_value = []
        api.pods.return_value = []
        api.events.return_value = []
        resource = {"metadata": {"name": "deployment", "uid": "uid", "namespace": "ns", "resourceVersion": "1"}}
        panel = ResourceDialog(window, api, window.background, self.cluster["id"], "deployment", "ns", "deployment", resource)
        self.addCleanup(panel.deleteLater)
        self.wait(lambda: not window.background.running)
        panel.nav_cluster.addItem("B", "other")
        panel.nav_cluster.setCurrentIndex(panel.nav_cluster.findData("other"))
        api.namespaces.side_effect = PermissionError("RBAC")
        before = api.namespaces.call_count
        panel._load_navigation_namespaces(True)
        self.assertTrue(panel._navigation_pending)
        called = Mock()
        panel._mutate(called, lambda _: None)
        called.assert_not_called()
        self.wait(lambda: not window.background.running)
        self.assertEqual(panel.nav_cluster.currentData(), self.cluster["id"])
        self.assertFalse(panel._navigation_pending)
        QTest.qWait(30)
        self.assertEqual(api.namespaces.call_count, before+1)
        panel.close()

    def test_http_error_is_closed_and_request_after_shutdown_is_refused(self):
        import urllib.error
        denied = urllib.error.HTTPError("http://local", 403, "Forbidden", {}, io.BytesIO(b"denied"))
        self.proxy.url.return_value = "http://127.0.0.1"
        self.api.opener = Mock()
        self.api.opener.open.side_effect = denied
        with self.assertRaises(PermissionError):
            self.api.request(self.cluster["id"], "/api/v1/namespaces")
        self.assertTrue(denied.closed)
        self.api.stopped = True
        self.proxy.url.reset_mock()
        with self.assertRaises(RuntimeError):
            self.api.request(self.cluster["id"], "/api/v1/namespaces")
        self.proxy.url.assert_not_called()

    def test_preference_writer_preserves_last_choice_and_reports_disk_failure(self):
        fake_store = Mock()
        fake_store.preferences.return_value = {"theme": "dark"}
        writer = PreferenceWriter(fake_store)
        errors = []
        writer.failed.connect(errors.append)
        writer.save({"theme": "light"})
        writer.save({"theme": "dark"})
        self.wait(lambda: fake_store.save_preferences.called)
        self.assertEqual(fake_store.save_preferences.call_args.args[0], {"theme": "dark"})
        fake_store.save_preferences.side_effect = OSError("disk full")
        writer.save({"theme": "light"})
        self.wait(lambda: bool(errors))
        writer.close()
        writer.thread.join(2)
        self.assertFalse(writer.thread.is_alive())

    def test_single_instance_lock_excludes_second_owner(self):
        path = str(self.root / "quaber.lock")
        first, second = QLockFile(path), QLockFile(path)
        self.assertTrue(first.tryLock(0))
        self.assertFalse(second.tryLock(0))
        first.unlock()
        self.assertTrue(second.tryLock(0))
        second.unlock()

    def test_new_code_snapshot_excludes_userdata_and_key(self):
        from snapshot import create_snapshot
        import zipfile
        (self.root / "main.py").write_text("# fixture")
        archive = create_snapshot(self.root)
        with zipfile.ZipFile(archive) as source:
            self.assertEqual(source.namelist(), ["main.py"])

    def test_connection_response_is_bounded_without_changing_transport(self):
        from quaber.kubectl import test_connection
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x"*(4*1024*1024+1)
        opener = Mock()
        opener.open.return_value = response
        manager = Mock()
        manager.url.return_value = "http://127.0.0.1:1234/prefix"
        with patch("quaber.kubectl.ProxyManager", return_value=manager), patch("quaber.kubectl.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                test_connection(Path("fixture.exe"), self.store, self.cluster["server"], CONFIG)
        response.read.assert_called_once_with(4*1024*1024+1)
        manager.close.assert_called_once()
