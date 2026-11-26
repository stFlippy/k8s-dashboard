import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PyQt5.QtWidgets import QApplication, QMainWindow, QTreeWidget
from quaber.callback_diagnostics import failure_hook
from quaber.shutdown import ShutdownCoordinator, release_window


class ShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def coordinator(self):
        coordinator = ShutdownCoordinator(self.app)
        old = self.app.quitOnLastWindowClosed()
        self.addCleanup(lambda:self.app.setQuitOnLastWindowClosed(old))
        self.addCleanup(coordinator.deleteLater)
        self.addCleanup(coordinator.timer.stop)
        return coordinator

    def test_keyboard_interrupt_is_info_and_cancel_does_not_latch_real_failure(self):
        shutdown = SimpleNamespace(request=Mock(return_value=False))
        hook = failure_hook(SimpleNamespace(exit=Mock()),shutdown)
        with self.assertLogs('quaber',level='INFO') as logs:
            hook(KeyboardInterrupt,KeyboardInterrupt(),None)
        self.assertFalse(any('CRITICAL' in line for line in logs.output))
        shutdown.request.assert_called_once_with('user_interrupt',130)
        with self.assertLogs('quaber',level='CRITICAL') as logs:
            hook(ValueError,ValueError('token=synthetic-secret'),None)
            hook(ValueError,ValueError('duplicate'),None)
        self.assertNotIn('synthetic-secret',''.join(logs.output))
        self.assertEqual(shutdown.request.call_count,2)
        shutdown.request.assert_called_with('callback_failure',1,True)

    def test_signal_handler_defers_work_and_bootstrap_interrupt_exits_once(self):
        coordinator = self.coordinator()
        with patch.object(self.app,'exit') as exit:
            coordinator.signal_interrupt()
            coordinator.signal_interrupt()
            exit.assert_not_called()
            coordinator.poll()
            coordinator.poll()
        exit.assert_called_once_with(130)

    def test_closed_window_and_child_views_are_released_before_application_exit(self):
        from PyQt5 import sip
        window = QMainWindow()
        child = QTreeWidget(window)
        window.setCentralWidget(child)
        window.close()
        self.assertFalse(sip.isdeleted(window))
        release_window(window)
        self.assertTrue(sip.isdeleted(window))
        self.assertTrue(sip.isdeleted(child))
        release_window(window)
        release_window(None)

    def test_logging_failure_cannot_prevent_fatal_or_interrupt_shutdown(self):
        for exc_type,code in ((ValueError,1),(KeyboardInterrupt,130)):
            shutdown = SimpleNamespace(request=Mock())
            with patch('quaber.callback_diagnostics.logging.getLogger',side_effect=OSError('log unavailable')):
                failure_hook(SimpleNamespace(exit=Mock()),shutdown)(exc_type,exc_type(),None)
            self.assertEqual(shutdown.request.call_count,1)
            self.assertEqual(shutdown.request.call_args.args[1],code)

    def test_cancel_close_keeps_running_then_fatal_uses_same_cleanup_path(self):
        coordinator = self.coordinator()
        window = SimpleNamespace(close=Mock(return_value=False), _closing=False,
            _cleanup_thread=SimpleNamespace(is_alive=lambda:False), write_guard=Mock(),
            preferences={'read_only':False}, api=SimpleNamespace(stopped=False))
        coordinator.attach(window)
        self.assertFalse(coordinator.request('user_interrupt',130))
        self.assertFalse(coordinator.accepted)
        self.assertFalse(window.api.stopped)
        window.close.return_value = True
        with patch.object(self.app,'exit') as exit:
            self.assertTrue(coordinator.request('callback_failure',1,True))
            coordinator.request('user_interrupt',130)
            coordinator.poll()
        self.assertEqual(window.close.call_count,2)
        self.assertTrue(window.preferences['read_only'])
        self.assertTrue(window.api.stopped)
        exit.assert_called_once_with(1)

    def test_interrupt_before_attach_closes_constructed_window_before_exit(self):
        coordinator = self.coordinator()
        coordinator.request('user_interrupt',130)
        window = SimpleNamespace(close=Mock(return_value=True),_closing=False,
                                 _cleanup_thread=SimpleNamespace(is_alive=lambda:False))
        with patch.object(self.app,'exit') as exit:
            coordinator.attach(window)
            window.close.assert_called_once()
            coordinator.poll()
        exit.assert_called_once_with(130)
