"""Synthetic Qt events and resume evidence; no production store or network."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PyQt5.QtCore import QObject, QEvent, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from quaber.monitoring import MonitoringController, MonitoringState
from quaber.resume import ResumeAttempt, core_read


class ActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_key_event_with_shadowed_window_and_non_activity_event(self):
        widget = QWidget()
        widget.window = QWidget()
        policy = SimpleNamespace(activity=Mock())
        controller = SimpleNamespace(window=object(), policy=policy)
        class Filter(QObject):
            def eventFilter(self, watched, event):
                return MonitoringController.eventFilter(controller, watched, event)
        observer = Filter()
        widget.installEventFilter(observer)
        QTest.keyClick(widget, Qt.Key_A)
        self.assertEqual(policy.activity.call_count, 1)
        QApplication.sendEvent(widget, QEvent(QEvent.Timer))
        self.assertEqual(policy.activity.call_count, 1)
        widget.window.deleteLater()
        widget.deleteLater()

    def test_attempt_identity_is_frozen_and_optional_reads_are_excluded(self):
        attempt = ResumeAttempt.start(7, [('c', 'revision')], 5)
        self.assertEqual(attempt.deadline, 35)
        with self.assertRaises(AttributeError):
            attempt.epoch = 8
        for path in ('/apis/metrics.k8s.io/v1beta1/pods', '/api/v1/namespaces/ns/pods/p/log', '/api/v1/namespaces/ns/events'):
            self.assertFalse(core_read(path))
        self.assertTrue(core_read('/apis/apps/v1/namespaces/ns/deployments?limit=10'))

    def test_core_evidence_cannot_resume_a_different_epoch(self):
        policy = MonitoringState()
        policy.sleep('inactivity')
        policy.reconnect()
        owner = SimpleNamespace(policy=policy, attempt=ResumeAttempt.start(policy.epoch, [], 0),
                                valid=policy.allows, window=Mock(), resume_results={}, changed=Mock())
        token = policy.token()
        policy.sleep('lock')
        MonitoringController.observe_read(owner, token)
        self.assertEqual(policy.state, 'SLEEPING')
        owner.changed.emit.assert_not_called()

    def test_failed_optional_task_does_not_sleep_and_pending_gets_new_token(self):
        from quaber.background import Background
        from quaber.monitoring import _read_token
        state = MonitoringState()
        state.sleep('inactivity')
        state.reconnect()
        background = Background()
        background.monitoring = SimpleNamespace(token=state.token, valid=state.allows,
                                                read_scope=state.read_scope, observe_read=Mock())
        seen, failed = [], Mock()
        def plan():
            state.check()
            seen.append(_read_token.get().epoch)
        with patch.object(background.pool, 'start'):
            background.submit(plan, lambda value: None, failed, key='read')
            old = next(iter(background.running))
            background.submit(plan, lambda value: None, failed, key='read')
            state._invalidate()
            background._failed(old, 'optional metrics unavailable')
            self.assertEqual(state.state, 'RECONNECTING')
            failed.assert_not_called()  # Old generation is retired.
            current = next(iter(background.running))
            current.function()
            self.assertEqual(seen, [state.epoch])
            background._done(current, None)
        background.stop()

    def test_cancelled_task_has_separate_signal(self):
        from quaber.background import Task
        from quaber.monitoring import MonitoringCancelled
        def cancelled():
            raise MonitoringCancelled('old generation')
        task = Task(cancelled)
        failed, done, cancel = Mock(), Mock(), Mock()
        task.signals.failed.connect(failed)
        task.signals.done.connect(done)
        task.signals.cancelled.connect(cancel)
        task.run()
        failed.assert_not_called()
        done.assert_not_called()
        cancel.assert_called_once_with(task)
