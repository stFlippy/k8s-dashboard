"""Recent Pod markers, local ageing and the timed matrix deployment mode."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from quaber.pod_recency import DeploymentMode, RECENT_SECONDS, recent_activity_deadline
from quaber.views import validate_preferences
from quaber.widgets import COLORS, pod_marker_style
from test_ux import ShellFixture


class PodActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)

    def timestamp(self, seconds):
        return (self.now - timedelta(seconds=seconds)).isoformat()

    def test_creation_includes_replacement_with_no_container_restarts(self):
        for seconds, expected in ((0, True), (1799.999, True), (1800, False), (1801, False), (-1, False)):
            with self.subTest(seconds=seconds):
                pod = {"metadata": {"creationTimestamp": self.timestamp(seconds)},
                       "status": {"containerStatuses": [{"restartCount": 0}]}}
                deadline = recent_activity_deadline(pod, self.now)
                self.assertEqual(deadline is not None, expected)
                if expected:
                    self.assertEqual(deadline, self.now + timedelta(seconds=RECENT_SECONDS-seconds))

    def test_restart_of_old_pod_and_latest_activity_define_expiration(self):
        for key in ("containerStatuses", "initContainerStatuses", "ephemeralContainerStatuses"):
            with self.subTest(key=key):
                pod = {"metadata": {"creationTimestamp": self.timestamp(86400)}, "status": {key: [{
                    "restartCount": 2,
                    "state": {"running": {"startedAt": self.timestamp(90)}},
                    "lastState": {"terminated": {"finishedAt": self.timestamp(100), "startedAt": self.timestamp(3600)}}}]}}
                self.assertEqual(recent_activity_deadline(pod, self.now), self.now+timedelta(seconds=1710))
                pod["status"][key][0]["restartCount"] = 0
                self.assertIsNone(recent_activity_deadline(pod, self.now))

    def test_old_restart_counter_does_not_mark_pod_forever(self):
        pod = {"status": {"containerStatuses": [{"restartCount": 7,
            "state": {"running": {"startedAt": self.timestamp(1801)}},
            "lastState": {"terminated": {"finishedAt": self.timestamp(1802)}}}]}}
        self.assertIsNone(recent_activity_deadline(pod, self.now))

    def test_invalid_future_and_timezone_less_timestamps_are_ignored(self):
        for value in (None, 123, "not a date", "2026-11-13T07:59:00", self.timestamp(-10)):
            with self.subTest(value=value):
                self.assertIsNone(recent_activity_deadline({"metadata": {"creationTimestamp": value}}, self.now))
        self.assertIsNotNone(recent_activity_deadline({"metadata": {"creationTimestamp": "2026-11-13T11:59:00+04:00"}}, self.now))

    def test_deploy_mode_changes_only_running_recent_markers(self):
        for phase in ("Running", "Pending", "Failed", "Unknown"):
            pod = {"status": {"phase": phase}, "_recent": True}
            with self.subTest(phase=phase):
                self.assertEqual(pod_marker_style(pod), (COLORS["Restart"], True) if phase == "Running" else (COLORS[phase], False))
                expected = (COLORS[phase], False)
                self.assertEqual(pod_marker_style(pod, True), expected)
                self.assertTrue(pod["_recent"])
                pod["_stale"] = True
                self.assertEqual(pod_marker_style(pod, True), (COLORS["Unknown"], False))
        unready = {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}, "_recent": True}
        self.assertEqual(pod_marker_style(unready, True), (COLORS["Pending"], False))


class DeploymentModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def mode(self, expires_at=None):
        self.wall, self.steady = 10000, 500
        mode = DeploymentMode(expires_at=expires_at, wall_clock=lambda: self.wall, steady_clock=lambda: self.steady)
        self.addCleanup(mode.deleteLater)
        self.addCleanup(mode.timer.stop)
        return mode

    def test_exact_thirty_minute_expiry_and_manual_disable(self):
        mode = self.mode()
        changes = []
        mode.changed.connect(changes.append)
        mode.set_active(True)
        self.assertEqual(mode.remaining_seconds, 1800)
        self.wall += 1799
        self.steady += 1799
        mode.tick()
        self.assertEqual(mode.remaining_seconds, 1)
        mode.set_active(True)  # refresh/view changes cannot extend a running mode
        self.wall += 1
        self.steady += 1
        mode.tick()
        self.assertFalse(mode.active)
        self.assertIsNone(mode.expires_at)
        self.assertFalse(mode.timer.isActive())
        self.assertEqual(changes, [True, False])
        mode.set_active(True)
        mode.set_active(False)
        self.assertFalse(mode.active)

    def test_restore_keeps_original_deadline_and_rejects_invalid_deadlines(self):
        mode = self.mode(10600)
        self.assertEqual(mode.remaining_seconds, 600)
        self.assertEqual(mode.expires_at, 10600)
        for invalid in (9999, 10000, 11801, float("nan"), float("inf"), True, "11800", 10**1000):
            with self.subTest(deadline=str(invalid)[:30]):
                restored = self.mode(invalid)
                self.assertFalse(restored.active)
                self.assertIsNone(restored.expires_at)

    def test_wall_clock_jump_does_not_prolong_mode_and_sleep_expires_it(self):
        mode = self.mode()
        mode.set_active(True)
        self.wall -= 3600
        self.steady += 1800
        mode.tick()
        self.assertFalse(mode.active)
        mode.set_active(True)
        self.wall += 1801  # wall clock advances across sleep without an event loop
        mode.tick()
        self.assertFalse(mode.active)

    def test_preference_deadline_validation(self):
        self.assertEqual(validate_preferences({"deploy_mode_until": 12345})["deploy_mode_until"], 12345)
        for value in (None, "12345", True, -1, float("nan"), float("inf"), 10**1000):
            self.assertNotIn("deploy_mode_until", validate_preferences({"deploy_mode_until": value}))


class MatrixDeploymentModeTests(ShellFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pod["metadata"]["creationTimestamp"] = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        rs = {"metadata": {"name": "app-rs", "uid": "rs", "ownerReferences": [
            {"controller": True, "kind": "Deployment", "name": "app", "uid": "d"}]}}
        self.window.snapshot = {"c": {"name": "Production", "namespaces": {"ns": {
            "deployments": [self.deployment], "pods": [self.pod], "replica_sets": [rs]}}}}
        self.window.render_matrix()

    def visible_pods(self):
        return self.window.matrix.item(0, 1).data(Qt.ItemDataRole.UserRole)

    def test_toggle_is_visual_and_filters_keep_the_same_pod(self):
        self.window.status_filters["Running"].setChecked(False)
        self.assertEqual(len(self.visible_pods()), 1)  # admitted by recent activity
        self.window.deploy_mode.setChecked(True)
        self.assertEqual(len(self.visible_pods()), 1)
        self.assertTrue(self.visible_pods()[0]["_recent"])
        self.assertTrue(self.window.matrix.property("deployMode"))
        self.assertIn("30:00", self.window.deploy_mode.text())
        self.assertFalse(self.window.shell_actions.mode.isChecked())
        self.window.recent.setChecked(False)
        self.assertEqual(self.visible_pods(), [])
        self.api.resource.assert_not_called()
        self.api.pods.assert_not_called()
        self.window.refresh.assert_not_called()

    def test_actual_markers_change_in_both_themes(self):
        for light in (False, True):
            self.window.preferences["theme"] = "light" if light else "dark"
            self.window._apply_theme()
            self.window.deploy_mode.setChecked(False)
            self.app.processEvents()
            def marker_colors():
                matrix = self.window.matrix
                from quaber.widgets import pod_rects
                rect = matrix.visualRect(matrix.model().index(0, 1))
                area = pod_rects(matrix.content_rect(rect, 0), 1)[0]
                image = matrix.viewport().grab().toImage()
                return {image.pixelColor(x, y).name().upper()
                        for x in range(area.left(), area.right()+1)
                        for y in range(area.top(), area.bottom()+1)}
            self.assertIn(COLORS["Restart"].upper(), marker_colors())
            self.window.deploy_mode.setChecked(True)
            self.app.processEvents()
            self.assertIn(COLORS["Running"].upper(), marker_colors())
            self.assertNotIn(COLORS["Restart"].upper(), marker_colors())

    def test_automatic_expiry_clears_checkbox_and_restores_blue(self):
        self.window.deploy_mode.setChecked(True)
        mode = self.window.deployment_mode
        with patch.object(mode, "_wall_clock", return_value=mode.expires_at+1):
            mode.tick()
        self.assertFalse(self.window.deploy_mode.isChecked())
        self.assertFalse(self.window.matrix.property("deployMode"))
        self.assertEqual(pod_marker_style(self.visible_pods()[0]), (COLORS["Restart"], True))
        self.assertNotIn("deploy_mode_until", self.window.preferences)
        self.assertEqual(self.window.deploy_mode.text(), "Режим деплоя")

    def test_activity_ages_out_without_network_refresh(self):
        self.window.status_filters["Running"].setChecked(False)
        self.pod["metadata"]["creationTimestamp"] = (datetime.now(timezone.utc)-timedelta(seconds=RECENT_SECONDS-0.6)).isoformat()
        self.window.render_matrix()
        self.assertEqual(len(self.visible_pods()), 1)
        self.assertTrue(self.window.recent_age_timer.isActive())
        QTest.qWait(1000)
        self.assertEqual(self.visible_pods(), [])
        self.assertFalse(self.window.recent_age_timer.isActive())
        self.window.refresh.assert_not_called()
        self.api.pods.assert_not_called()

    def test_global_mode_persists_deadline_and_is_not_reset_by_view_change(self):
        self.window.deploy_mode.setChecked(True)
        deadline = self.window.deployment_mode.expires_at
        self.assertEqual(self.window.preferences["deploy_mode_until"], deadline)
        mode = self.window.deployment_mode
        with patch.object(mode, "_wall_clock", return_value=deadline-599):
            mode.tick()
            self.assertIn("09:59", self.window.deploy_mode.text())
            self.window.views.add("Second")
            self.window._load_view_controls()
            self.window.render_matrix()
            self.assertEqual(mode.expires_at, deadline)
            self.assertTrue(self.window.deploy_mode.isChecked())
            self.assertTrue(self.window.matrix.property("deployMode"))
            self.assertNotIn("deploy_mode_until", self.window.views.active)
        self.wait_for_preferences(deadline)
        self.assertEqual(self.window.store.preferences()["deploy_mode_until"], deadline)
        self.window.deploy_mode.setChecked(False)
        self.wait_for_preferences(None)
        self.assertNotIn("deploy_mode_until", self.window.store.preferences())

    def wait_for_preferences(self, expected):
        timeout = time.monotonic()+3
        while self.window.preference_writer.last.get("deploy_mode_until") != expected and time.monotonic() < timeout:
            QTest.qWait(10)
        self.assertEqual(self.window.preference_writer.last.get("deploy_mode_until"), expected)
