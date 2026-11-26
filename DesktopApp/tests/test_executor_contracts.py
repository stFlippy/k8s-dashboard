import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QWidget, QDialogButtonBox
from PyQt5.QtTest import QTest
from helpers_background import DeferredBackend
from quaber.background import PanelBackground
from quaber.confirmations import ResourceConfirmation
from quaber.write_guard import WriteGuard
from test_ux import ShellFixture


class LifecycleContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_queued_result_cannot_destroy_sender_before_worker_returns(self):
        import threading,time
        from PyQt5 import sip
        from PyQt5.QtCore import QCoreApplication,QEvent
        from quaber.background import Background,Task
        backend=Background()
        gate=threading.Event()
        original=Task.run
        done=Mock()
        def delayed_return(task):
            original(task)
            gate.wait(5)
        def cleanup():
            gate.set()
            backend.stop()
            backend.pool.waitForDone(5000)
            backend._collect_retired()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            backend.deleteLater()
        self.addCleanup(cleanup)
        with patch.object(Task,'run',delayed_return):
            backend.submit(lambda:'result',done)
            until=time.monotonic()+3
            while not done.called and time.monotonic()<until: QTest.qWait(5)
            done.assert_called_once_with('result')
            self.assertEqual(backend.pool.activeThreadCount(),1)
            sender=next(iter(backend._retired)).signals
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            self.assertFalse(sip.isdeleted(sender))
            gate.set()
            self.assertTrue(backend.pool.waitForDone(5000))
            QTest.qWait(100)
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            self.assertTrue(sip.isdeleted(sender))
            self.assertFalse(backend._retired)

    def test_panel_late_adoption_and_exact_terminal_cleanup(self):
        owner, backend = QWidget(), DeferredBackend()
        self.addCleanup(owner.deleteLater)
        panel = PanelBackground(backend, owner)
        done, failed, cancel, final = Mock(), Mock(), Mock(), Mock()
        self.assertIs(panel.submit(lambda: 'old', done, failed, demand='tab', cancelled=cancel, finalized=final), True)
        job = backend.jobs[0]
        self.assertEqual(job.options['demand'], 'tab')
        panel.cancel_reads(tab_only=True)
        backend.finish(job_id=job.id, cancel=True)
        backend.deliver_stale(job,value='stale')
        backend.deliver_stale(job,failure='late failure')
        done.assert_not_called()
        failed.assert_not_called()
        cancel.assert_called_once()
        final.assert_called_once_with('CANCELLED')
        backend.admit = False
        self.assertIs(panel.submit(lambda: 'rejected'), False)

    def test_scale_zero_real_confirmation_yes_and_no(self):
        owner = QWidget()
        self.addCleanup(owner.deleteLater)
        for standard, expected in ((QDialogButtonBox.Yes, 1), (QDialogButtonBox.No, 0)):
            dialog = ResourceConfirmation(owner, 'Scale to zero', 'c/ns/app [UID=d]', 'Stop selected deployment?')
            self.assertEqual(dialog.target_label.text(), 'c/ns/app [UID=d]')
            fired = []
            def click():
                fired.append(True)
                QTest.mouseClick(dialog.buttons.button(standard), Qt.LeftButton)
            QTimer.singleShot(0,click)
            watchdog = QTimer(dialog)
            watchdog.setSingleShot(True)
            watchdog.timeout.connect(dialog.reject)
            watchdog.start(1000)
            self.assertEqual(dialog.exec_(),expected)
            self.assertEqual(fired,[True])
            watchdog.stop()
            dialog.deleteLater()

    def test_scale_permit_binds_approved_opaque_rv(self):
        guard = WriteGuard(lambda:True,lambda cid:'credential')
        permit = guard.issue(('c','deployment','ns','app','uid'),'credential','scale',authorized=True,
                             parameters={'replicas':2,'resourceVersion':'opaque:old'})
        body = dict(apiVersion='autoscaling/v1',kind='Scale',metadata=dict(
            name='app',namespace='ns',uid='uid',resourceVersion='opaque:old'),spec={'replicas':2})
        permit.check_rest_parameters('PUT',body)
        body['metadata']['resourceVersion'] = 'opaque:new'
        with self.assertRaises(PermissionError): permit.check_rest_parameters('PUT',body)

    def test_closed_owner_error_never_opens_a_message_box(self):
        from PyQt5 import sip
        from quaber.ui import error
        owner = QWidget()
        sip.delete(owner)
        with patch('quaber.ui.QMessageBox.warning') as warning:
            error(owner,'late synthetic error')
        warning.assert_not_called()

    def test_displaced_and_failed_callback_finalize_pending_exactly_once(self):
        from quaber.background import Background
        background = Background()
        old, latest = Mock(), Mock()
        with patch.object(background.pool,'start'):
            background.submit(lambda:None,done=Mock(side_effect=ValueError('callback failed')),key='one')
            running = next(iter(background.running))
            background.submit(lambda:None,key='one',finalized=old)
            background.submit(lambda:None,key='one',finalized=latest)
            old.assert_called_once_with('CANCELLED')
            with self.assertRaisesRegex(ValueError,'callback failed'):
                background._done(running,None)
        latest.assert_called_once_with('CANCELLED')
        self.assertFalse(background.running)
        self.assertFalse(background._pending)
        background.stop()

    def test_stop_cleans_other_owned_operations_before_raising(self):
        from quaber.background import Background
        background = Background()
        final = Mock()
        with patch.object(background.pool,'start'):
            background.submit(lambda:None,finalized=Mock(side_effect=ValueError('first cleanup failed')))
            background.submit(lambda:None,finalized=final)
            with self.assertRaisesRegex(ValueError,'first cleanup failed'):
                background.stop()
        final.assert_called_once_with('CANCELLED')
        self.assertFalse(background._terminal)
        self.assertFalse(background._keys)
        self.assertFalse(background.running)

    def test_registered_window_cleanup_survives_early_assertion(self):
        from helpers_window import create_window
        from quaber.storage import UserStore
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = UserStore(root/'UserData')
            class EarlyFailure(unittest.TestCase):
                def runTest(test):
                    with patch('quaber.ui.locate_kubectl',return_value=None):
                        create_window(test,store,root)
                    raise AssertionError('intentional fixture self-test')
            result = unittest.TestResult()
            EarlyFailure().run(result)
            self.assertEqual(len(result.failures),1)
            self.assertEqual(result.errors,[])
            log = root/'UserData/logs/quaber.log'
            moved = log.with_suffix('.closed')
            log.rename(moved)
            moved.unlink()


class ScaleDraftContracts(ShellFixture, unittest.TestCase):
    def test_terminal_external_revision_is_rejected_and_input_preserved(self):
        panel = self.open_panel('pod')
        panel.tabs.setCurrentWidget(panel.terminal_page)
        self.window.set_read_only(False)
        panel.console_input.setText('printf synthetic')
        original = self.api.cluster.side_effect
        self.api.cluster.side_effect = lambda cid: dict(original(cid),username='changed-subject')
        with patch('quaber.ui.error') as warning:
            panel.run_console()
        self.api.execute_pod.assert_not_called()
        self.assertEqual(panel.console_input.text(),'printf synthetic')
        self.assertTrue(self.window.preferences['read_only'])
        warning.assert_called_once()
    def test_changed_rv_preserves_draft_and_never_calls_scale(self):
        panel = self.open_panel('deployment')
        self.window.set_read_only(False)
        backend = DeferredBackend()
        panel.background.background = backend
        panel.scale_by(1)
        approved = copy.deepcopy(panel._scale_snapshot)
        self.deployment['metadata']['resourceVersion'] = 'changed-after-confirmation'
        with patch('quaber.ui.error') as notice:
            panel._commit_scale_draft()
            backend.finish(execute=True)
        self.api.scale.assert_not_called()
        notice.assert_called_once()
        self.assertEqual(panel._scale_snapshot,approved)
        self.assertEqual(panel._scaling_target,2)

    def test_zero_cancel_preserves_draft_without_dispatch(self):
        panel = self.open_panel('deployment')
        self.window.set_read_only(False)
        backend = DeferredBackend()
        panel.background.background = backend
        panel.scale_by(-1)
        approved = copy.deepcopy(panel._scale_snapshot)
        with patch('quaber.ui.confirm',return_value=False) as confirmation:
            panel._commit_scale_draft()
        self.assertEqual(confirmation.call_args.args[1],'Scale to zero')
        self.assertEqual(panel._scale_snapshot,approved)
        self.assertEqual(panel._scaling_target,0)
        self.assertFalse(backend.jobs)
        self.api.scale.assert_not_called()
